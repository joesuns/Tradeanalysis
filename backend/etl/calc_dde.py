import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

import numpy as np
import pandas as pd
from backend.etl.base import (
    ema, to_float_safe, weighted_window_slopes, sliding_window_mean_abs,
    insert_dws_batch, compute_input_fingerprint, check_dwd_unchanged,
    load_latest_fingerprints, load_latest_spec_versions, resolve_ema_seeds,
    compute_history_signature,
    SkipReason, CalcResult,
)
from backend.etl.b4_alerts import compute_ddx2_slope_alerts
from backend.etl.divergence_structure import compute_dde_structure_divergence
from backend.etl.recalc_spec import RecalcSpec

logger = logging.getLogger(__name__)

# 123 project alignment for B4 short_dde_trend (analyze_moneyflow_trend_optimized)
DDE_MONEYFLOW_REGRESSION_DAILY = 5
DDE_MONEYFLOW_REGRESSION_WEEKLY = 4
DDE_DDX3_WINDOW = 10
DDE_DDX1_EMA_SPAN = 60
DDE_COMPENSATION_FACTOR = 60.1953
# 123 main_batch_analyzer extends moneyflow/circ history for weekly DDE EMA(60)
DDE_WEEKLY_TREND_HISTORY_DAYS = 900
# TA-native derived signals (not 123 B4 hard-gate parity for alert)
DDE_ALERT_WINDOW = 2
DDE_TREND_STRENGTH_WINDOW = 5

# Shared weekly SQL fragments (_load_weekly + _load_weekly_batch).
_WEEK_START_FALLBACK_SQL = """
strftime(
    CAST(
        SUBSTR({date_col}, 1, 4) || '-' ||
        SUBSTR({date_col}, 5, 2) || '-' ||
        SUBSTR({date_col}, 7, 2)
        AS DATE
    ) - INTERVAL 7 DAY,
    '%Y%m%d'
)"""


def _week_start_expr(date_col: str, partition_by: str, order_col: str) -> str:
    fallback = _WEEK_START_FALLBACK_SQL.format(date_col=date_col)
    return (
        f"COALESCE(LAG({date_col}) OVER (PARTITION BY {partition_by} "
        f"ORDER BY {order_col}), {fallback})"
    )


_WEEKLY_EXPECTED_DAYS_SQL = """
    (SELECT COUNT(*) FROM dim_date dd_t
     WHERE dd_t.trade_date > wr.week_start
       AND dd_t.trade_date <= wr.week_end
       AND dd_t.is_trade_day = 1)"""


