import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from backend.etl.base import (
    ema, to_float_safe, insert_dws_batch, compute_fingerprint, check_dwd_unchanged,
    SkipReason, CalcResult,
)

logger = logging.getLogger(__name__)


class DDECalculator:
    """DDE (Data Display Estimate) indicator calculator.

    Computes DDX, DDX2 from moneyflow data, plus divergence, trend, and alerts.
    Divergence/trend/alert logic mirrors MACD but uses DDX/DDX2.
    Works for both daily and weekly frequencies.
    """

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

    def calculate(self, ts_codes: list[str], calc_date: str) -> CalcResult:
        result = CalcResult()
        for ts_code in ts_codes:
            if self.freq == "daily":
                df = self._load_daily(ts_code)
            else:
                df = self._load_weekly(ts_code)

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

            if check_dwd_unchanged(self.con, self.dws_table, ts_code, df):
                result.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                                "DWD fingerprint match")
                continue

            fp = compute_fingerprint(df)
            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date, input_fingerprint=fp)
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

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
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
        df["ddx2"] = ema(ddx, 5)

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
        result = [None] * len(ddx2)
        for i in range(len(ddx2)):
            if i < window - 1:
                continue
            segment = ddx2[i - window + 1:i + 1]
            valid = segment[~np.isnan(segment)]
            if len(valid) < window:
                continue
            n = len(valid)
            x = np.arange(n, dtype=float)
            weights = np.exp(x * 0.20)
            try:
                slope = float(np.polyfit(x, valid, 1, w=weights)[0])
            except (np.linalg.LinAlgError, ValueError, TypeError):
                continue
            if not np.isfinite(slope):
                continue
            if slope > 0.0001:
                result[i] = "up"
            elif slope < -0.0001:
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
        result = np.full(len(ddx2), np.nan)
        for i in range(window - 1, len(ddx2)):
            segment = ddx2[i - window + 1:i + 1]
            valid = segment[~np.isnan(segment)]
            if len(valid) < window:
                continue
            mean_abs = np.mean(np.abs(valid))
            if mean_abs == 0:
                continue
            n = len(valid)
            x = np.arange(n, dtype=float)
            weights = np.exp(x * 0.20)
            try:
                slope = float(np.polyfit(x, valid, 1, w=weights)[0])
            except (np.linalg.LinAlgError, ValueError, TypeError):
                continue
            if not np.isfinite(slope):
                continue
            result[i] = slope / mean_abs
        return result

    def _compute_divergence(self, df: pd.DataFrame) -> list:
        """Top/bottom divergence using DDX (raw) vs close over 60-day window.

        Confirmation day: DDX has clearly rolled from its 60d peak/valley,
        but price still near extreme. Dedup: no repeat within 5 bars.
        Single-bar DDX spikes are filtered by requiring at least 2 bars
        within ±2 days of the peak to reach >= 80% of the peak value.
        """
        result = [None] * len(df)
        w = 59  # 60-bar window: iloc[i-59 : i+1] = 60 elements
        for i in range(w, len(df)):
            window_close = df["close_qfq"].iloc[i - w : i + 1]
            window_ddx = df["ddx"].iloc[i - w : i + 1]

            if window_ddx.isna().any():
                continue

            c_hi = window_close.max()
            c_lo = window_close.min()
            d_hi = window_ddx.max()
            d_lo = window_ddx.min()
            cur_c = df["close_qfq"].iloc[i]
            cur_d = df["ddx"].iloc[i]

            if pd.isna(cur_c) or pd.isna(cur_d):
                continue

            # Top divergence: DDX peaked in past, has fallen from peak,
            #                price still near 60d high (within 2%).
            ddx_peak_iloc = np.argmax(window_ddx.values)
            ddx_peak_val = window_ddx.max()
            ddx_fallen = d_hi != 0 and cur_d < d_hi
            price_near_peak = cur_c >= c_hi * 0.98

            # 邻域确认：峰值不是孤立的单日尖刺
            neighbors = window_ddx.values[
                max(0, ddx_peak_iloc - 2):min(len(window_ddx), ddx_peak_iloc + 3)
            ]
            is_spike = (neighbors >= ddx_peak_val * 0.8).sum() < 2

            if ddx_peak_iloc < w and ddx_fallen and not is_spike and price_near_peak:
                recent = any(result[j] == "top_divergence" for j in range(max(0, i - 5), i))
                if not recent:
                    result[i] = "top_divergence"

            # Bottom divergence: DDX valley in past, recovered >10%,
            #                   price stopped falling (low >= 3 bars ago).
            ddx_valley_iloc = np.argmin(window_ddx.values)
            ddx_valley_val = window_ddx.min()
            ddx_recovered = d_lo != 0 and cur_d > d_lo
            # 回升确认：DDX 回升幅度 > 谷值绝对值的 10%
            ddx_recovery_pct = (cur_d - d_lo) / abs(d_lo) if d_lo != 0 else 0
            ddx_confirmed = ddx_recovery_pct > 0.1
            # 价格止跌确认：60日低点距今 >= 3 根 bar
            c_lo_iloc = np.argmin(window_close.values)
            price_stopped = (w - c_lo_iloc) >= 3
            price_near_bottom = cur_c <= c_lo * 1.02

            if (ddx_valley_iloc < w and ddx_recovered and ddx_confirmed
                    and price_stopped and price_near_bottom):
                recent = any(result[j] == "bottom_divergence" for j in range(max(0, i - 5), i))
                if not recent:
                    result[i] = "bottom_divergence"

        return result

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

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str,
                input_fingerprint: str = None):
        dws_cols = ["ts_code", "trade_date", "net_mf_amount", "ddx", "ddx2",
                    "trend", "trend_strength", "alert", "divergence",
                    "calc_date", "input_fingerprint", "spec_version"]
        float_cols = ["net_mf_amount", "ddx", "ddx2", "trend_strength"]
        insert_dws_batch(self.con, self.dws_table, df, ts_code, calc_date,
                         dws_cols, float_cols,
                         input_fingerprint=input_fingerprint)
