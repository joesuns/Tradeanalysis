import logging
from typing import Optional

import numpy as np
import pandas as pd
from backend.etl.base import (
    sma, linear_regression_slope, to_float_safe,
    weighted_window_slopes, sliding_window_mean_abs,
    compute_price_signal_divergence,
    insert_dws_batch, compute_input_fingerprint, check_dwd_unchanged,
    load_latest_fingerprints, load_quote_groups, SkipReason, CalcResult,
)
from backend.etl.recalc_spec import RecalcSpec

logger = logging.getLogger(__name__)


class VolumeCalculator:
    """Volume indicator calculator.

    Computes MA5 volume, percentile rank, zone classification (explosive,
    low_volume, normal), and trend (expanding, shrinking, flat).
    Works for both daily and weekly frequencies.
    """

    RECALC_SPEC_DAILY = RecalcSpec(lookback=120, seed=5, event_tail=5, min_rows=5)
    RECALC_SPEC_WEEKLY = RecalcSpec(lookback=120, seed=5, event_tail=5, min_rows=5)

    SIGNATURE_COLS = ["close_qfq", "vol"]

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws_table = f"dws_volume_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str,
                  recalc_start: str = None,
                  quote_groups: dict = None) -> CalcResult:
        result = CalcResult()
        latest_fps = load_latest_fingerprints(self.con, self.dws_table, ts_codes)
        if quote_groups is None:
            load_start = None
            if recalc_start:
                from backend.etl.recalc_spec import resolve_load_start
                load_start = resolve_load_start(self.con, recalc_start, self.freq)
            groups = load_quote_groups(self.con, self.src_table, self.freq,
                                       ["trade_date", "vol", "close_qfq"], ts_codes,
                                       start_date=load_start)
        else:
            groups = quote_groups
        for ts_code in ts_codes:
            df = groups.get(ts_code)

            if df is None or df.empty:
                logger.debug("Volume %s skip %s: no DWD data", self.freq, ts_code)
                result.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
                continue
            if len(df) < 5:
                logger.debug("Volume %s skip %s: %d rows < 5",
                             self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=5")
                continue

            if check_dwd_unchanged(self.con, self.dws_table, ts_code, df,
                                   latest_fps=latest_fps, recalc_start=recalc_start):
                result.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                                "DWD fingerprint match")
                continue

            fp = compute_input_fingerprint(df, recalc_start=recalc_start)
            df = self._compute_indicators(df)
            if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                            write_start=recalc_start,
                            write_end=calc_date if recalc_start else None):
                result.calculated += 1
        return result

    def _compute_indicators(self, df: pd.DataFrame,
                             zone_seed: Optional[str] = None) -> pd.DataFrame:
        v = df["vol"].values.astype(float)

        # MA5 volume
        df["ma_vol_5"] = sma(v, 5)

        # Volume ratio: vol / MA5_vol
        df["volume_ratio"] = self._compute_volume_ratio(df)

        # Percentile rank of MA5_vol within last 120 days
        df["pct_vol_rank"] = self._compute_pct_rank(df["ma_vol_5"].values, 120)

        # Zone: explosive / low_volume / normal (hysteresis; pass seed for append mode)
        df["zone"] = self._compute_zone(df, zone_seed=zone_seed)

        # Trend: linear regression slope on ln(raw_vol) over 10 days
        df["trend"] = self._compute_trend(df["vol"].values, 10)

        # Trend strength: de-unitized slope
        window = 10
        df["trend_strength"] = self._compute_trend_strength(df["vol"].values, window=window)

        # Divergence: vol vs close over 60-day window
        df["divergence"] = self._compute_divergence(df)

        return df

    def _compute_indicators_append(self, df: pd.DataFrame,
                                    zone_seed: Optional[str] = None) -> pd.DataFrame:
        """Compute volume indicators for append / tail-window mode.

        Delegates to _compute_indicators with the zone_seed initialising the
        hysteresis state.  All rolling functions (MA5, pct_vol_rank, trend,
        divergence) are causal: the last bar's value depends only on its
        trailing window, which the caller-supplied tail df fully contains.
        The zone state at the first bar of df is seeded from zone_seed
        (the stored DWS zone of the bar immediately before df starts).

        Callers must supply df with >= 120 bars for pct_vol_rank accuracy.
        """
        return self._compute_indicators(df, zone_seed=zone_seed)

    def _fetch_zone_seed(self, ts_code: str, before_date: str) -> Optional[str]:
        """Return the stored zone of the last DWS bar strictly before before_date.

        Queries the latest calc_date snapshot to correctly initialise zone
        hysteresis for tail-window APPEND recompute.  Returns None when no
        prior bar exists (new stock, first calc, or no DWS connection).
        """
        if self.con is None:
            return None
        try:
            row = self.con.execute(f"""
                SELECT zone FROM (
                    SELECT zone,
                           ROW_NUMBER() OVER (
                               PARTITION BY ts_code
                               ORDER BY trade_date DESC, calc_date DESC
                           ) AS rn
                    FROM {self.dws_table}
                    WHERE ts_code = ? AND trade_date < ?
                      AND zone IS NOT NULL
                ) WHERE rn = 1
            """, [ts_code, before_date]).fetchone()
        except Exception:
            return None
        return row[0] if row else None

    def append_calculate(self, ts_code: str, df: pd.DataFrame, new_bars: list,
                         calc_date: str, state: dict) -> "CalcResult":
        """APPEND mode: compute over tail-window df, write only new_bars.

        Zone hysteresis is correctly seeded by fetching the stored DWS zone
        of the bar immediately before df starts (_fetch_zone_seed).  All
        other indicators (MA5, pct_vol_rank, trend, divergence) are causal
        rolling functions that match FULL given a tail window >= 120 bars.

        Signature columns: ["close_qfq", "vol"].
        Only rows in new_bars are written to DWS (write_start/write_end).
        """
        from backend.etl.base import compute_history_signature
        result = CalcResult()
        first_date = str(df["trade_date"].min())
        zone_seed = self._fetch_zone_seed(ts_code, before_date=first_date)
        df = self._compute_indicators_append(df, zone_seed=zone_seed)
        fp = compute_history_signature(df, self.SIGNATURE_COLS)
        if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                        write_start=new_bars[0], write_end=new_bars[-1]):
            result.calculated += 1
        return result

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

    def _compute_zone(self, df: pd.DataFrame,
                      zone_seed: Optional[str] = None) -> list:
        """Classify volume zone based on percentile rank persistence.

        - explosive: P90 threshold (pct_vol_rank > 90 for 2 consecutive days)
          Exit: pct_vol_rank < 75 for 2 consecutive days
        - low_volume: P10 threshold (pct_vol_rank < 10 for 5 consecutive days)
          Exit: pct_vol_rank > 25 for 2 consecutive days
        - normal: everything else

        zone_seed: zone of the bar immediately *before* df starts, used to
            initialise hysteresis state for append / tail-window recompute.
            - "explosive"  → start with in_explosive=True
            - "low_volume" → start with in_low_volume=True
            - None / "normal" → start with both False (default / full recompute)
        """
        n = len(df)
        rank = df["pct_vol_rank"].values
        result = [None] * n

        in_explosive = (zone_seed == "explosive")
        in_low_volume = (zone_seed == "low_volume")

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

    def _log_slope_and_scale(self, vol_series: np.ndarray, window: int,
                             min_pos: int = 5, decay: float = 0.20):
        """Per-window weighted log-vol regression slope + |log| scale.

        Exactly reproduces the legacy compacted-positive per-bar loop:
        per window, keep non-NaN values > 0, log them, re-base x to 0..m-1,
        weighted (decay) LS slope; scale = mean(|log values|).

        Hybrid for speed: windows whose `window` values are ALL non-NaN and > 0
        (the overwhelmingly common case) use the vectorized fixed-window closed
        form on ln(vol); only windows containing NaN / non-positive values fall
        back to the exact per-bar computation. Windows with < min_pos positive
        values are marked invalid (→ NaN downstream).

        Returns (slopes, scales, valid): float arrays + bool mask of length n.
        """
        vol = np.asarray(vol_series, dtype=float)
        n = len(vol)
        slopes = np.full(n, np.nan)
        scales = np.full(n, np.nan)
        valid = np.zeros(n, dtype=bool)
        if n < window:
            return slopes, scales, valid

        pos = (~np.isnan(vol)) & (vol > 0)
        pos_count = np.convolve(pos.astype(float), np.ones(window), mode="valid")
        valid[window - 1:] = pos_count >= min_pos

        # Fast path: log(vol) is NaN at non-positive/NaN positions, so
        # weighted_window_slopes is finite ONLY for fully-positive windows.
        logvol = np.log(np.where(pos, vol, np.nan))
        fast_slope = weighted_window_slopes(logvol, window, decay)
        fast_scale = sliding_window_mean_abs(logvol, window)
        fast_mask = np.isfinite(fast_slope)
        slopes[fast_mask] = fast_slope[fast_mask]
        scales[fast_mask] = fast_scale[fast_mask]

        # Slow path: valid but not full (has NaN / non-positive in window).
        for i in np.nonzero(valid & ~fast_mask)[0]:
            segment = vol[i - window + 1:i + 1]
            vp = segment[~np.isnan(segment)]
            vp = vp[vp > 0]
            log_segment = np.log(vp)
            m = len(log_segment)
            x = np.arange(m, dtype=float)
            weights = np.exp(x * decay)
            try:
                s = float(np.polyfit(x, log_segment, 1, w=weights)[0])
            except (np.linalg.LinAlgError, ValueError, TypeError):
                valid[i] = False
                continue
            if not np.isfinite(s):
                valid[i] = False
                continue
            slopes[i] = s
            scales[i] = float(np.mean(np.abs(log_segment)))
        return slopes, scales, valid

    def _compute_trend(self, vol_series: np.ndarray, window: int) -> list:
        """Volume trend via exponentially weighted linear regression on ln(vol).

        Weighted regression (decay=0.20) — same method as _compute_trend_strength
        and DDE trend. Ensures trend direction and trend_strength never contradict.
        - expanding: weighted_slope > 0.008
        - shrinking: weighted_slope < -0.008
        - flat: otherwise
        """
        slopes, _, valid = self._log_slope_and_scale(vol_series, window)
        result = [None] * len(vol_series)
        for i in np.nonzero(valid)[0]:
            s = slopes[i]
            if s > 0.008:
                result[i] = "expanding"
            elif s < -0.008:
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
        slopes, scales, valid = self._log_slope_and_scale(vol_series, window)
        result = np.full(len(vol_series), np.nan)
        result[valid & (scales < 1e-6)] = 0.0
        mask = valid & (scales >= 1e-6)
        result[mask] = slopes[mask] / scales[mask]
        return result

    def _compute_divergence(self, df: pd.DataFrame) -> list:
        """Top/bottom volume-price divergence (vectorized rolling + dedup)."""
        if "close_qfq" not in df.columns:
            return [None] * len(df)
        return compute_price_signal_divergence(
            df["close_qfq"].values, df["vol"].values, window=60, dedup=5,
        )

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str,
                input_fingerprint: str = None,
                write_start: str = None, write_end: str = None):
        dws_cols = ["ts_code", "trade_date", "ma_vol_5", "pct_vol_rank",
                    "zone", "trend", "volume_ratio", "trend_strength",
                    "divergence", "calc_date", "input_fingerprint", "spec_version"]
        float_cols = ["ma_vol_5", "pct_vol_rank", "volume_ratio", "trend_strength"]
        return insert_dws_batch(self.con, self.dws_table, df, ts_code, calc_date,
                                dws_cols, float_cols,
                                input_fingerprint=input_fingerprint,
                                write_start=write_start, write_end=write_end)
