import logging

import numpy as np
import pandas as pd
from backend.etl.base import (
    sma, linear_regression_slope, to_float_safe,
    insert_dws_batch, compute_fingerprint, check_dwd_unchanged,
    SkipReason, CalcResult,
)

logger = logging.getLogger(__name__)


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

    def calculate(self, ts_codes: list[str], calc_date: str) -> CalcResult:
        result = CalcResult()
        for ts_code in ts_codes:
            if self.freq == "weekly":
                df = self.con.execute(f"""
                    SELECT d.trade_date, d.vol, d.close_qfq FROM {self.src_table} d
                    JOIN dim_date dd ON d.trade_date = dd.trade_date
                    WHERE d.ts_code = ? AND dd.is_week_end = 1
                    ORDER BY d.trade_date
                """, (ts_code,)).df()
            else:
                df = self.con.execute(f"""
                    SELECT trade_date, vol, close_qfq FROM {self.src_table}
                    WHERE ts_code = ? AND is_suspended = 0
                    ORDER BY trade_date
                """, (ts_code,)).df()

            if df.empty:
                logger.debug("Volume %s skip %s: no DWD data", self.freq, ts_code)
                result.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
                continue
            if len(df) < 5:
                logger.debug("Volume %s skip %s: %d rows < 5",
                             self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=5")
                continue

            if check_dwd_unchanged(self.con, self.dws_table, ts_code, df):
                result.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                                "DWD fingerprint match")
                continue

            fp = compute_fingerprint(df)
            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date, input_fingerprint=fp)
            result.calculated += 1
        return result

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        v = df["vol"].values.astype(float)

        # MA5 volume
        df["ma_vol_5"] = sma(v, 5)

        # Volume ratio: vol / MA5_vol
        df["volume_ratio"] = self._compute_volume_ratio(df)

        # Percentile rank of MA5_vol within last 120 days
        df["pct_vol_rank"] = self._compute_pct_rank(df["ma_vol_5"].values, 120)

        # Zone: explosive / low_volume / normal
        df["zone"] = self._compute_zone(df)

        # Trend: linear regression slope on ln(raw_vol) over 10 days
        df["trend"] = self._compute_trend(df["vol"].values, 10)

        # Trend strength: de-unitized slope
        window = 10
        df["trend_strength"] = self._compute_trend_strength(df["vol"].values, window=window)

        # Divergence: vol vs close over 60-day window
        df["divergence"] = self._compute_divergence(df)

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
        """Volume trend via exponentially weighted linear regression on ln(vol).

        Weighted regression (decay=0.20) — same method as _compute_trend_strength
        and DDE trend. Ensures trend direction and trend_strength never contradict.
        - expanding: weighted_slope > 0.008
        - shrinking: weighted_slope < -0.008
        - flat: otherwise
        """
        n = len(vol_series)
        result = [None] * n
        decay = 0.20

        for i in range(n):
            if i < window - 1:
                continue
            segment = vol_series[i - window + 1:i + 1]
            valid = segment[~np.isnan(segment)]
            valid_pos = valid[valid > 0]
            if len(valid_pos) < 5:
                continue

            log_segment = np.log(valid_pos)
            m = len(log_segment)
            x = np.arange(m, dtype=float)
            weights = np.exp(x * decay)
            try:
                slope = float(np.polyfit(x, log_segment, 1, w=weights)[0])
            except (np.linalg.LinAlgError, ValueError, TypeError):
                continue
            if not np.isfinite(slope):
                continue

            if slope > 0.008:
                result[i] = "expanding"
            elif slope < -0.008:
                result[i] = "shrinking"
            else:
                result[i] = "flat"

        return result

    def _compute_volume_ratio(self, df: pd.DataFrame) -> np.ndarray:
        """volume_ratio = vol / MA5_vol. NaN where MA5 not available."""
        vol = df["vol"].values.astype(float)
        ma5 = df["ma_vol_5"].values.astype(float)
        result = np.full(len(vol), np.nan)
        mask = ~np.isnan(ma5) & (ma5 > 0)
        result[mask] = vol[mask] / ma5[mask]
        return result

    def _compute_trend_strength(self, vol_series: np.ndarray, window: int = 10) -> np.ndarray:
        """Volume trend strength via exponentially weighted linear regression.

        Formula: weighted_slope(ln(vol)) / mean(|ln(vol)|), unitless.
        Positive = volume expanding, negative = shrinking.
        Weighted regression (decay=0.20) gives recent bars ~3x more influence.
        """
        n = len(vol_series)
        result = np.full(n, np.nan)
        for i in range(window - 1, n):
            segment = vol_series[i - window + 1:i + 1]
            valid = segment[~np.isnan(segment)]
            valid_pos = valid[valid > 0]
            if len(valid_pos) < 5:
                continue
            log_segment = np.log(valid_pos)
            m = len(log_segment)
            x = np.arange(m, dtype=float)
            weights = np.exp(x * 0.20)
            try:
                slope = float(np.polyfit(x, log_segment, 1, w=weights)[0])
            except (np.linalg.LinAlgError, ValueError, TypeError):
                continue
            if not np.isfinite(slope):
                continue
            scale = np.mean(np.abs(log_segment))
            if scale < 1e-6:
                result[i] = 0.0
            else:
                result[i] = slope / scale
        return result

    def _compute_divergence(self, df: pd.DataFrame) -> list:
        """Top/bottom volume-price divergence using 60-day window.

        Confirmation day + 5-day dedup, same pattern as MACD/DDE divergence.
        - Top divergence: price near 60d high + vol has fallen from 60d peak
        - Bottom divergence: price near 60d low + vol has recovered from 60d valley
        """
        result = [None] * len(df)
        if "close_qfq" not in df.columns:
            return result
        w = 59  # 60-bar window
        for i in range(w, len(df)):
            window_close = df["close_qfq"].iloc[i - w:i + 1]
            window_vol = df["vol"].iloc[i - w:i + 1]

            c_hi = window_close.max()
            c_lo = window_close.min()
            v_hi = window_vol.max()
            v_lo = window_vol.min()
            cur_c = df["close_qfq"].iloc[i]
            cur_v = df["vol"].iloc[i]

            if pd.isna(cur_c) or pd.isna(cur_v):
                continue

            # Top divergence: price at 60d high, vol has fallen from 60d peak
            vol_peak_iloc = np.argmax(window_vol.values)
            vol_fallen = v_hi != 0 and cur_v < window_vol.values[vol_peak_iloc]
            price_near_peak = cur_c >= c_hi * 0.98

            if vol_peak_iloc < w and vol_fallen and price_near_peak:
                recent = any(
                    result[j] == "top_divergence" for j in range(max(0, i - 5), i)
                )
                if not recent:
                    result[i] = "top_divergence"

            # Bottom divergence: price at 60d low, vol has recovered from 60d valley
            vol_valley_iloc = np.argmin(window_vol.values)
            vol_recovered = v_lo != 0 and cur_v > window_vol.values[vol_valley_iloc]
            vol_recovery_pct = (cur_v - v_lo) / abs(v_lo) if v_lo != 0 else 0
            vol_confirmed = vol_recovery_pct > 0.1
            c_lo_iloc = np.argmin(window_close.values)
            price_stopped = (w - c_lo_iloc) >= 3
            price_near_bottom = cur_c <= c_lo * 1.02

            if (vol_valley_iloc < w and vol_recovered and vol_confirmed
                    and price_stopped and price_near_bottom):
                recent = any(
                    result[j] == "bottom_divergence" for j in range(max(0, i - 5), i)
                )
                if not recent:
                    result[i] = "bottom_divergence"

        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str,
                input_fingerprint: str = None):
        dws_cols = ["ts_code", "trade_date", "ma_vol_5", "pct_vol_rank",
                    "zone", "trend", "volume_ratio", "trend_strength",
                    "divergence", "calc_date", "input_fingerprint", "spec_version"]
        float_cols = ["ma_vol_5", "pct_vol_rank", "volume_ratio", "trend_strength"]
        insert_dws_batch(self.con, self.dws_table, df, ts_code, calc_date,
                         dws_cols, float_cols,
                         input_fingerprint=input_fingerprint)
