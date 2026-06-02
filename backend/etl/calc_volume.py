import numpy as np
import pandas as pd
from backend.etl.base import sma, linear_regression_slope, to_float_safe



class VolumeCalculator:
    """Volume indicator calculator.

    Computes MA5 volume, percentile rank, zone classification (explosive,
    low_volume, normal), and trend (expanding, shrinking, flat).
    Works for both daily and weekly frequencies.
    """

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws_table = f"dws_volume_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str):
        """Calculate volume indicators for a batch of stocks. INSERT results into DWS table."""
        for ts_code in ts_codes:
            if self.freq == "weekly":
                df = self.con.execute(f"""
                    SELECT d.trade_date, d.vol FROM {self.src_table} d
                    JOIN dim_date dd ON d.trade_date = dd.trade_date
                    WHERE d.ts_code = ? AND dd.is_week_end = 1
                    ORDER BY d.trade_date
                """, (ts_code,)).df()
            else:
                df = self.con.execute(f"""
                    SELECT trade_date, vol FROM {self.src_table}
                    WHERE ts_code = ? AND is_suspended = 0
                    ORDER BY trade_date
                """, (ts_code,)).df()
            if df.empty or len(df) < 5:
                continue
            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date)

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        v = df["vol"].values.astype(float)

        # MA5 volume
        df["ma_vol_5"] = sma(v, 5)

        # Percentile rank of MA5_vol within last 120 days
        df["pct_vol_rank"] = self._compute_pct_rank(df["ma_vol_5"].values, 120)

        # Zone: explosive / low_volume / normal
        df["zone"] = self._compute_zone(df)

        # Trend: linear regression slope on ln(raw_vol) over 10 days
        df["trend"] = self._compute_trend(df["vol"].values, 10)

        return df

    def _compute_pct_rank(self, ma_vol_5: np.ndarray, window: int) -> np.ndarray:
        """Percentile rank of current MA5_vol within the last `window` valid values."""
        n = len(ma_vol_5)
        result = np.full(n, np.nan)

        for i in range(window - 1, n):
            start = max(0, i - window + 1)
            window_vals = ma_vol_5[start:i + 1]
            valid = window_vals[~np.isnan(window_vals)]
            if len(valid) < 2:
                continue
            cur = ma_vol_5[i]
            if pd.isna(cur):
                continue
            # Percentile rank: fraction of values <= current (mid-rank for ties)
            rank = np.sum(valid <= cur) / len(valid) * 100.0
            result[i] = rank

        return result

    def _compute_zone(self, df: pd.DataFrame) -> list:
        """Classify volume zone based on percentile rank persistence.

        - explosive: P90 threshold (pct_vol_rank > 90 for 2 consecutive days)
          Exit: pct_vol_rank < 75 for 2 consecutive days
        - low_volume: P10 threshold (pct_vol_rank < 10 for 5 consecutive days)
          Exit: pct_vol_rank > 25 for 2 consecutive days
        - normal: everything else
        """
        n = len(df)
        rank = df["pct_vol_rank"].values
        result = [None] * n

        in_explosive = False
        in_low_volume = False

        for i in range(n):
            if pd.isna(rank[i]):
                continue

            if not in_explosive and not in_low_volume:
                # Check for explosive entry: > P90 for 2 consecutive days
                if i >= 1 and rank[i] > 90 and rank[i - 1] > 90:
                    in_explosive = True
                    in_low_volume = False
                # Check for low_volume entry: < P10 for 5 consecutive days
                elif i >= 4 and all(rank[i - j] < 10 for j in range(5)):
                    in_low_volume = True
                    in_explosive = False
                else:
                    result[i] = "normal"

            if in_explosive:
                # Check exit: < P75 for 2 consecutive days
                if i >= 1 and rank[i] < 75 and rank[i - 1] < 75:
                    in_explosive = False
                    result[i] = "normal"
                else:
                    result[i] = "explosive"

            if in_low_volume:
                # Check exit: > P25 for 2 consecutive days
                if i >= 1 and rank[i] > 25 and rank[i - 1] > 25:
                    in_low_volume = False
                    result[i] = "normal"
                else:
                    result[i] = "low_volume"

            # If both flags got cleared, re-evaluate for normal
            if not in_explosive and not in_low_volume and result[i] is None:
                result[i] = "normal"

        return result

    def _compute_trend(self, vol_series: np.ndarray, window: int) -> list:
        """Volume trend via linear regression slope on ln(raw volume).

        - expanding: slope > 0.008
        - shrinking: slope < -0.008
        - flat: otherwise
        """
        n = len(vol_series)
        result = [None] * n

        for i in range(n):
            if i < window - 1:
                continue
            segment = vol_series[i - window + 1:i + 1]
            # Need at least 5 valid (non-NaN, positive) values
            valid = segment[~np.isnan(segment)]
            valid_positive = valid[valid > 0]
            if len(valid_positive) < 5:
                continue

            slope = linear_regression_slope(valid_positive)
            if slope > 0.008:
                result[i] = "expanding"
            elif slope < -0.008:
                result[i] = "shrinking"
            else:
                result[i] = "flat"

        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        """Batch insert all rows for one stock via DuckDB register."""
        dws_cols = ["ts_code", "trade_date", "ma_vol_5", "pct_vol_rank",
                    "zone", "trend", "calc_date"]
        data_cols = dws_cols[1:]
        for c in data_cols:
            if c not in df.columns:
                df[c] = None
        batch = df[data_cols].copy()
        batch["ts_code"] = ts_code
        for c in ["ma_vol_5", "pct_vol_rank"]:
            batch[c] = batch[c].apply(to_float_safe)
        batch["calc_date"] = batch["calc_date"].astype(str)
        batch = batch[dws_cols]
        self.con.register("_batch", batch)
        cols_sql = ", ".join(dws_cols)
        self.con.execute(f"INSERT OR REPLACE INTO {self.dws_table} ({cols_sql}) SELECT {cols_sql} FROM _batch")
        self.con.unregister("_batch")
