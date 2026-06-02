import numpy as np
import pandas as pd
from backend.etl.base import sma, to_float_safe, linear_regression_slope


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
        # Slope: 3-period rate of change of the MA line
        df["ma5_slope"] = df["ma_5"].diff(3) / df["ma_5"].shift(3) * 100.0
        df["ma10_slope"] = df["ma_10"].diff(3) / df["ma_10"].shift(3) * 100.0
        df["alignment"] = self._compute_alignment(df)
        df["turning_point"] = self._compute_turning_points(df)
        return df

    def _compute_alignment(self, df: pd.DataFrame) -> list:
        """9-value alignment classification based on MA5/MA10 relative position
        and dual-slope direction (threshold +/- 0.3% over 3 days).

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

            # Sideways: 双斜率均在平区（|s| < 0.3%）且非 tangle
            s5_flat = s5[i] > -0.3 and s5[i] < 0.3
            s10_flat = s10[i] > -0.3 and s10[i] < 0.3
            if s5_flat and s10_flat:
                result[i] = "sideways"
                continue

            above = ma5[i] > ma10[i]
            s5_up = s5[i] > 0.3
            s5_dn = s5[i] < -0.3
            s10_up = s10[i] > 0.3
            s10_dn = s10[i] < -0.3

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

        Near golden: MA5 < MA10, gap narrowing, |MA5-MA10|/MA10 < 15%.
        Near dead:   MA5 > MA10, gap narrowing, |MA5-MA10|/MA10 < 15%.
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

            # Near golden / near dead
            gap = abs(ma5[i] - ma10[i])

            # 收敛判定：优先用 3 日回归（容忍日间波动），兜底 3 日绝对值缩小
            narrowing = False
            if i >= 2:
                gap_seq = np.array([
                    abs(ma5[i - 2] - ma10[i - 2]),
                    abs(ma5[i - 1] - ma10[i - 1]),
                    gap,
                ])
                gap_slope = linear_regression_slope(gap_seq, use_log=False)
                narrowing = gap_slope < 0 or gap < abs(ma5[i - 2] - ma10[i - 2])
            else:
                gap_prev = abs(ma5[i - 1] - ma10[i - 1])
                narrowing = gap < gap_prev

            if not narrowing:
                continue

            if gap / ma10[i] < 0.15:
                if ma5[i] < ma10[i]:
                    result[i] = "near_golden"
                else:
                    result[i] = "near_dead"

        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        for _, row in df.iterrows():
            self.con.execute(
                f"""INSERT OR REPLACE INTO {self.dws_table}
                (ts_code, trade_date, ma_5, ma_10, bias_ma5, bias_ma10,
                 ma5_slope, ma10_slope, alignment, turning_point, calc_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts_code,
                    row["trade_date"],
                    to_float_safe(row.get("ma_5")),
                    to_float_safe(row.get("ma_10")),
                    to_float_safe(row.get("bias_ma5")),
                    to_float_safe(row.get("bias_ma10")),
                    to_float_safe(row.get("ma5_slope")),
                    to_float_safe(row.get("ma10_slope")),
                    row.get("alignment"),
                    row.get("turning_point"),
                    calc_date,
                ),
            )
