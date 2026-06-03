import numpy as np
import pandas as pd
from backend.etl.base import to_float_safe


class PricePositionCalculator:
    """Price position (relative strength) calculator.

    Computes price_position for 3 window sizes (60, 120, 250).
    price_position_N = (close - N_day_low) / (N_day_high - N_day_low) * 100

    This is a PURE PRICE feature — no dependency on any other DWS table.
    It serves as infrastructure for MACD divergence high-position checks,
    K-pattern trend context, and volume zone price-aware interpretation.
    Works for both daily and weekly frequencies.
    """

    WINDOWS = [60, 120, 250]

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws_table = f"dws_price_position_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str):
        """Calculate price_position for a batch of stocks. INSERT results into DWS table."""
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
            if df.empty or len(df) < 60:
                continue
            df = self._compute_positions(df)
            self._insert(ts_code, df, calc_date)

    def _compute_positions(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute price_position for all window sizes using rolling min/max."""
        c = df["close_qfq"].values.astype(float)

        for window in self.WINDOWS:
            col = f"price_position_{window}d"
            s = pd.Series(c)
            roll_min = s.rolling(window, min_periods=window).min()
            roll_max = s.rolling(window, min_periods=window).max()
            denom = roll_max - roll_min
            with np.errstate(divide='ignore', invalid='ignore'):
                df[col] = np.where(
                    denom.values > 0,
                    (c - roll_min.values) / denom.values * 100.0,
                    np.nan,
                )

        return df

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        """Batch insert all rows for one stock via DuckDB register."""
        dws_cols = ["ts_code", "trade_date",
                    "price_position_60d", "price_position_120d",
                    "price_position_250d", "calc_date"]
        data_cols = dws_cols[1:]
        for c in data_cols:
            if c not in df.columns:
                df[c] = None
        batch = df[data_cols].copy()
        batch["ts_code"] = ts_code
        for c in ["price_position_60d", "price_position_120d", "price_position_250d"]:
            batch[c] = batch[c].apply(to_float_safe)
        batch["calc_date"] = batch["calc_date"].astype(str)
        batch = batch[dws_cols]
        self.con.register("_batch", batch)
        cols_sql = ", ".join(dws_cols)
        self.con.execute(
            f"INSERT OR REPLACE INTO {self.dws_table} ({cols_sql}) "
            f"SELECT {cols_sql} FROM _batch"
        )
        self.con.unregister("_batch")
