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
        window = 5  # 5-bar weighted regression for both daily and weekly
        df["trend"] = self._compute_trend(df["macd_bar"].values, window=window)
        df["trend_strength"] = self._compute_trend_strength(
            df["macd_bar"].values, window=window
        )
        df["divergence"] = self._compute_divergence(df)
        df["turning_point"] = self._compute_turning_points(df)
        df["alert"] = self._compute_alerts(df)
        return df

    def _compute_trend(self, bar: np.ndarray, window: int = 5) -> list:
        """MACD bar trend via exponentially weighted linear regression.
        Same method as 123 project: weighted slope with threshold 0.001.
        - up: weighted_slope > 0.001
        - down: weighted_slope < -0.001
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
            # Exponentially weighted regression (same as 123)
            n = len(valid)
            x = np.arange(n, dtype=float)
            weights = np.exp(x * 0.15)
            try:
                slope = float(np.polyfit(x, valid, 1, w=weights)[0])
            except (np.linalg.LinAlgError, ValueError, TypeError):
                continue
            if not np.isfinite(slope):
                continue
            if slope > 0.001:
                result[i] = "up"
            elif slope < -0.001:
                result[i] = "down"
            else:
                result[i] = "flat"
        return result

    def _compute_trend_strength(self, bar: np.ndarray, window: int = 5) -> np.ndarray:
        """MACD bar trend strength via exponentially weighted linear regression.

        Formula: slope / mean(|bar|), unitless signed value.
        Positive = bullish strength, negative = bearish strength.
        Weighted regression (decay=0.15) makes recent bars ~3x more influential.
        """
        result = np.full(len(bar), np.nan)
        for i in range(window - 1, len(bar)):
            segment = bar[i - window + 1:i + 1]
            valid = segment[~np.isnan(segment)]
            if len(valid) < window:
                continue
            # Weighted slope: recent bars carry more weight
            n = len(valid)
            x = np.arange(n, dtype=float)
            weights = np.exp(x * 0.15)
            try:
                slope = float(np.polyfit(x, valid, 1, w=weights)[0])
            except (np.linalg.LinAlgError, ValueError, TypeError):
                continue
            # Normalize by mean absolute value
            scale = np.mean(np.abs(valid))
            if scale < 1e-6:
                result[i] = 0.0
            elif np.isfinite(slope):
                result[i] = float(slope) / scale
        return result

    def _compute_divergence(self, df: pd.DataFrame) -> list:
        """Top/bottom divergence using 60-day window. Marked on confirmation day.

        Confirmation day = DIF has clearly rolled over from its 60d peak
        but price is still near its 60d high (within 2%).
        Deduplication: same type of divergence does not repeat within 5 bars.
        """
        result = [None] * len(df)
        w = 59  # 60-bar window: iloc[i-59 : i+1] = 60 elements
        for i in range(w, len(df)):
            window_close = df["close_qfq"].iloc[i - w : i + 1]
            window_dif = df["dif"].iloc[i - w : i + 1]
            c_hi = window_close.max()
            c_lo = window_close.min()
            d_hi = window_dif.max()
            d_lo = window_dif.min()
            cur_c = df["close_qfq"].iloc[i]
            cur_d = df["dif"].iloc[i]

            if pd.isna(cur_c) or pd.isna(cur_d):
                continue

            # Top divergence: DIF peaked in past, DIF has fallen from peak,
            #                price still near 60d high (within 2%).
            dif_peak_iloc = np.argmax(window_dif.values)
            dif_has_fallen = d_hi != 0 and cur_d < d_hi
            price_near_peak = cur_c >= c_hi * 0.98

            if dif_peak_iloc < w and dif_has_fallen and price_near_peak:
                # Dedup: no top_divergence within previous 5 bars
                recent = any(result[j] == "top_divergence" for j in range(max(0, i - 5), i))
                if not recent:
                    result[i] = "top_divergence"

            # Bottom divergence: DIF valley in past, DIF recovered >10%,
            #                   price stopped falling (low >= 3 bars ago).
            dif_valley_iloc = np.argmin(window_dif.values)
            dif_valley_val = window_dif.min()
            dif_has_recovered = d_lo != 0 and cur_d > d_lo
            # 回升确认：DIF 回升幅度 > 谷值绝对值的 10%
            dif_recovery_pct = (cur_d - d_lo) / abs(d_lo) if d_lo != 0 else 0
            dif_confirmed = dif_recovery_pct > 0.1
            # 价格止跌确认：60日低点距今 >= 3 根 bar
            c_lo_iloc = np.argmin(window_close.values)
            price_stopped = (w - c_lo_iloc) >= 3
            price_near_bottom = cur_c <= c_lo * 1.02

            if (dif_valley_iloc < w and dif_has_recovered and dif_confirmed
                    and price_stopped and price_near_bottom):
                recent = any(result[j] == "bottom_divergence" for j in range(max(0, i - 5), i))
                if not recent:
                    result[i] = "bottom_divergence"

        return result

    def _compute_turning_points(self, df: pd.DataFrame) -> list:
        """Golden cross / Dead cross / Near golden / Near dead.

        Golden/dead cross = MACD bar sign flip.
        Near = estimated days to cross < 3 (small-gap direct or speed-based).
        Small gap: |DIF-DEA| < 0.005 → direct near.
        Speed: 3-day gap regression slope < 0 AND gap/|slope| < 3.
        Zero-axis fallback (|DEA| < close * 0.1%): absolute threshold.
        """
        result = [None] * len(df)
        bar = df["macd_bar"].values
        dif = df["dif"].values
        dea = df["dea"].values
        close = df["close_qfq"].values

        for i in range(1, len(df)):
            if pd.isna(bar[i - 1]) or pd.isna(bar[i]):
                continue

            # Golden / dead cross: MACD bar sign flip
            if bar[i - 1] <= 0 and bar[i] > 0:
                result[i] = "golden_cross"
                continue
            elif bar[i - 1] >= 0 and bar[i] < 0:
                result[i] = "dead_cross"
                continue

            # Near golden / near dead: 预估交叉天数 < 3
            if pd.isna(dif[i]) or pd.isna(dea[i]) or dea[i] == 0:
                continue
            if pd.isna(dif[i - 1]) or pd.isna(dea[i - 1]):
                continue

            gap = abs(dif[i] - dea[i])

            # 小间距直通: DIF-DEA 几乎合并
            if gap < 0.005:
                if dif[i] < dea[i]:
                    result[i] = "near_golden"
                else:
                    result[i] = "near_dead"
                continue

            # 速度判定: 3 日回归 est_days = gap / convergence_speed
            if i >= 2:
                if not pd.isna(dif[i - 2]) and not pd.isna(dea[i - 2]):
                    gap_seq = np.array([
                        abs(dif[i - 2] - dea[i - 2]),
                        abs(dif[i - 1] - dea[i - 1]),
                        gap,
                    ])
                    gap_slope = linear_regression_slope(gap_seq, use_log=False)
                    if gap_slope < 0:
                        conv_speed = -gap_slope
                        if conv_speed > 1e-9 and gap / conv_speed < 3:
                            # 零轴兜底（保留不变）
                            if abs(dea[i]) < close[i] * 0.001:
                                near = gap < close[i] * 0.0001
                            else:
                                near = gap / abs(dea[i]) < 0.15
                            if near:
                                if dif[i] < dea[i]:
                                    result[i] = "near_golden"
                                else:
                                    result[i] = "near_dead"

        return result

    def _compute_alerts(self, df: pd.DataFrame) -> list:
        """Upturn/downturn reverse + flat alerts.

        - reverse: prev 2 consecutive rises/falls, then direction flips
        - flat: prev 2 consecutive rises/falls, then |change|/|prev| <= 2%
        Reverse takes priority over flat when bar[i] < bar[i-1] (or > for downtrend).
        """
        result = [None] * len(df)
        bar = df["macd_bar"].values
        for i in range(3, len(df)):
            prev = bar[i - 3:i + 1]
            if any(pd.isna(x) for x in prev):
                continue

            prev_up = bar[i - 1] > bar[i - 2] and bar[i - 2] > bar[i - 3]
            prev_down = bar[i - 1] < bar[i - 2] and bar[i - 2] < bar[i - 3]

            if prev_up:
                if bar[i] < bar[i - 1]:
                    result[i] = "upturn_reverse"
                elif bar[i - 1] != 0 and abs(bar[i] - bar[i - 1]) / abs(bar[i - 1]) <= 0.02:
                    result[i] = "upturn_flat"
            elif prev_down:
                if bar[i] > bar[i - 1]:
                    result[i] = "downturn_reverse"
                elif bar[i - 1] != 0 and abs(bar[i] - bar[i - 1]) / abs(bar[i - 1]) <= 0.02:
                    result[i] = "downturn_flat"

        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        for _, row in df.iterrows():
            self.con.execute(
                f"""INSERT OR REPLACE INTO {self.dws_table}
                (ts_code, trade_date, ema_12, ema_26, dif, dea, macd_bar,
                 divergence, zone, turning_point, alert, trend, trend_strength, calc_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    to_float_safe(row.get("trend_strength")),
                    calc_date,
                ),
            )
