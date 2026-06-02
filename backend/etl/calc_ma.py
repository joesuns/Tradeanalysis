import numpy as np
import pandas as pd
from backend.etl.base import sma, to_float_safe, linear_regression_slope


def _compute_slope_pct(series: np.ndarray, window: int = 5) -> np.ndarray:
    """5-bar linear regression slope, normalized as %/day of current value.

    Replaces the old diff(3)/shift(3)*100 formula. The normalization by
    current MA value makes slopes comparable across stocks of different prices.
    """
    result = np.full(len(series), np.nan)
    for i in range(window - 1, len(series)):
        segment = series[i - window + 1:i + 1]
        valid = segment[~np.isnan(segment)]
        if len(valid) < window or series[i] == 0:
            continue
        raw_slope = linear_regression_slope(valid, use_log=False)
        result[i] = raw_slope / series[i] * 100.0
    return result



class MACalculator:
    """Moving Average indicator calculator. Computes MA5, MA10, bias, slope,
    alignment (9-value classification), and turning points (golden/dead cross).
    Works for both daily and weekly frequencies."""

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws_table = f"dws_ma_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str):
        """Calculate MA indicators for a batch of stocks. INSERT results into DWS table."""
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
            if df.empty or len(df) < 11:
                continue
            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date)

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["close_qfq"].values.astype(float)
        df["ma_5"] = sma(c, 5)
        df["ma_10"] = sma(c, 10)
        # Bias (乖离率): (close - MA) / MA * 100
        df["bias_ma5"] = (c - df["ma_5"].values) / df["ma_5"].values * 100.0
        df["bias_ma10"] = (c - df["ma_10"].values) / df["ma_10"].values * 100.0
        # Slope: 5-bar linear regression normalized as %/day
        df["ma5_slope"] = _compute_slope_pct(df["ma_5"].values)
        df["ma10_slope"] = _compute_slope_pct(df["ma_10"].values)
        df["alignment"] = self._compute_alignment(df)
        df["turning_point"] = self._compute_turning_points(df)
        return df

    def _compute_alignment(self, df: pd.DataFrame) -> list:
        """10-value alignment classification based on MA5/MA10 relative position
        and dual-slope direction (threshold +/- 0.08%/day via 5-bar regression).

        Tangle requires BOTH: gap < 3% of MA10 AND >= 2 crosses in last 10 days.
        """
        result = [None] * len(df)
        ma5 = df["ma_5"].values
        ma10 = df["ma_10"].values
        s5 = df["ma5_slope"].values
        s10 = df["ma10_slope"].values

        # Pre-compute crosses for tangle cross-count
        cross_marker = np.zeros(len(df), dtype=int)
        for i in range(1, len(df)):
            if pd.isna(ma5[i]) or pd.isna(ma10[i]) or pd.isna(ma5[i - 1]) or pd.isna(ma10[i - 1]):
                continue
            if (ma5[i - 1] <= ma10[i - 1] and ma5[i] > ma10[i]) or \
               (ma5[i - 1] >= ma10[i - 1] and ma5[i] < ma10[i]):
                cross_marker[i] = 1

        for i in range(len(df)):
            if pd.isna(ma5[i]) or pd.isna(ma10[i]):
                continue
            if pd.isna(s5[i]) or pd.isna(s10[i]):
                continue

            # Tangle: gap < 3% AND >= 2 crosses in last 10 days
            gap = abs(ma5[i] - ma10[i]) / ma10[i] if ma10[i] != 0 else 0
            recent_crosses = cross_marker[max(0, i - 9):i + 1].sum()
            if gap < 0.0299 and recent_crosses >= 2:
                result[i] = "tangle"
                continue

            # Sideways: 双斜率均在平区（|s| < 0.08%）且非 tangle
            s5_flat = s5[i] > -0.08 and s5[i] < 0.08
            s10_flat = s10[i] > -0.08 and s10[i] < 0.08
            if s5_flat and s10_flat:
                result[i] = "sideways"
                continue

            above = ma5[i] > ma10[i]
            s5_up = s5[i] > 0.08
            s5_dn = s5[i] < -0.08
            s10_up = s10[i] > 0.08
            s10_dn = s10[i] < -0.08

            if above and s5_up and s10_up:
                result[i] = "bull_strong"
            elif above and s5_up and s10_dn:
                result[i] = "bull_building"
            elif above and s5_dn and s10_up:
                result[i] = "bull_weakening"
            elif above and s5_dn and s10_dn:
                result[i] = "bull_rolling"
            elif not above and s5_dn and s10_dn:
                result[i] = "bear_strong"
            elif not above and s5_dn and s10_up:
                result[i] = "bear_building"
            elif not above and s5_up and s10_dn:
                result[i] = "bear_weakening"
            elif not above and s5_up and s10_up:
                result[i] = "bear_rolling"

        return result

    def _compute_turning_points(self, df: pd.DataFrame) -> list:
        """Golden/dead cross + near_golden/near_dead on MA5/MA10 crossover.

        Near = estimated days to cross < 3 (small-gap direct or speed-based).
        Small gap: |MA5-MA10|/MA10 < 0.5% → direct near.
        Speed: 3-day gap regression slope < 0 AND gap/|slope| < 3.
        """
        result = [None] * len(df)
        ma5 = df["ma_5"].values
        ma10 = df["ma_10"].values

        for i in range(1, len(df)):
            if pd.isna(ma5[i - 1]) or pd.isna(ma10[i - 1]):
                continue
            if pd.isna(ma5[i]) or pd.isna(ma10[i]):
                continue
            if ma10[i] == 0:
                continue

            # Golden / dead cross
            if ma5[i - 1] <= ma10[i - 1] and ma5[i] > ma10[i]:
                result[i] = "golden_cross"
                continue
            elif ma5[i - 1] >= ma10[i - 1] and ma5[i] < ma10[i]:
                result[i] = "dead_cross"
                continue

            # Near golden / near dead: 预估交叉天数 < 3
            gap = abs(ma5[i] - ma10[i])
            gap_pct = gap / ma10[i]

            # 小间距直通: gap < 0.5% of MA10
            if gap_pct < 0.005:
                if ma5[i] < ma10[i]:
                    result[i] = "near_golden"
                else:
                    result[i] = "near_dead"
                continue

            # 速度判定: 3 日回归 est_days = gap / convergence_speed
            if i >= 2:
                gap_seq = np.array([
                    abs(ma5[i - 2] - ma10[i - 2]),
                    abs(ma5[i - 1] - ma10[i - 1]),
                    gap,
                ])
                gap_slope = linear_regression_slope(gap_seq, use_log=False)
                if gap_slope < 0:
                    conv_speed = -gap_slope
                    if conv_speed > 1e-9 and gap / conv_speed < 3:
                        if ma5[i] < ma10[i]:
                            result[i] = "near_golden"
                        else:
                            result[i] = "near_dead"

        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        """Batch insert all rows for one stock via DuckDB register."""
        dws_cols = ["ts_code", "trade_date", "ma_5", "ma_10", "bias_ma5",
                    "bias_ma10", "ma5_slope", "ma10_slope", "alignment",
                    "turning_point", "calc_date"]
        data_cols = dws_cols[1:]
        for c in data_cols:
            if c not in df.columns:
                df[c] = None
        batch = df[data_cols].copy()
        batch["ts_code"] = ts_code
        for c in ["ma_5", "ma_10", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope"]:
            batch[c] = batch[c].apply(to_float_safe)
        batch["calc_date"] = batch["calc_date"].astype(str)
        batch = batch[dws_cols]
        self.con.register("_batch", batch)
        cols_sql = ", ".join(dws_cols)
        self.con.execute(f"INSERT OR REPLACE INTO {self.dws_table} ({cols_sql}) SELECT {cols_sql} FROM _batch")
        self.con.unregister("_batch")
