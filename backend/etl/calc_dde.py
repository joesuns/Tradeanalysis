import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from backend.etl.base import (
    ema, to_float_safe, weighted_window_slopes, sliding_window_mean_abs,
    compute_price_signal_divergence,
    insert_dws_batch, compute_input_fingerprint, check_dwd_unchanged,
    load_latest_fingerprints, resolve_ema_seeds,
    compute_history_signature,
    SkipReason, CalcResult,
)
from backend.etl.recalc_spec import RecalcSpec

logger = logging.getLogger(__name__)


class DDECalculator:
    """DDE (Data Display Estimate) indicator calculator.

    Computes DDX, DDX2 from moneyflow data, plus divergence, trend, and alerts.
    Divergence/trend/alert logic mirrors MACD but uses DDX/DDX2.
    Works for both daily and weekly frequencies.
    """

    RECALC_SPEC_DAILY = RecalcSpec(lookback=60, seed=5, event_tail=5, min_rows=10)
    RECALC_SPEC_WEEKLY = RecalcSpec(lookback=60, seed=5, event_tail=5, min_rows=10)

    SIGNATURE_COLS = [
        "buy_lg_vol", "sell_lg_vol", "buy_elg_vol", "sell_elg_vol",
        "total_vol", "net_mf_amount", "close_qfq",
    ]

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        if freq == "daily":
            self.src_table = "dwd_daily_moneyflow"
            self.quote_table = "dwd_daily_quote"
        else:
            self.src_table = "dwd_daily_moneyflow"
            self.quote_table = "dwd_weekly_quote"
        self.dws_table = f"dws_dde_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str,
                  recalc_start: str = None) -> CalcResult:
        result = CalcResult()
        latest_fps = load_latest_fingerprints(self.con, self.dws_table, ts_codes)
        load_start = None
        if recalc_start:
            from backend.etl.recalc_spec import resolve_load_start
            load_start = resolve_load_start(self.con, recalc_start, self.freq)
        if self.freq == "daily":
            groups = self._load_daily_batch(ts_codes, start_date=load_start)
        else:
            groups = self._load_weekly_batch(ts_codes, start_date=load_start)
        empty = pd.DataFrame()
        for ts_code in ts_codes:
            df = groups.get(ts_code, empty)

            if df.empty:
                if ts_code.endswith(".BJ"):
                    logger.debug("DDE %s skip %s: BSE -- moneyflow unavailable",
                                 self.freq, ts_code)
                    result.add_skip(SkipReason.SOURCE_UNAVAILABLE, ts_code,
                                    "BSE stocks have no moneyflow data from tushare")
                else:
                    logger.debug("DDE %s skip %s: no DWD moneyflow data",
                                 self.freq, ts_code)
                    result.add_skip(SkipReason.NO_DWD_DATA, ts_code,
                                    "DWD moneyflow returned 0 rows")
                continue
            if len(df) < 10:
                logger.debug("DDE %s skip %s: %d rows < 10",
                             self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=10")
                continue

            if check_dwd_unchanged(self.con, self.dws_table, ts_code, df,
                                   latest_fps=latest_fps, recalc_start=recalc_start):
                result.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                                "DWD fingerprint match")
                continue

            fp = compute_input_fingerprint(df, recalc_start=recalc_start)
            ema_seeds = resolve_ema_seeds(
                self.con, self.dws_table, ts_code, df, self.freq,
                ("ddx2",), recalc_start,
            )
            df = self._compute_indicators(df, ema_seeds=ema_seeds)
            if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                            write_start=recalc_start,
                            write_end=calc_date if recalc_start else None):
                result.calculated += 1
        return result

    def _load_daily(self, ts_code: str) -> pd.DataFrame:
        """Load daily moneyflow + close data."""
        return self.con.execute(f"""
            SELECT m.trade_date, m.buy_lg_vol, m.sell_lg_vol,
                   m.buy_elg_vol, m.sell_elg_vol, m.total_vol,
                   m.net_mf_amount, q.close_qfq
            FROM {self.src_table} m
            JOIN {self.quote_table} q ON m.ts_code = q.ts_code AND m.trade_date = q.trade_date
            WHERE m.ts_code = ? {'' if self.freq == 'weekly' else 'AND q.is_suspended = 0'}
            ORDER BY m.trade_date
        """, (ts_code,)).df()

    def _load_daily_batch(self, ts_codes: list[str], chunk_size: int = 400,
                          start_date: str = None) -> dict:
        """Batch version of _load_daily: one query per chunk → {ts_code: df}.

        Each frame is identical to _load_daily(ts_code) (same columns/order/filter).
        """
        susp = '' if self.freq == 'weekly' else 'AND q.is_suspended = 0'
        groups = {}
        for i in range(0, len(ts_codes), chunk_size):
            chunk = ts_codes[i:i + chunk_size]
            ph = ",".join(["?"] * len(chunk))
            date_filter = " AND m.trade_date >= ?" if start_date else ""
            params = list(chunk)
            if start_date:
                params.append(start_date)
            big = self.con.execute(f"""
                SELECT m.ts_code, m.trade_date, m.buy_lg_vol, m.sell_lg_vol,
                       m.buy_elg_vol, m.sell_elg_vol, m.total_vol,
                       m.net_mf_amount, q.close_qfq
                FROM {self.src_table} m
                JOIN {self.quote_table} q ON m.ts_code = q.ts_code AND m.trade_date = q.trade_date
                WHERE m.ts_code IN ({ph}) {susp}{date_filter}
                ORDER BY m.ts_code, m.trade_date
            """, params).df()
            if big.empty:
                continue
            for ts_code, g in big.groupby("ts_code", sort=False):
                groups[ts_code] = g.drop(columns=["ts_code"]).reset_index(drop=True)
        return groups

    def _load_weekly(self, ts_code: str) -> pd.DataFrame:
        """Aggregate daily moneyflow to weekly granularity — single-query.

        Uses LAG window function to determine week boundaries, then SUM
        aggregates moneyflow between consecutive week-end dates in one pass.
        Replaces ~50 per-week SQL calls with 1 call (~150x fewer roundtrips).
        """
        return self.con.execute(f"""
            WITH week_ranges AS (
                SELECT
                    wq.ts_code,
                    wq.trade_date AS week_end,
                    COALESCE(
                        LAG(wq.trade_date) OVER (
                            PARTITION BY wq.ts_code ORDER BY wq.trade_date
                        ),
                        strftime(
                            CAST(
                                SUBSTR(wq.trade_date, 1, 4) || '-' ||
                                SUBSTR(wq.trade_date, 5, 2) || '-' ||
                                SUBSTR(wq.trade_date, 7, 2)
                                AS DATE
                            ) - INTERVAL 7 DAY,
                            '%Y%m%d'
                        )
                    ) AS week_start
                FROM dwd_weekly_quote wq
                JOIN dim_date dd ON wq.trade_date = dd.trade_date
                WHERE wq.ts_code = ? AND dd.is_week_end = 1
            ),
            weekly_agg AS (
                SELECT
                    wr.week_end,
                    COALESCE(SUM(mf.buy_lg_vol),   0) AS buy_lg_vol,
                    COALESCE(SUM(mf.sell_lg_vol),  0) AS sell_lg_vol,
                    COALESCE(SUM(mf.buy_elg_vol),  0) AS buy_elg_vol,
                    COALESCE(SUM(mf.sell_elg_vol), 0) AS sell_elg_vol,
                    COALESCE(SUM(mf.total_vol),    0) AS total_vol,
                    COALESCE(SUM(mf.net_mf_amount),0) AS net_mf_amount,
                    COUNT(DISTINCT mf.trade_date) AS active_days,
                    COUNT(DISTINCT dd_t.trade_date) AS expected_days
                FROM week_ranges wr
                JOIN {self.src_table} mf
                    ON wr.ts_code = mf.ts_code
                    AND mf.trade_date > wr.week_start
                    AND mf.trade_date <= wr.week_end
                JOIN dwd_daily_quote q
                    ON mf.ts_code = q.ts_code
                    AND mf.trade_date = q.trade_date
                    AND q.is_suspended = 0
                JOIN dim_date dd_t
                    ON dd_t.trade_date > wr.week_start
                    AND dd_t.trade_date <= wr.week_end
                    AND dd_t.is_trade_day = 1
                GROUP BY wr.week_end
            )
            SELECT
                wa.week_end   AS trade_date,
                wa.buy_lg_vol,
                wa.sell_lg_vol,
                wa.buy_elg_vol,
                wa.sell_elg_vol,
                wa.total_vol,
                wa.net_mf_amount,
                wa.active_days,
                wa.expected_days,
                wq.close_qfq,
                CASE
                    WHEN wa.active_days < wa.expected_days * 0.6
                    THEN 1 ELSE 0
                END AS _skip_dde
            FROM weekly_agg wa
            JOIN dwd_weekly_quote wq
                ON wq.ts_code = ? AND wq.trade_date = wa.week_end
            WHERE wa.expected_days > 0
            ORDER BY wa.week_end
        """, (ts_code, ts_code)).df()

    def _load_weekly_batch(self, ts_codes: list[str], chunk_size: int = 400,
                           start_date: str = None) -> dict:
        """Batch version of _load_weekly: one query per chunk → {ts_code: df}.

        Carries ts_code through every CTE (PARTITION/GROUP BY already keyed by
        ts_code) so each per-stock frame is identical to _load_weekly(ts_code).
        """
        groups = {}
        for i in range(0, len(ts_codes), chunk_size):
            chunk = ts_codes[i:i + chunk_size]
            ph = ",".join(["?"] * len(chunk))
            big = self.con.execute(f"""
                WITH week_ranges AS (
                    SELECT
                        wq.ts_code,
                        wq.trade_date AS week_end,
                        COALESCE(
                            LAG(wq.trade_date) OVER (
                                PARTITION BY wq.ts_code ORDER BY wq.trade_date
                            ),
                            strftime(
                                CAST(
                                    SUBSTR(wq.trade_date, 1, 4) || '-' ||
                                    SUBSTR(wq.trade_date, 5, 2) || '-' ||
                                    SUBSTR(wq.trade_date, 7, 2)
                                    AS DATE
                                ) - INTERVAL 7 DAY,
                                '%Y%m%d'
                            )
                        ) AS week_start
                    FROM dwd_weekly_quote wq
                    JOIN dim_date dd ON wq.trade_date = dd.trade_date
                    WHERE wq.ts_code IN ({ph}) AND dd.is_week_end = 1
                ),
                weekly_agg AS (
                    SELECT
                        wr.ts_code,
                        wr.week_end,
                        COALESCE(SUM(mf.buy_lg_vol),   0) AS buy_lg_vol,
                        COALESCE(SUM(mf.sell_lg_vol),  0) AS sell_lg_vol,
                        COALESCE(SUM(mf.buy_elg_vol),  0) AS buy_elg_vol,
                        COALESCE(SUM(mf.sell_elg_vol), 0) AS sell_elg_vol,
                        COALESCE(SUM(mf.total_vol),    0) AS total_vol,
                        COALESCE(SUM(mf.net_mf_amount),0) AS net_mf_amount,
                        COUNT(DISTINCT mf.trade_date) AS active_days,
                        COUNT(DISTINCT dd_t.trade_date) AS expected_days
                    FROM week_ranges wr
                    JOIN {self.src_table} mf
                        ON wr.ts_code = mf.ts_code
                        AND mf.trade_date > wr.week_start
                        AND mf.trade_date <= wr.week_end
                    JOIN dwd_daily_quote q
                        ON mf.ts_code = q.ts_code
                        AND mf.trade_date = q.trade_date
                        AND q.is_suspended = 0
                    JOIN dim_date dd_t
                        ON dd_t.trade_date > wr.week_start
                        AND dd_t.trade_date <= wr.week_end
                        AND dd_t.is_trade_day = 1
                    GROUP BY wr.ts_code, wr.week_end
                )
                SELECT
                    wa.ts_code,
                    wa.week_end   AS trade_date,
                    wa.buy_lg_vol,
                    wa.sell_lg_vol,
                    wa.buy_elg_vol,
                    wa.sell_elg_vol,
                    wa.total_vol,
                    wa.net_mf_amount,
                    wa.active_days,
                    wa.expected_days,
                    wq.close_qfq,
                    CASE
                        WHEN wa.active_days < wa.expected_days * 0.6
                        THEN 1 ELSE 0
                    END AS _skip_dde
                FROM weekly_agg wa
                JOIN dwd_weekly_quote wq
                    ON wq.ts_code = wa.ts_code AND wq.trade_date = wa.week_end
                WHERE wa.expected_days > 0
                {(" AND wa.week_end >= ?" if start_date else "")}
                ORDER BY wa.ts_code, wa.week_end
            """, (chunk + [start_date]) if start_date else chunk).df()
            if big.empty:
                continue
            for ts_code, g in big.groupby("ts_code", sort=False):
                groups[ts_code] = g.drop(columns=["ts_code"]).reset_index(drop=True)
        return groups

    def _compute_indicators(self, df: pd.DataFrame,
                            ema_seeds: dict = None) -> pd.DataFrame:
        """Compute DDX, DDX2, divergence, trend, alerts, and turning points."""
        buy_lg = df["buy_lg_vol"].values.astype(float)
        sell_lg = df["sell_lg_vol"].values.astype(float)
        buy_elg = df["buy_elg_vol"].values.astype(float)
        sell_elg = df["sell_elg_vol"].values.astype(float)
        total = df["total_vol"].values.astype(float)

        # 检查 _skip_dde 标记（周线 moneyflow 覆盖不足的周）
        skip_mask = df.get("_skip_dde", pd.Series([False] * len(df)))

        # DDX = (buy_lg + buy_elg - sell_lg - sell_elg) / total_vol
        net_big = buy_lg + buy_elg - sell_lg - sell_elg
        ddx = np.full(len(df), np.nan)
        for i in range(len(df)):
            if not skip_mask.iloc[i] and total[i] != 0:
                ddx[i] = net_big[i] / total[i]
        df["ddx"] = ddx

        # DDX2 = EMA(DDX, 5)
        sddx2 = ema_seeds.get("ddx2") if ema_seeds else None
        df["ddx2"] = ema(ddx, 5, seed=sddx2)

        # net_mf_amount: direct copy
        df["net_mf_amount"] = df["net_mf_amount"].values.astype(float)

        # Trend: exponentially weighted linear regression on DDX2 (8-bar, decay=0.20)
        trend_window = 8
        df["trend"] = self._compute_trend(df["ddx2"].values.astype(float), window=trend_window)
        df["trend_strength"] = self._compute_trend_strength(df["ddx2"].values.astype(float), window=trend_window)

        # Divergence: same logic as MACD but using DDX2 instead of DIF
        df["divergence"] = self._compute_divergence(df)

        # Alerts: upturn/downturn reverse using DDX
        df["alert"] = self._compute_alerts(df)

        return df

    def _compute_trend(self, ddx2: np.ndarray, window: int = 8) -> list:
        """DDX2 trend via exponentially weighted linear regression.

        Weighted regression (decay=0.20) makes recent bars ~3x more influential.
        - up: weighted_slope > 0.0001
        - down: weighted_slope < -0.0001
        - flat: otherwise
        """
        # Vectorized fixed-window weighted regression (decay=0.20), equivalent
        # to the legacy per-bar np.polyfit loop. See base.weighted_window_slopes.
        slopes = weighted_window_slopes(ddx2, window, 0.20)
        result = [None] * len(ddx2)
        for i in np.nonzero(np.isfinite(slopes))[0]:
            s = slopes[i]
            if s > 0.0001:
                result[i] = "up"
            elif s < -0.0001:
                result[i] = "down"
            else:
                result[i] = "flat"
        return result

    def _compute_trend_strength(self, ddx2: np.ndarray, window: int = 8) -> np.ndarray:
        """DDX2 trend strength via exponentially weighted linear regression.

        Formula: weighted_slope / mean(|DDX2_segment|), unitless signed value.
        Positive = bullish capital flow strength, negative = bearish.
        Weighted regression (decay=0.20) gives recent bars ~3x more influence
        than bars 8 days ago.

        Returns NaN where window < window or all DDX2 in segment are zero.
        """
        # Vectorized: weighted slope / mean(|DDX2|) over each full window.
        # mean_abs == 0 → skip (NaN), matching legacy (no 0.0 fallback here).
        slopes = weighted_window_slopes(ddx2, window, 0.20)
        scale = sliding_window_mean_abs(ddx2, window)
        result = np.full(len(ddx2), np.nan)
        mask = (~np.isnan(scale)) & (scale != 0) & np.isfinite(slopes)
        result[mask] = slopes[mask] / scale[mask]
        return result

    def _compute_divergence(self, df: pd.DataFrame) -> list:
        """Top/bottom divergence using DDX vs close (vectorized + spike filter)."""
        return compute_price_signal_divergence(
            df["close_qfq"].values, df["ddx"].values, window=60, dedup=5,
            require_finite_signal_window=True, spike_filter_top=True,
        )

    def _compute_alerts(self, df: pd.DataFrame) -> list:
        """Upturn/downturn reverse + flat alerts using DDX2.

        - reverse: prev 2 consecutive rises/falls, then direction flips
        - flat: prev 2 consecutive rises/falls, then |change|/|prev| <= 2%
        Reverse takes priority over flat.
        """
        result = [None] * len(df)
        ddx2 = df["ddx2"].values
        for i in range(3, len(df)):
            prev = ddx2[i - 3:i + 1]
            if any(pd.isna(x) for x in prev):
                continue

            prev_up = prev[2] > prev[1] and prev[1] > prev[0]
            prev_down = prev[2] < prev[1] and prev[1] < prev[0]

            if prev_up:
                if ddx2[i] < ddx2[i - 1]:
                    result[i] = "upturn_reverse"
                elif ddx2[i - 1] != 0 and abs(ddx2[i] - ddx2[i - 1]) / abs(ddx2[i - 1]) <= 0.02:
                    result[i] = "upturn_flat"
            elif prev_down:
                if ddx2[i] > ddx2[i - 1]:
                    result[i] = "downturn_reverse"
                elif ddx2[i - 1] != 0 and abs(ddx2[i] - ddx2[i - 1]) / abs(ddx2[i - 1]) <= 0.02:
                    result[i] = "downturn_flat"

        return result

    def append_calculate(self, ts_code: str, df: pd.DataFrame, new_bars: list,
                         calc_date: str, state: dict) -> CalcResult:
        """APPEND mode: compute over full tail-window df, write only new_bars.

        ddx is purely causal (per-bar ratio); ddx2 = EMA(ddx, 5) uses seeds
        from DWS at the bar before df[0] for exact equivalence to FULL.
        Falls back to SMA warm-up when seeds are unavailable.
        """
        result = CalcResult()
        seeds = resolve_ema_seeds(
            self.con, self.dws_table, ts_code, df, self.freq,
            ("ddx2",), recalc_start=new_bars[0],
        )
        df = self._compute_indicators(df, ema_seeds=seeds)
        fp = compute_history_signature(df, self.SIGNATURE_COLS)
        if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                        write_start=new_bars[0], write_end=new_bars[-1]):
            result.calculated += 1
        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str,
                input_fingerprint: str = None,
                write_start: str = None, write_end: str = None):
        dws_cols = ["ts_code", "trade_date", "net_mf_amount", "ddx", "ddx2",
                    "trend", "trend_strength", "alert", "divergence",
                    "calc_date", "input_fingerprint", "spec_version"]
        float_cols = ["net_mf_amount", "ddx", "ddx2", "trend_strength"]
        return insert_dws_batch(self.con, self.dws_table, df, ts_code, calc_date,
                                dws_cols, float_cols,
                                input_fingerprint=input_fingerprint,
                                write_start=write_start, write_end=write_end)
