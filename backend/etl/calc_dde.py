from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import logging
from backend.etl.base import ema, to_float_safe, linear_regression_slope


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

    def calculate(self, ts_codes: list[str], calc_date: str):
        """Calculate DDE indicators for a batch of stocks. INSERT results into DWS table."""
        for ts_code in ts_codes:
            logger.debug("%s: processing %s", self.__class__.__name__, ts_code)
            if self.freq == "daily":
                df = self._load_daily(ts_code)
            else:
                df = self._load_weekly(ts_code)
            if df.empty or len(df) < 10:
                continue
            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date)

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
        """Aggregate daily moneyflow to weekly granularity using week-end trade dates."""
        # Get week-end dates for this stock
        weeks = self.con.execute(f"""
            SELECT d.trade_date FROM {self.quote_table} d
            JOIN dim_date dd ON d.trade_date = dd.trade_date
            WHERE d.ts_code = ? AND dd.is_week_end = 1
            ORDER BY d.trade_date
        """, (ts_code,)).df()
        if weeks.empty:
            return weeks

        week_dates = weeks["trade_date"].tolist()

        # For each week ending date, sum moneyflow from the prior week's daily data
        rows = []
        for i, week_end in enumerate(week_dates):
            # Determine the start of this week's daily range
            if i == 0:
                week_start = week_end  # first week: just that day's range
            else:
                week_start = week_dates[i - 1]

            # Compute 7-day lookback for first week's expected_days query
            if i == 0:
                we_dt = datetime.strptime(week_end, "%Y%m%d")
                week_start_7d = (we_dt - timedelta(days=7)).strftime("%Y%m%d")
            else:
                week_start_7d = week_start

            if i == 0:
                agg = self.con.execute(f"""
                    SELECT
                        SUM(mf.buy_lg_vol) AS buy_lg_vol,
                        SUM(mf.sell_lg_vol) AS sell_lg_vol,
                        SUM(mf.buy_elg_vol) AS buy_elg_vol,
                        SUM(mf.sell_elg_vol) AS sell_elg_vol,
                        SUM(mf.total_vol) AS total_vol,
                        SUM(mf.net_mf_amount) AS net_mf_amount,
                        COUNT(DISTINCT mf.trade_date) AS active_days
                    FROM {self.src_table} mf
                    JOIN dwd_daily_quote q ON mf.ts_code = q.ts_code AND mf.trade_date = q.trade_date
                    WHERE mf.ts_code = ?
                      AND mf.trade_date > ?
                      AND mf.trade_date <= ?
                      AND q.is_suspended = 0
                """, (ts_code, week_start_7d, week_end)).fetchone()
            else:
                agg = self.con.execute(f"""
                    SELECT
                        SUM(mf.buy_lg_vol) AS buy_lg_vol,
                        SUM(mf.sell_lg_vol) AS sell_lg_vol,
                        SUM(mf.buy_elg_vol) AS buy_elg_vol,
                        SUM(mf.sell_elg_vol) AS sell_elg_vol,
                        SUM(mf.total_vol) AS total_vol,
                        SUM(mf.net_mf_amount) AS net_mf_amount,
                        COUNT(DISTINCT mf.trade_date) AS active_days
                    FROM {self.src_table} mf
                    JOIN dwd_daily_quote q ON mf.ts_code = q.ts_code AND mf.trade_date = q.trade_date
                    WHERE mf.ts_code = ? AND mf.trade_date > ? AND mf.trade_date <= ?
                      AND q.is_suspended = 0
                """, (ts_code, week_start, week_end)).fetchone()

            # Also get the close price from weekly quote
            close_row = self.con.execute(f"""
                SELECT close_qfq FROM {self.quote_table}
                WHERE ts_code = ? AND trade_date = ?
            """, (ts_code, week_end)).fetchone()

            if close_row is None:
                continue

            # 查该周应有交易日数（考虑假期），对比 moneyflow 覆盖
            expected_days = self.con.execute("""
                SELECT COUNT(*) FROM dim_date
                WHERE trade_date > ? AND trade_date <= ? AND is_trade_day = 1
            """, (week_start_7d, week_end)).fetchone()[0]

            active_days = agg[6] if agg[6] else 0
            skip_dde = False
            if expected_days == 0:
                continue  # 无交易日（如黄金周），不应存在周线
            if active_days < expected_days * 0.6:
                skip_dde = True  # moneyflow 覆盖不足 60%

            rows.append({
                "trade_date": week_end,
                "buy_lg_vol": agg[0] if agg[0] else 0,
                "sell_lg_vol": agg[1] if agg[1] else 0,
                "buy_elg_vol": agg[2] if agg[2] else 0,
                "sell_elg_vol": agg[3] if agg[3] else 0,
                "total_vol": agg[4] if agg[4] else 0,
                "net_mf_amount": agg[5] if agg[5] else 0,
                "close_qfq": close_row[0],
                "_skip_dde": skip_dde,
            })

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

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

        # Trend: linear regression slope on DDX2 (8-bar window for both daily and weekly)
        trend_window = 8
        df["trend"] = self._compute_trend(df["ddx2"].values.astype(float), window=trend_window)

        # Divergence: same logic as MACD but using DDX2 instead of DIF
        df["divergence"] = self._compute_divergence(df)

        # Alerts: upturn/downturn reverse using DDX
        df["alert"] = self._compute_alerts(df)

        return df

    def _compute_trend(self, ddx2: np.ndarray, window: int = 8) -> list:
        """DDX2 trend via linear regression slope (same method as volume trend).
        - up: slope > 0.0001
        - down: slope < -0.0001
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
            slope = linear_regression_slope(valid, use_log=False)
            if slope > 0.0001:
                result[i] = "up"
            elif slope < -0.0001:
                result[i] = "down"
            else:
                result[i] = "flat"
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

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        """Batch insert all rows for one stock via DuckDB register."""
        dws_cols = ["ts_code", "trade_date", "net_mf_amount", "ddx", "ddx2",
                    "trend", "alert", "divergence", "calc_date"]
        data_cols = dws_cols[1:]
        for c in data_cols:
            if c not in df.columns:
                df[c] = None
        batch = df[data_cols].copy()
        batch["ts_code"] = ts_code
        for c in ["net_mf_amount", "ddx", "ddx2"]:
            batch[c] = batch[c].apply(to_float_safe)
        batch["calc_date"] = batch["calc_date"].astype(str)
        batch = batch[dws_cols]
        self.con.register("_batch", batch)
        cols_sql = ", ".join(dws_cols)
        self.con.execute(f"INSERT OR REPLACE INTO {self.dws_table} ({cols_sql}) SELECT {cols_sql} FROM _batch")
        self.con.unregister("_batch")
