import numpy as np
import pandas as pd
from backend.etl.base import sma


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
        # Slope: 3-day rate of change of the MA line
        df["ma5_slope"] = df["ma_5"].diff(3) / df["ma_5"].shift(3) * 100.0
        df["ma10_slope"] = df["ma_10"].diff(3) / df["ma_10"].shift(3) * 100.0
        df["alignment"] = self._compute_alignment(df)
        df["turning_point"] = self._compute_turning_points(df)
        return df

    def _compute_alignment(self, df: pd.DataFrame) -> list:
        """9-value alignment classification based on MA5/MA10 relative position
        and dual-slope direction (threshold +/- 0.3% over 3 days)."""
        result = [None] * len(df)
        ma5 = df["ma_5"].values
        ma10 = df["ma_10"].values
        s5 = df["ma5_slope"].values
        s10 = df["ma10_slope"].values

        for i in range(len(df)):
            if pd.isna(ma5[i]) or pd.isna(ma10[i]):
                continue
            if pd.isna(s5[i]) or pd.isna(s10[i]):
                continue

            # Tangle: near cross (gap < 3% of MA10)
            gap = abs(ma5[i] - ma10[i]) / ma10[i] if ma10[i] != 0 else 0
            if gap < 0.03:
                result[i] = "tangle"
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
        """Golden cross (金叉) / Dead cross (死叉) detection on MA5/MA10 crossover."""
        result = [None] * len(df)
        ma5 = df["ma_5"].values
        ma10 = df["ma_10"].values

        for i in range(1, len(df)):
            if pd.isna(ma5[i - 1]) or pd.isna(ma10[i - 1]):
                continue
            if pd.isna(ma5[i]) or pd.isna(ma10[i]):
                continue
            if ma5[i - 1] <= ma10[i - 1] and ma5[i] > ma10[i]:
                result[i] = "golden_cross"
            elif ma5[i - 1] >= ma10[i - 1] and ma5[i] < ma10[i]:
                result[i] = "dead_cross"

        return result

    @staticmethod
    def _to_float(val):
        """Convert numpy float/NaN to Python float or None for DuckDB compatibility."""
        if val is None:
            return None
        try:
            f = float(val)
            if pd.isna(f):
                return None
            return f
        except (ValueError, TypeError):
            return None

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
                    self._to_float(row.get("ma_5")),
                    self._to_float(row.get("ma_10")),
                    self._to_float(row.get("bias_ma5")),
                    self._to_float(row.get("bias_ma10")),
                    self._to_float(row.get("ma5_slope")),
                    self._to_float(row.get("ma10_slope")),
                    row.get("alignment"),
                    row.get("turning_point"),
                    calc_date,
                ),
            )
