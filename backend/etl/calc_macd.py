import numpy as np
import pandas as pd
from backend.etl.base import ema, to_float_safe, linear_regression_slope


class MACDCalculator:
    """MACD indicator calculator. Works for both daily and weekly frequencies."""

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws_table = f"dws_macd_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str):
        """Calculate MACD for a batch of stocks. INSERT results into DWS table."""
        for ts_code in ts_codes:
            if self.freq == "weekly":
                df = self.con.execute(f"""
                    SELECT d.trade_date, d.close_qfq FROM {self.src_table} d
                    JOIN dim_date dd ON d.trade_date = dd.trade_date
                    WHERE d.ts_code = ? AND dd.is_week_end = 1
                    ORDER BY d.trade_date
                """, (ts_code,)).df()
            else:
                df = self.con.execute(f"""
                    SELECT trade_date, close_qfq FROM {self.src_table}
                    WHERE ts_code = ? AND is_suspended = 0
                    ORDER BY trade_date
                """, (ts_code,)).df()
            if df.empty or len(df) < 27:
                continue
            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date)

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["close_qfq"].values.astype(float)
        df["ema_12"] = ema(c, 12)
        df["ema_26"] = ema(c, 26)
        df["dif"] = df["ema_12"] - df["ema_26"]
        df["dea"] = ema(df["dif"].values.astype(float), 9)
        df["macd_bar"] = 2.0 * (df["dif"] - df["dea"])
        df["zone"] = df["macd_bar"].apply(
            lambda x: "bull" if x > 0 else ("bear" if x < 0 else None)
        )
        window = 4  # 4-bar regression for both daily and weekly
        df["trend"] = self._compute_trend(df["macd_bar"].values, window=window)
        df["divergence"] = self._compute_divergence(df)
        df["turning_point"] = self._compute_turning_points(df)
        df["alert"] = self._compute_alerts(df)
        return df

    def _compute_trend(self, bar: np.ndarray, window: int = 20) -> list:
        """MACD bar trend via linear regression slope (same method as DDE/Volume).
        - up: slope > 0.0005
        - down: slope < -0.0005
        - flat: otherwise
        """
        result = [None] * len(bar)
        for i in range(len(bar)):
            if i < window - 1:
                continue
            segment = bar[i - window + 1:i + 1]
            valid = segment[~np.isnan(segment)]
            if len(valid) < window:
                continue
            slope = linear_regression_slope(valid, use_log=False)
            if slope > 0.2:
                result[i] = "up"
            elif slope < -0.2:
                result[i] = "down"
            else:
                result[i] = "flat"
        return result

    def _compute_divergence(self, df: pd.DataFrame) -> list:
        """Top/bottom divergence using 60-day window. Marked on confirmation day."""
        result = [None] * len(df)
        w = 60
        for i in range(w, len(df)):
            window_close = df["close_qfq"].iloc[i - w : i + 1]
            window_dif = df["dif"].iloc[i - w : i + 1]
            c_hi, c_lo = window_close.max(), window_close.min()
            d_hi, d_lo = window_dif.max(), window_dif.min()
            cur_c, cur_d = df["close_qfq"].iloc[i], df["dif"].iloc[i]
            # Top divergence: price at 60d high but DIF below 60d peak, and DIF peaked BEFORE today
            if cur_c >= c_hi and cur_d < d_hi:
                dif_peak_idx = window_dif.idxmax()
                if dif_peak_idx < df.index[i]:
                    result[i] = "top_divergence"
            # Bottom divergence
            if cur_c <= c_lo and cur_d > d_lo:
                dif_valley_idx = window_dif.idxmin()
                if dif_valley_idx < df.index[i]:
                    result[i] = "bottom_divergence"
        return result

    def _compute_turning_points(self, df: pd.DataFrame) -> list:
        """Golden cross (金叉) / Dead cross (死叉) / Near golden / Near dead."""
        result = [None] * len(df)
        bar = df["macd_bar"].values
        for i in range(1, len(df)):
            if pd.isna(bar[i - 1]) or pd.isna(bar[i]):
                continue
            if bar[i - 1] <= 0 and bar[i] > 0:
                result[i] = "golden_cross"
            elif bar[i - 1] >= 0 and bar[i] < 0:
                result[i] = "dead_cross"
        return result

    def _compute_alerts(self, df: pd.DataFrame) -> list:
        """Upturn/downturn reverse alerts based on most recent 2 comparisons."""
        result = [None] * len(df)
        bar = df["macd_bar"].values
        for i in range(3, len(df)):
            prev_up = all(bar[i - 1 - j] > bar[i - 2 - j] for j in range(2))
            prev_down = all(bar[i - 1 - j] < bar[i - 2 - j] for j in range(2))
            if prev_up and bar[i] < bar[i - 1]:
                result[i] = "upturn_reverse"
            elif prev_down and bar[i] > bar[i - 1]:
                result[i] = "downturn_reverse"
        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        for _, row in df.iterrows():
            self.con.execute(
                f"""INSERT OR REPLACE INTO {self.dws_table}
                (ts_code, trade_date, ema_12, ema_26, dif, dea, macd_bar,
                 divergence, zone, turning_point, alert, trend, calc_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts_code,
                    row["trade_date"],
                    to_float_safe(row.get("ema_12")),
                    to_float_safe(row.get("ema_26")),
                    to_float_safe(row.get("dif")),
                    to_float_safe(row.get("dea")),
                    to_float_safe(row.get("macd_bar")),
                    row.get("divergence"),
                    row.get("zone"),
                    row.get("turning_point"),
                    row.get("alert"),
                    row.get("trend"),
                    calc_date,
                ),
            )