class DDECalculator:
    """DDE (Data Display Estimate) indicator calculator.

    Computes DDX, DDX2 from moneyflow data, plus divergence, trend, and alerts.
    Divergence uses structure pipeline; alert uses TA-native short DDX2 inflection.
    Works for both daily and weekly frequencies.
    """

    # v3: alert 2-bar + trend_strength 5-bar (TA-native; bump invalidates fingerprint skip).
    SPEC_VERSION = "v3"

    RECALC_SPEC_DAILY = RecalcSpec(lookback=250, seed=5, event_tail=10, min_rows=10)
    RECALC_SPEC_WEEKLY = RecalcSpec(lookback=250, seed=5, event_tail=10, min_rows=2)

    SIGNATURE_COLS = [
        "buy_lg_vol", "sell_lg_vol", "buy_elg_vol", "sell_elg_vol",
        "total_vol", "net_mf_amount", "net_amount_dc", "circ_mv", "close_qfq",
    ]

    DWS_COLS = [
        "ts_code", "trade_date", "net_mf_amount", "ddx", "ddx2",
        "trend", "trend_strength", "alert", "divergence",
        "calc_date", "input_fingerprint", "spec_version",
    ]
    FLOAT_COLS = ["net_mf_amount", "ddx", "ddx2", "trend_strength"]

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
        latest_specs = load_latest_spec_versions(self.con, self.dws_table, ts_codes)
        load_start = None
        if recalc_start:
            from backend.etl.recalc_spec import resolve_load_start
            load_start = resolve_load_start(self.con, recalc_start, self.freq)
        daily_trend_groups: dict = {}
        if self.freq == "daily":
            groups = self._load_daily_batch(ts_codes, start_date=load_start)
        else:
            groups = self._load_weekly_batch(ts_codes, start_date=load_start)
            daily_trend_groups = self._load_daily_for_trend_batch(
                ts_codes, end_date=calc_date,
            )
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
            min_rows = 2 if self.freq == "weekly" else 10
            if len(df) < min_rows:
                logger.debug("DDE %s skip %s: %d rows < %d",
                             self.freq, ts_code, len(df), min_rows)
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min={min_rows}")
                continue

            if check_dwd_unchanged(
                self.con, self.dws_table, ts_code, df,
                latest_fps=latest_fps, recalc_start=recalc_start,
                expected_spec_version=self.SPEC_VERSION,
                latest_specs=latest_specs,
            ):
                result.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                                "DWD fingerprint match")
                continue

            fp = compute_input_fingerprint(df, recalc_start=recalc_start)
            ema_seeds = resolve_ema_seeds(
                self.con, self.dws_table, ts_code, df, self.freq,
                ("ddx2",), recalc_start,
            )
            daily_trend = daily_trend_groups.get(ts_code, empty) if self.freq == "weekly" else None
            df = self._compute_indicators(
                df, ema_seeds=ema_seeds, daily_for_trend=daily_trend,
                calc_date=calc_date,
            )
            if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                            write_start=recalc_start,
                            write_end=calc_date if recalc_start else None):
                result.calculated += 1
        return result

    @staticmethod
    def _weekly_trend_daily_start(calc_date: str) -> str:
        end = datetime.strptime(calc_date, "%Y%m%d")
        start = end - timedelta(days=DDE_WEEKLY_TREND_HISTORY_DAYS)
        return start.strftime("%Y%m%d")

    def _load_daily(self, ts_code: str) -> pd.DataFrame:
        """Load daily moneyflow + daily quote (always dwd_daily_quote)."""
        return self.con.execute("""
            SELECT m.trade_date, m.buy_lg_vol, m.sell_lg_vol,
                   m.buy_elg_vol, m.sell_elg_vol, m.total_vol,
                   m.net_mf_amount, m.net_amount_dc, q.close_qfq, q.total_mv, q.circ_mv
            FROM dwd_daily_moneyflow m
            JOIN dwd_daily_quote q
              ON m.ts_code = q.ts_code AND m.trade_date = q.trade_date
            WHERE m.ts_code = ? AND q.is_suspended = 0
            ORDER BY m.trade_date
        """, (ts_code,)).df()

    def _load_daily_for_trend(
        self, ts_code: str, start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Daily moneyflow + quote for weekly ``trend`` (LEFT JOIN — keep all MF days)."""
        clauses = ["m.ts_code = ?"]
        params: list = [ts_code]
        if start_date:
            clauses.append("m.trade_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("m.trade_date <= ?")
            params.append(end_date)
        where = " AND ".join(clauses)
        return self.con.execute(f"""
            SELECT m.trade_date, m.net_mf_amount, m.net_amount_dc,
                   q.close_qfq, q.total_mv, q.circ_mv
            FROM dwd_daily_moneyflow m
            LEFT JOIN dwd_daily_quote q
              ON m.ts_code = q.ts_code AND m.trade_date = q.trade_date
            WHERE {where}
            ORDER BY m.trade_date
        """, params).df()

    def _load_daily_batch(self, ts_codes: list[str], chunk_size: int = 400,
                          start_date: str = None,
                          tail_window: int = None) -> dict:
        """Batch version of _load_daily: one query per chunk → {ts_code: df}.

        Each frame is identical to _load_daily(ts_code) (same columns/order/filter).
        When ``tail_window`` is set, only the latest N bars per stock are returned
        (SQL ROW_NUMBER), matching ``df.tail(tail_window)`` on the full batch frame.
        """
        groups = {}
        for i in range(0, len(ts_codes), chunk_size):
            chunk = ts_codes[i:i + chunk_size]
            ph = ",".join(["?"] * len(chunk))
            date_filter = " AND m.trade_date >= ?" if start_date else ""
            inner = f"""
                SELECT m.ts_code, m.trade_date, m.buy_lg_vol, m.sell_lg_vol,
                       m.buy_elg_vol, m.sell_elg_vol, m.total_vol,
                       m.net_mf_amount, m.net_amount_dc, q.close_qfq, q.total_mv, q.circ_mv
                FROM dwd_daily_moneyflow m
                JOIN dwd_daily_quote q
                  ON m.ts_code = q.ts_code AND m.trade_date = q.trade_date
                WHERE m.ts_code IN ({ph}) AND q.is_suspended = 0{date_filter}
            """
            params = list(chunk)
            if start_date:
                params.append(start_date)
            if tail_window is not None:
                query = f"""
                    WITH base AS (
                        {inner}
                        ORDER BY m.ts_code, m.trade_date
                    ),
                    ranked AS (
                        SELECT *,
                               ROW_NUMBER() OVER (
                                   PARTITION BY ts_code ORDER BY trade_date DESC
                               ) AS rn
                        FROM base
                    )
                    SELECT ts_code, trade_date, buy_lg_vol, sell_lg_vol,
                           buy_elg_vol, sell_elg_vol, total_vol, net_mf_amount,
                           net_amount_dc, close_qfq, total_mv, circ_mv
                    FROM ranked
                    WHERE rn <= ?
                    ORDER BY ts_code, trade_date
                """
                params = params + [tail_window]
            else:
                query = inner + "\n                ORDER BY m.ts_code, m.trade_date"
            big = self.con.execute(query, params).df()
            if big.empty:
                continue
            for ts_code, g in big.groupby("ts_code", sort=False):
                groups[ts_code] = g.drop(columns=["ts_code"]).reset_index(drop=True)
        return groups

    def _load_daily_for_trend_batch(
        self, ts_codes: list[str], chunk_size: int = 400,
        start_date: Optional[str] = None, end_date: Optional[str] = None,
    ) -> dict:
        """Batch load for weekly B4 ``trend`` — all moneyflow days, optional circ gaps."""
        groups = {}
        for i in range(0, len(ts_codes), chunk_size):
            chunk = ts_codes[i:i + chunk_size]
            ph = ",".join(["?"] * len(chunk))
            clauses = [f"m.ts_code IN ({ph})"]
            params: list = list(chunk)
            if start_date:
                clauses.append("m.trade_date >= ?")
                params.append(start_date)
            if end_date:
                clauses.append("m.trade_date <= ?")
                params.append(end_date)
            where = " AND ".join(clauses)
            big = self.con.execute(f"""
                SELECT m.ts_code, m.trade_date, m.net_mf_amount, m.net_amount_dc,
                       q.close_qfq, q.total_mv, q.circ_mv
                FROM dwd_daily_moneyflow m
                LEFT JOIN dwd_daily_quote q
                  ON m.ts_code = q.ts_code AND m.trade_date = q.trade_date
                WHERE {where}
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
        week_start = _week_start_expr(
            "wq.trade_date", "wq.ts_code", "wq.trade_date",
        )
        return self.con.execute(f"""
            WITH week_ranges AS (
                SELECT
                    wq.ts_code,
                    wq.trade_date AS week_end,
                    {week_start} AS week_start
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
                    COALESCE(SUM(mf.net_amount_dc),0) AS net_amount_dc,
                    COUNT(DISTINCT mf.trade_date) AS active_days,
                    {_WEEKLY_EXPECTED_DAYS_SQL} AS expected_days
                FROM week_ranges wr
                JOIN {self.src_table} mf
                    ON wr.ts_code = mf.ts_code
                    AND mf.trade_date > wr.week_start
                    AND mf.trade_date <= wr.week_end
                JOIN dwd_daily_quote q
                    ON mf.ts_code = q.ts_code
                    AND mf.trade_date = q.trade_date
                    AND q.is_suspended = 0
                GROUP BY wr.week_end, wr.week_start
            )
            SELECT
                wa.week_end   AS trade_date,
                wa.buy_lg_vol,
                wa.sell_lg_vol,
                wa.buy_elg_vol,
                wa.sell_elg_vol,
                wa.total_vol,
                wa.net_mf_amount,
                wa.net_amount_dc,
                wa.active_days,
                wa.expected_days,
                wq.close_qfq,
                dq_mv.total_mv,
                dq_mv.circ_mv,
                CASE
                    WHEN wa.active_days < wa.expected_days * 0.6
                    THEN 1 ELSE 0
                END AS _skip_dde
            FROM weekly_agg wa
            JOIN dwd_weekly_quote wq
                ON wq.ts_code = ? AND wq.trade_date = wa.week_end
            LEFT JOIN dwd_daily_quote dq_mv
                ON dq_mv.ts_code = wq.ts_code AND dq_mv.trade_date = wq.trade_date
            WHERE wa.expected_days > 0
            ORDER BY wa.week_end
        """, (ts_code, ts_code)).df()

    def _load_weekly_batch(self, ts_codes: list[str], chunk_size: int = 400,
                           start_date: str = None,
                           tail_window: int = None) -> dict:
        """Batch version of _load_weekly: one query per chunk → {ts_code: df}.

        Carries ts_code through every CTE (PARTITION/GROUP BY already keyed by
        ts_code) so each per-stock frame is identical to _load_weekly(ts_code).
        When ``tail_window`` is set, only the latest N+1 week ranges are scanned
        before ROW_NUMBER trim (hot path for batch_load_dde_tails).
        """
        from backend.etl.progress import StageProgress

        groups = {}
        if not ts_codes:
            return groups

        n_chunks = (len(ts_codes) + chunk_size - 1) // chunk_size
        progress = None
        if n_chunks > 1:
            progress = StageProgress(
                "calc.dde_weekly", n_chunks, count_step=1, unit="chunks",
            )
            progress.log_start(stocks=len(ts_codes))

        week_start_full = _week_start_expr(
            "wq.trade_date", "wq.ts_code", "wq.trade_date",
        )
        week_start_tail = _week_start_expr(
            "rw.week_end", "rw.ts_code", "rw.week_end",
        )

        for i in range(0, len(ts_codes), chunk_size):
            chunk = ts_codes[i:i + chunk_size]
            ph = ",".join(["?"] * len(chunk))
            if tail_window is not None:
                week_ranges_cte = f"""
                    recent_weeks AS (
                        SELECT
                            wq.ts_code,
                            wq.trade_date AS week_end,
                            ROW_NUMBER() OVER (
                                PARTITION BY wq.ts_code ORDER BY wq.trade_date DESC
                            ) AS rn
                        FROM dwd_weekly_quote wq
                        JOIN dim_date dd ON wq.trade_date = dd.trade_date
                        WHERE wq.ts_code IN ({ph}) AND dd.is_week_end = 1
                    ),
                    week_ranges AS (
                        SELECT
                            rw.ts_code,
                            rw.week_end,
                            {week_start_tail} AS week_start
                        FROM recent_weeks rw
                        WHERE rw.rn <= ?
                    )"""
            else:
                week_ranges_cte = f"""
                    week_ranges AS (
                        SELECT
                            wq.ts_code,
                            wq.trade_date AS week_end,
                            {week_start_full} AS week_start
                        FROM dwd_weekly_quote wq
                        JOIN dim_date dd ON wq.trade_date = dd.trade_date
                        WHERE wq.ts_code IN ({ph}) AND dd.is_week_end = 1
                    )"""

            inner = f"""
                WITH {week_ranges_cte},
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
                        COALESCE(SUM(mf.net_amount_dc),0) AS net_amount_dc,
                        COUNT(DISTINCT mf.trade_date) AS active_days,
                        {_WEEKLY_EXPECTED_DAYS_SQL} AS expected_days
                    FROM week_ranges wr
                    JOIN {self.src_table} mf
                        ON wr.ts_code = mf.ts_code
                        AND mf.trade_date > wr.week_start
                        AND mf.trade_date <= wr.week_end
                    JOIN dwd_daily_quote q
                        ON mf.ts_code = q.ts_code
                        AND mf.trade_date = q.trade_date
                        AND q.is_suspended = 0
                    GROUP BY wr.ts_code, wr.week_end, wr.week_start
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
                    wa.net_amount_dc,
                    wa.active_days,
                    wa.expected_days,
                    wq.close_qfq,
                    dq_mv.total_mv,
                    dq_mv.circ_mv,
                    CASE
                        WHEN wa.active_days < wa.expected_days * 0.6
                        THEN 1 ELSE 0
                    END AS _skip_dde
                FROM weekly_agg wa
                JOIN dwd_weekly_quote wq
                    ON wq.ts_code = wa.ts_code AND wq.trade_date = wa.week_end
                LEFT JOIN dwd_daily_quote dq_mv
                    ON dq_mv.ts_code = wq.ts_code AND dq_mv.trade_date = wq.trade_date
                WHERE wa.expected_days > 0
                {(" AND wa.week_end >= ?" if start_date else "")}
            """
            params: list = list(chunk)
            if tail_window is not None:
                params.append(tail_window + 1)
                if start_date:
                    params.append(start_date)
                query = f"""
                    WITH base AS (
                        {inner}
                        ORDER BY wa.ts_code, wa.week_end
                    ),
                    ranked AS (
                        SELECT *,
                               ROW_NUMBER() OVER (
                                   PARTITION BY ts_code ORDER BY trade_date DESC
                               ) AS rn
                        FROM base
                    )
                    SELECT ts_code, trade_date, buy_lg_vol, sell_lg_vol,
                           buy_elg_vol, sell_elg_vol, total_vol, net_mf_amount,
                           net_amount_dc, active_days, expected_days, close_qfq,
                           total_mv, circ_mv, _skip_dde
                    FROM ranked
                    WHERE rn <= ?
                    ORDER BY ts_code, trade_date
                """
                params.append(tail_window)
            else:
                if start_date:
                    params.append(start_date)
                query = inner + "\n                ORDER BY wa.ts_code, wa.week_end"
            big = self.con.execute(query, params).df()
            if not big.empty:
                for ts_code, g in big.groupby("ts_code", sort=False):
                    groups[ts_code] = g.drop(columns=["ts_code"]).reset_index(drop=True)
            if progress is not None:
                progress.tick(1)

        if progress is not None:
            progress.log_done(stocks=len(ts_codes))
        return groups

    def _compute_indicators(self, df: pd.DataFrame,
                            ema_seeds: dict = None,
                            daily_for_trend: Optional[pd.DataFrame] = None,
                            calc_date: Optional[str] = None,
                            target_indices: Optional[set] = None) -> pd.DataFrame:
        """Compute DDX, DDX2, divergence, trend, alerts, and turning points."""
        df = self._compute_dde_core(df, ema_seeds=ema_seeds)
        return self._compute_dde_derived(
            df, daily_for_trend=daily_for_trend, calc_date=calc_date,
            target_indices=target_indices,
        )

    def _compute_dde_core(
        self, df: pd.DataFrame, ema_seeds: dict = None,
    ) -> pd.DataFrame:
        buy_lg = df["buy_lg_vol"].values.astype(float)
        sell_lg = df["sell_lg_vol"].values.astype(float)
        buy_elg = df["buy_elg_vol"].values.astype(float)
        sell_elg = df["sell_elg_vol"].values.astype(float)
        total = df["total_vol"].values.astype(float)

        skip_mask = df.get("_skip_dde", pd.Series([False] * len(df)))

        net_big = buy_lg + buy_elg - sell_lg - sell_elg
        ddx = np.full(len(df), np.nan)
        for i in range(len(df)):
            if not skip_mask.iloc[i] and total[i] != 0:
                ddx[i] = net_big[i] / total[i]
        df["ddx"] = ddx

        sddx2 = ema_seeds.get("ddx2") if ema_seeds else None
        df["ddx2"] = ema(ddx, 5, seed=sddx2)
        df["net_mf_amount"] = df["net_mf_amount"].values.astype(float)
        return df

    def _compute_dde_derived(
        self,
        df: pd.DataFrame,
        daily_for_trend: Optional[pd.DataFrame] = None,
        calc_date: Optional[str] = None,
        target_indices: Optional[set] = None,
    ) -> pd.DataFrame:
        skip_mask = df.get("_skip_dde", pd.Series([False] * len(df)))

        # Trend: 123-aligned moneyflow regression on DDX3 (B4 short_dde_trend)
        skip_arr = skip_mask.values if hasattr(skip_mask, "values") else None
        total_mv = df["total_mv"].values.astype(float) if "total_mv" in df.columns else None
        circ_mv = df["circ_mv"].values.astype(float) if "circ_mv" in df.columns else None
        net_dc = (
            df["net_amount_dc"].values.astype(float)
            if "net_amount_dc" in df.columns else None
        )
        if (
            getattr(self, "freq", "daily") == "weekly"
            and daily_for_trend is not None
            and not daily_for_trend.empty
        ):
            trend_daily = daily_for_trend
            if calc_date and not trend_daily.empty:
                trend_daily = trend_daily[
                    trend_daily["trade_date"] <= calc_date
                ].copy()
            df["trend"] = self._weekly_trend_from_daily(
                trend_daily,
                df["trade_date"].tolist(),
                skip_mask=skip_arr,
            )
        else:
            df["trend"] = self._compute_moneyflow_trend(
                df["net_mf_amount"].values.astype(float),
                total_mv,
                net_amount_dc=net_dc,
                circ_mv=circ_mv,
                skip_mask=skip_arr,
            )
        df["trend_strength"] = self._compute_trend_strength(
            df["ddx2"].values.astype(float),
            window=DDE_TREND_STRENGTH_WINDOW,
        )

        df["divergence"] = self._compute_divergence(df, target_indices=target_indices)
        df["alert"] = self._compute_alerts(df)
        return df

    def _compute_moneyflow_trend(
        self,
        net_mf_amount: np.ndarray,
        total_mv: Optional[np.ndarray],
        net_amount_dc: Optional[np.ndarray] = None,
        circ_mv: Optional[np.ndarray] = None,
        skip_mask: Optional[np.ndarray] = None,
    ) -> list:
        """123 ``analyze_moneyflow_trend_optimized`` direction (up/down/flat).

        Prefers moneyflow_dc net_amount + circ_mv (123 path); falls back to
        moneyflow net_mf_amount + total_mv when dc/circ missing (pre-2023-09).
        DDX1=EMA(DDX,60)*compensation; DDX3=SMA(DDX1,10); polyfit on DDX3.
        """
        n = len(net_mf_amount)
        result: list = [None] * n
        if total_mv is None:
            return result
        reg_w = (
            DDE_MONEYFLOW_REGRESSION_WEEKLY
            if self.freq == "weekly"
            else DDE_MONEYFLOW_REGRESSION_DAILY
        )
        net = pd.Series(net_mf_amount.astype(float))
        if net_amount_dc is not None:
            dc = pd.Series(net_amount_dc.astype(float))
            net = dc.where(dc.notna(), net)
        mv = pd.Series(total_mv.astype(float))
        if circ_mv is not None:
            c = pd.Series(circ_mv.astype(float))
            mv = c.where(c.notna(), mv)
        ddx = (net / mv * 100.0).where(mv > 0)
        ddx1 = ddx.ewm(span=DDE_DDX1_EMA_SPAN, adjust=False).mean() * DDE_COMPENSATION_FACTOR
        ddx3 = ddx1.rolling(DDE_DDX3_WINDOW).mean()
        # Vectorized OLS slopes: DDX3 primary, DDX fallback (handles NaN segments)
        ddx3_slopes = weighted_window_slopes(
            ddx3.values.astype(float), reg_w, decay=0.0,
        )
        ddx_slopes = weighted_window_slopes(
            ddx.values.astype(float), reg_w, decay=0.0,
        )
        slopes = np.where(np.isnan(ddx3_slopes), ddx_slopes, ddx3_slopes)

        for i in range(n):
            if skip_mask is not None and skip_mask[i]:
                continue
            if i < reg_w - 1:
                continue
            s = slopes[i]
            if not np.isfinite(s):
                continue
            if s > 0:
                result[i] = "up"
            elif s < 0:
                result[i] = "down"
            else:
                result[i] = "flat"
        return result

    def _build_123_weekly_dde_series(
        self, daily_df: pd.DataFrame,
    ) -> Tuple[pd.Series, pd.Series]:
        """DDX + DDX3 on calendar weeks — mirrors 123 ``calc_dde_weekly`` pipeline."""
        empty = pd.Series(dtype=float)
        if daily_df is None or daily_df.empty:
            return empty, empty
        d = daily_df.copy()
        d["trade_date"] = pd.to_datetime(d["trade_date"])
        d = d.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
        d = d.set_index("trade_date")

        net = pd.Series(np.nan, index=d.index, dtype=float)
        if "net_amount_dc" in d.columns:
            net = d["net_amount_dc"].astype(float)
        # 123 weekly path uses moneyflow net_amount (dc) only — no net_mf fallback.
        circ = pd.Series(np.nan, index=d.index, dtype=float)
        if "circ_mv" in d.columns:
            circ = d["circ_mv"].astype(float)
        # 123 weekly DDE uses circ_mv only — do not fall back to total_mv here.

        if net.dropna().empty or circ.dropna().empty:
            return empty, empty

        # Resample net/circ independently then inner-merge (not daily inner-join first).
        wnet = net.resample("W").sum().dropna()
        wcirc = circ.resample("W").last().dropna()
        wmf = wnet.reset_index()
        wmf.columns = ["trade_date", "net_amount"]
        wcirc_df = wcirc.reset_index()
        wcirc_df.columns = ["trade_date", "circ_mv"]
        wmf = wmf.drop_duplicates(subset=["trade_date"], keep="last")
        wcirc_df = wcirc_df.drop_duplicates(subset=["trade_date"], keep="last")
        merged = pd.merge(wmf, wcirc_df, on="trade_date", how="inner")
        merged = merged.sort_values("trade_date")
        merged = merged[merged["circ_mv"] > 0]
        if merged.empty:
            return empty, empty
        merged = merged.set_index("trade_date")
        ddx = merged["net_amount"] / merged["circ_mv"] * 100.0
        ddx1 = ddx.ewm(span=DDE_DDX1_EMA_SPAN, adjust=False).mean() * DDE_COMPENSATION_FACTOR
        ddx3 = ddx1.rolling(DDE_DDX3_WINDOW).mean()
        return ddx, ddx3

    def _resample_w_index_for_week_end(
        self, week_end: pd.Timestamp, labels: pd.DatetimeIndex,
    ) -> Optional[int]:
        """Map dim_date week_end to pandas W bucket index (label = period Sunday)."""
        if labels.empty:
            return None
        idx = int(labels.searchsorted(week_end, side="left"))
        if idx >= len(labels):
            idx = len(labels) - 1
        if labels[idx] < week_end and idx + 1 < len(labels):
            idx += 1
        return idx

    def _weekly_trend_from_daily(
        self,
        daily_df: pd.DataFrame,
        week_end_dates: list,
        skip_mask: Optional[np.ndarray] = None,
    ) -> list:
        """Weekly ``trend`` aligned with 123 ``analyze_weekly_dde_trend`` (resample W)."""
        reg_w = DDE_MONEYFLOW_REGRESSION_WEEKLY
        ddx, ddx3 = self._build_123_weekly_dde_series(daily_df)
        n = len(week_end_dates)
        result: list = [None] * n
        if ddx3.empty:
            return result
        labels = ddx3.index
        last_idx = n - 1
        latest_pos = len(ddx3) - 1
        for i in range(n):
            if skip_mask is not None and skip_mask[i]:
                continue
            # Latest week_end row: 123 batch uses iloc[-1] on full resample-W series
            if i == last_idx:
                pos = latest_pos
            else:
                pos = self._resample_w_index_for_week_end(
                    pd.to_datetime(week_end_dates[i]), labels,
                )
            if pos is None or pos < reg_w - 1:
                continue
            use_ddx3 = not ddx3.dropna().empty
            if use_ddx3:
                seg = ddx3.iloc[pos - reg_w + 1:pos + 1]
            else:
                seg = ddx.iloc[pos - reg_w + 1:pos + 1]
            if len(seg) < reg_w:
                continue
            x = np.arange(len(seg), dtype=float)
            try:
                slope = float(np.polyfit(x, seg.values.astype(float), 1)[0])
            except (np.linalg.LinAlgError, ValueError, TypeError):
                continue
            if not np.isfinite(slope):
                result[i] = "flat"
                continue
            if slope > 0:
                result[i] = "up"
            elif slope < 0:
                result[i] = "down"
            else:
                result[i] = "flat"
        return result

    def _compute_ddx2_trend(self, ddx2: np.ndarray, window: int = 8) -> list:
        """Legacy DDX2 weighted-regression trend (used by tests / trend_strength oracle)."""
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

    def _compute_trend_strength(
        self, ddx2: np.ndarray, window: int = DDE_TREND_STRENGTH_WINDOW,
    ) -> np.ndarray:
        """DDX2 trend strength via exponentially weighted linear regression.

        Formula: weighted_slope / mean(|DDX2_segment|), unitless signed value.
        Positive = bullish capital flow strength, negative = bearish.
        Weighted regression (decay=0.20) gives recent bars ~3x more influence
        than older bars in the 5-bar window.

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

    def _compute_divergence(
        self, df: pd.DataFrame, target_indices: Optional[set] = None,
    ) -> list:
        """Top/bottom divergence via DDX/DDX2 structure (TG day annotation)."""
        return compute_dde_structure_divergence(
            df["close_qfq"].values,
            df["ddx"].values,
            df["ddx2"].values,
            dedup=10,
            spike_filter_top=True,
            require_finite=True,
            target_indices=target_indices,
        )

    def _compute_alerts(self, df: pd.DataFrame) -> list:
        """TA-native DDX2 adjacent-window slope inflection (daily + weekly)."""
        return compute_ddx2_slope_alerts(
            df["ddx2"].values, window=DDE_ALERT_WINDOW, eps=0.0,
        )

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
        daily_trend = None
        if self.freq == "weekly":
            daily_trend = self._load_daily_for_trend(
                ts_code, end_date=calc_date,
            )
        df = self._compute_indicators(
            df, ema_seeds=seeds, daily_for_trend=daily_trend,
            calc_date=calc_date,
        )
        fp = compute_history_signature(df, self.SIGNATURE_COLS)
        if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                        write_start=new_bars[0], write_end=new_bars[-1]):
            result.calculated += 1
        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str,
                input_fingerprint: str = None,
                write_start: str = None, write_end: str = None):
        return insert_dws_batch(
            self.con, self.dws_table, df, ts_code, calc_date,
            self.DWS_COLS, self.FLOAT_COLS,
            spec_version=self.SPEC_VERSION,
            input_fingerprint=input_fingerprint,
            write_start=write_start, write_end=write_end,
        )
