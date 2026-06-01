import numpy as np
import pandas as pd
from backend.etl.base import ema, to_float_safe


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
            WHERE m.ts_code = ? AND q.is_suspended = 0
            ORDER BY m.trade_date
        """, (ts_code,)).df()

    def _load_weekly(self, ts_code: str) -> pd.DataFrame:
        """Aggregate daily moneyflow to weekly granularity using weekly trade dates."""
        # Get weekly trade dates for this stock
        weeks = self.con.execute(f"""
            SELECT trade_date FROM {self.quote_table}
            WHERE ts_code = ? AND is_suspended = 0
            ORDER BY trade_date
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

            agg = self.con.execute(f"""
                SELECT
                    SUM(buy_lg_vol) AS buy_lg_vol,
                    SUM(sell_lg_vol) AS sell_lg_vol,
                    SUM(buy_elg_vol) AS buy_elg_vol,
                    SUM(sell_elg_vol) AS sell_elg_vol,
                    SUM(total_vol) AS total_vol,
                    SUM(net_mf_amount) AS net_mf_amount
                FROM {self.src_table}
                WHERE ts_code = ? AND trade_date > ? AND trade_date <= ?
            """, (ts_code, week_start, week_end)).fetchone()

            # Also get the close price from weekly quote
            close_row = self.con.execute(f"""
                SELECT close_qfq FROM {self.quote_table}
                WHERE ts_code = ? AND trade_date = ?
            """, (ts_code, week_end)).fetchone()

            if close_row is None:
                continue

            rows.append({
                "trade_date": week_end,
                "buy_lg_vol": agg[0] if agg[0] else 0,
                "sell_lg_vol": agg[1] if agg[1] else 0,
                "buy_elg_vol": agg[2] if agg[2] else 0,
                "sell_elg_vol": agg[3] if agg[3] else 0,
                "total_vol": agg[4] if agg[4] else 0,
                "net_mf_amount": agg[5] if agg[5] else 0,
                "close_qfq": close_row[0],
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

        # DDX = (buy_lg + buy_elg - sell_lg - sell_elg) / total_vol
        net_big = buy_lg + buy_elg - sell_lg - sell_elg
        ddx = np.full(len(df), np.nan)
        for i in range(len(df)):
            if total[i] != 0:
                ddx[i] = net_big[i] / total[i]
        df["ddx"] = ddx

        # DDX2 = EMA(DDX, 5)
        df["ddx2"] = ema(ddx, 5)

        # net_mf_amount: direct copy
        df["net_mf_amount"] = df["net_mf_amount"].values.astype(float)

        # Trend: 3 consecutive days same direction in DDX
        df["trend"] = self._compute_trend(ddx)

        # Divergence: same logic as MACD but using DDX2 instead of DIF
        df["divergence"] = self._compute_divergence(df)

        # Alerts: upturn/downturn reverse using DDX
        df["alert"] = self._compute_alerts(df)

        return df

    def _compute_trend(self, ddx: np.ndarray) -> list:
        """DDX 3 consecutive days same direction -> trend."""
        result = [None] * len(ddx)
        for i in range(3, len(ddx)):
            b = ddx[i - 3:i + 1]
            if any(pd.isna(x) for x in b):
                continue
            if all(b[j] > b[j - 1] for j in range(1, 4)):
                result[i] = "up"
            elif all(b[j] < b[j - 1] for j in range(1, 4)):
                result[i] = "down"
            else:
                result[i] = "flat"
        return result

    def _compute_divergence(self, df: pd.DataFrame) -> list:
        """Top/bottom divergence using DDX2 vs close over 60-day window."""
        result = [None] * len(df)
        w = 60
        for i in range(w, len(df)):
            window_close = df["close_qfq"].iloc[i - w:i + 1]
            window_ddx2 = df["ddx2"].iloc[i - w:i + 1]

            # Skip if window has NaN in ddx2
            if window_ddx2.isna().any():
                continue

            c_hi = window_close.max()
            c_lo = window_close.min()
            d_hi = window_ddx2.max()
            d_lo = window_ddx2.min()
            cur_c = df["close_qfq"].iloc[i]
            cur_d = df["ddx2"].iloc[i]

            if pd.isna(cur_d):
                continue

            # Top divergence: price at 60d high but DDX2 below 60d peak
            if cur_c >= c_hi and cur_d < d_hi:
                ddx2_peak_idx = window_ddx2.idxmax()
                if ddx2_peak_idx < df.index[i]:
                    result[i] = "top_divergence"

            # Bottom divergence: price at 60d low but DDX2 above 60d valley
            if cur_c <= c_lo and cur_d > d_lo:
                ddx2_valley_idx = window_ddx2.idxmin()
                if ddx2_valley_idx < df.index[i]:
                    result[i] = "bottom_divergence"

        return result

    def _compute_alerts(self, df: pd.DataFrame) -> list:
        """Upturn/downturn reverse alerts using DDX."""
        result = [None] * len(df)
        ddx = df["ddx"].values
        for i in range(4, len(df)):
            # Check if previous 3 days were trending up
            prev = ddx[i - 4:i]
            if any(pd.isna(x) for x in prev):
                continue
            prev_up = all(prev[j + 1] > prev[j] for j in range(3))
            prev_down = all(prev[j + 1] < prev[j] for j in range(3))
            if prev_up and ddx[i] < ddx[i - 1]:
                result[i] = "upturn_reverse"
            elif prev_down and ddx[i] > ddx[i - 1]:
                result[i] = "downturn_reverse"
        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        for _, row in df.iterrows():
            self.con.execute(
                f"""INSERT OR REPLACE INTO {self.dws_table}
                (ts_code, trade_date, net_mf_amount, ddx, ddx2,
                 trend, alert, divergence, calc_date)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    ts_code,
                    row["trade_date"],
                    to_float_safe(row.get("net_mf_amount")),
                    to_float_safe(row.get("ddx")),
                    to_float_safe(row.get("ddx2")),
                    row.get("trend"),
                    row.get("alert"),
                    row.get("divergence"),
                    calc_date,
                ),
            )
