import logging
from typing import Optional

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
import pandas as pd
from backend.etl.base import (
    sma, linear_regression_slope, to_float_safe,
    weighted_window_slopes, sliding_window_mean_abs,
    compute_price_signal_divergence,
    insert_dws_batch, compute_input_fingerprint, check_dwd_unchanged,
    load_latest_fingerprints, load_latest_spec_versions,
    load_quote_groups, SkipReason, CalcResult,
)
from backend.etl.recalc_spec import RecalcSpec

logger = logging.getLogger(__name__)

# B4 vol_trend / w_vol_trend: 123 ``volume_trend_v2`` (分位生态法) params
VOLUME_TREND_V2_DAILY = {
    "anchor_bars": 60,
    "ma_window": 5,
    "confirm_window": 10,
    "confirm_count": 3,
    "high_percentile": 80,
    "low_percentile": 20,
    "amp_threshold": 1.4,
    "fast_count": 3,
    "recent_count": 2,
    "vol_flat_eps": 0.001,
}

VOLUME_TREND_V2_WEEKLY = {
    "anchor_bars": 30,
    "ma_window": 5,
    "confirm_window": 8,
    "confirm_count": 2,
    "high_percentile": 80,
    "low_percentile": 20,
    "amp_threshold": 1.4,
    "fast_count": 2,
    "recent_count": 2,
    "vol_flat_eps": 0.001,
}

_V2_DIRECTION_TO_TREND = {
    "放量中": "expanding",
    "缩量中": "shrinking",
    "平量": "flat",
}


def _slope_over_mean_abs(y_values, denom_eps=1e-9):
    """Tail linear slope / mean(|y|); matches 123 ``utils_volume.slope_over_mean_abs``."""
    y = np.asarray(y_values, dtype=float)
    y = y[np.isfinite(y)]
    if len(y) < 2:
        return None
    x = np.arange(len(y), dtype=float)
    try:
        slope = float(np.polyfit(x, y, 1)[0])
    except (np.linalg.LinAlgError, ValueError, TypeError):
        return None
    scale = float(np.mean(np.abs(y)) + denom_eps)
    if not np.isfinite(scale) or scale <= 0:
        return None
    v = slope / scale
    if not np.isfinite(v):
        return None
    return float(v)


def volume_trend_v2(raw_volume_series, anchor_bars=60, ma_window=5,
                    confirm_window=10, confirm_count=3,
                    high_percentile=80, low_percentile=20,
                    amp_threshold=1.4, fast_count=3, recent_count=2,
                    vol_flat_eps=0.001):
    """123 分位生态法量能趋势；返回 (score, label)。label 形如 ``正常区·放量中``。"""
    y = np.asarray(raw_volume_series, dtype=float)
    y = y[np.isfinite(y)]
    if len(y) < anchor_bars:
        return None, "数据不足"

    ma = pd.Series(y).rolling(window=ma_window, min_periods=ma_window).mean()
    ma = ma.dropna().to_numpy(dtype=float)
    if len(ma) < anchor_bars:
        return None, "数据不足"
    anchor = ma[-anchor_bars:]
    p80 = float(np.percentile(anchor, high_percentile))
    p20 = float(np.percentile(anchor, low_percentile))

    amp = p80 / max(p20, 1e-9)
    if amp < amp_threshold:
        regime = "振幅不足"
    else:
        recent_ma = ma[-confirm_window:] if len(ma) >= confirm_window else ma
        if len(recent_ma) >= fast_count and bool(np.all(recent_ma[-fast_count:] >= p80)):
            regime = "爆量区"
        elif len(recent_ma) >= fast_count and bool(np.all(recent_ma[-fast_count:] <= p20)):
            regime = "地量区"
        else:
            boom_days = int(np.sum(recent_ma >= p80))
            dry_days = int(np.sum(recent_ma <= p20))
            boom_recent = int(np.sum(recent_ma[-5:] >= p80)) if len(recent_ma) >= 5 else 0
            dry_recent = int(np.sum(recent_ma[-5:] <= p20)) if len(recent_ma) >= 5 else 0
            if boom_days >= confirm_count and boom_recent >= recent_count:
                regime = "爆量区"
            elif dry_days >= confirm_count and dry_recent >= recent_count:
                regime = "地量区"
            else:
                regime = "正常区"

    obs_5 = y[-5:] if len(y) >= 5 else y
    dir_val = _slope_over_mean_abs(obs_5)
    if dir_val is None:
        dir_val = 0.0
    if dir_val > vol_flat_eps:
        direction = "放量中"
    elif dir_val < -vol_flat_eps:
        direction = "缩量中"
    else:
        direction = "平量"

    label = f"{regime}·{direction}"
    pct = float(np.searchsorted(np.sort(anchor), ma[-1]) / len(anchor) * 100.0)
    eco_score = max(min((pct - 50.0) / 40.0, 1.0), -1.0)
    trend_score = max(min(dir_val * 100.0, 1.0), -1.0)
    score = round(float(max(min(eco_score * 0.4 + trend_score * 0.6, 1.0), -1.0)), 6)
    return score, label


def trend_from_v2_label(label: Optional[str]) -> Optional[str]:
    """Map 123 v2 label to DWS trend enum (direction only for B4)."""
    if label is None or label == "数据不足":
        return None
    if "·" in label:
        direction = label.split("·", 1)[1].strip()
    else:
        direction = label.strip()
    return _V2_DIRECTION_TO_TREND.get(direction)


def compute_volume_trend_series(
    vol_series,
    params: dict,
    target_indices: Optional[list] = None,
) -> list:
    """Per-bar ``volume_trend_v2`` on expanding prefixes; None until anchor met.

    Delegates to the vectorized implementation that pre-computes SMA and
    direction slopes once, then does a lightweight per-bar pass for
    percentile/regime classification only.
    """
    return _compute_volume_trend_series_vectorized(
        vol_series, params, target_indices=target_indices,
    )


def _compute_volume_trend_series_vectorized(
    vol_series,
    params: dict,
    target_indices: Optional[list] = None,
) -> list:
    """Pre-computed SMA + direction slopes version of compute_volume_trend_series.

    Avoids per-bar ``pd.Series.rolling()`` and ``np.polyfit`` by:
    1. Computing the full MA5 rolling series ONCE (via pandas, then .to_numpy())
    2. Pre-computing 5-bar OLS direction slopes for ALL bars via
       ``weighted_window_slopes(decay=0)`` / ``sliding_window_mean_abs``
    Then the per-bar loop only does percentile / regime classification,
    which are cheap (anchor: 60 or 30 elements).

    Semantically identical to compute_volume_trend_series; verified by
    test_compute_volume_trend_series_vectorized_matches_original.
    """
    from backend.etl.base import weighted_window_slopes, sliding_window_mean_abs

    vol = np.asarray(vol_series, dtype=float)
    n = len(vol)
    anchor_bars = int(params["anchor_bars"])
    ma_window = int(params.get("ma_window", 5))
    result = [None] * n
    kw = {k: params[k] for k in params if k not in ("anchor_bars", "ma_window")}

    # ---- pre-compute once ----
    # 1. Full MA5 series (same as volume_trend_v2's pd.Series.rolling)
    ma_full = (
        pd.Series(vol)
        .rolling(window=ma_window, min_periods=ma_window)
        .mean()
        .to_numpy(dtype=float)
    )

    # 2. Per-bar direction slopes for obs_5 = vol[i-4:i+1] (window=5, unweighted)
    dir_slopes = weighted_window_slopes(vol, window=5, decay=0.0)
    dir_scales = sliding_window_mean_abs(vol, window=5)
    # direction value = slope / scale (matches _slope_over_mean_abs)
    dir_vals = np.full(n, np.nan)
    with np.errstate(invalid="ignore"):
        dir_vals_ok = np.isfinite(dir_slopes) & (dir_scales > 1e-9)
        dir_vals[dir_vals_ok] = dir_slopes[dir_vals_ok] / dir_scales[dir_vals_ok]

    vol_flat_eps = float(kw.get("vol_flat_eps", 0.001))
    high_percentile = float(kw.get("high_percentile", 80))
    low_percentile = float(kw.get("low_percentile", 20))
    amp_threshold = float(kw.get("amp_threshold", 1.4))
    fast_count = int(kw.get("fast_count", 3))
    recent_count = int(kw.get("recent_count", 2))
    confirm_window = int(kw.get("confirm_window", 10))
    confirm_count = int(kw.get("confirm_count", 3))

    # ---- per-bar loop (only percentile + regime, no polyfit / rolling) ----
    indices = range(n) if target_indices is None else target_indices
    for i in indices:
        if i < 0 or i >= n:
            continue
        if i + 1 < anchor_bars:
            continue

        # ma for bars 0..i  (pre-computed, just slice)
        ma_i = ma_full[: i + 1]
        valid_ma = ma_i[~np.isnan(ma_i)]
        if len(valid_ma) < anchor_bars:
            continue

        anchor = valid_ma[-anchor_bars:]
        p80 = float(np.percentile(anchor, high_percentile))
        p20 = float(np.percentile(anchor, low_percentile))

        amp = p80 / max(p20, 1e-9)
        if amp < amp_threshold:
            regime = "振幅不足"
        else:
            recent_ma = (
                valid_ma[-confirm_window:]
                if len(valid_ma) >= confirm_window
                else valid_ma
            )
            if len(recent_ma) >= fast_count and bool(
                np.all(recent_ma[-fast_count:] >= p80)
            ):
                regime = "爆量区"
            elif len(recent_ma) >= fast_count and bool(
                np.all(recent_ma[-fast_count:] <= p20)
            ):
                regime = "地量区"
            else:
                boom_days = int(np.sum(recent_ma >= p80))
                dry_days = int(np.sum(recent_ma <= p20))
                boom_recent = (
                    int(np.sum(recent_ma[-5:] >= p80))
                    if len(recent_ma) >= 5
                    else 0
                )
                dry_recent = (
                    int(np.sum(recent_ma[-5:] <= p20))
                    if len(recent_ma) >= 5
                    else 0
                )
                if boom_days >= confirm_count and boom_recent >= recent_count:
                    regime = "爆量区"
                elif dry_days >= confirm_count and dry_recent >= recent_count:
                    regime = "地量区"
                else:
                    regime = "正常区"

        # direction from pre-computed dir_vals
        dir_val = dir_vals[i]
        if not np.isfinite(dir_val):
            dir_val = 0.0
        if dir_val > vol_flat_eps:
            direction = "放量中"
        elif dir_val < -vol_flat_eps:
            direction = "缩量中"
        else:
            direction = "平量"

        label = f"{regime}·{direction}"
        result[i] = trend_from_v2_label(label)

    return result


def resolve_trend_target_indices(
    df: pd.DataFrame, new_bars: list,
) -> list:
    """Map APPEND ``new_bars`` trade dates to row indices in ``df`` (no validation)."""
    td_set = {str(d) for d in new_bars}
    return [i for i, d in enumerate(df["trade_date"].astype(str)) if d in td_set]


def require_trend_target_indices(
    df: pd.DataFrame,
    new_bars: Optional[list],
    *,
    ts_code: str = "",
) -> list:
    """APPEND gate: ``new_bars`` must 1:1 map to tail df row indices."""
    prefix = f"ts_code={ts_code} " if ts_code else ""
    if new_bars is None:
        raise ValueError(f"{prefix}APPEND volume trend requires new_bars")
    if len(new_bars) == 0:
        raise ValueError(f"{prefix}APPEND volume trend new_bars must be non-empty")
    indices = resolve_trend_target_indices(df, new_bars)
    if len(indices) != len(new_bars):
        str_bars = [str(d) for d in new_bars]
        if len(set(str_bars)) != len(str_bars):
            raise ValueError(
                f"{prefix}duplicate dates in new_bars: {new_bars}"
            )
        td_in_df = set(df["trade_date"].astype(str))
        missing = [s for s in str_bars if s not in td_in_df]
        raise ValueError(
            f"{prefix}new_bars not found in tail df: missing={missing} "
            f"new_bars={new_bars} mapped={len(indices)}"
        )
    return indices


def _compute_pct_rank_vectorized(ma_vol_5: np.ndarray, window: int = 120) -> np.ndarray:
    """Percentile rank (vectorized, single-pass).

    For each bar ``i >= window-1``, computes the fraction of valid values
    in ``ma_vol_5[i-window+1 : i+1]`` that are <= ``ma_vol_5[i]``, then
    multiplies by 100.  Uses ``sliding_window_view`` to compare all bars
    against their respective windows in one broadcast operation.

    Matches the original per-bar loop exactly (verified by
    ``test_pct_rank_vectorized_matches_original``).
    """
    n = len(ma_vol_5)
    result = np.full(n, np.nan)

    if n < window:
        return result

    # sliding_window_view(x, w) returns shape (n-w+1, w):
    #   row k = x[k : k+w]  for k = 0 .. n-w
    # For bar i, the trailing window is ma_vol_5[i-window+1 : i+1],
    # which is row (i-window+1) of the view.
    windows = sliding_window_view(ma_vol_5, window)               # (n-w+1, window)
    cur = ma_vol_5[window - 1:]                                    # (n-w+1,)

    valid_mask = ~np.isnan(windows)                                # (n-w+1, window)
    valid_count = valid_mask.sum(axis=1)                           # (n-w+1,)

    cur_2d = cur[:, np.newaxis]                                    # (n-w+1, 1)
    le = (windows <= cur_2d) & valid_mask                          # (n-w+1, window)
    rank = le.sum(axis=1) / np.maximum(valid_count, 1) * 100.0    # (n-w+1,)

    apply_mask = (valid_count >= 2) & np.isfinite(cur)
    result[window - 1:][apply_mask] = rank[apply_mask]

    return result


class VolumeCalculator:
    """Volume indicator calculator.

    Computes MA5 volume, percentile rank, zone classification (explosive,
    low_volume, normal), and trend (expanding, shrinking, flat via 123 v2).
    ``trend_strength`` remains ln(vol) weighted regression (non-B4).
    Works for both daily and weekly frequencies.
    """

    # B4 vol_trend v2: bump invalidates fingerprint-only skip (pre-v2 DWS rows).
    SPEC_VERSION = "v2"

    RECALC_SPEC_DAILY = RecalcSpec(lookback=120, seed=5, event_tail=5, min_rows=5)
    RECALC_SPEC_WEEKLY = RecalcSpec(lookback=120, seed=5, event_tail=5, min_rows=5)

    DWS_COLS = [
        "ts_code", "trade_date", "ma_vol_5", "pct_vol_rank",
        "zone", "trend", "volume_ratio", "trend_strength",
        "divergence", "calc_date", "input_fingerprint", "spec_version",
    ]
    FLOAT_COLS = ["ma_vol_5", "pct_vol_rank", "volume_ratio", "trend_strength"]

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws_table = f"dws_volume_{freq}"
        self.SIGNATURE_COLS = ["close_qfq", "vol"]
        if freq == "weekly":
            self.SIGNATURE_COLS = ["close_qfq", "vol", "active_days"]

    @staticmethod
    def quote_load_columns(freq: str) -> list:
        cols = ["trade_date", "vol", "close_qfq"]
        if freq == "weekly":
            cols.append("active_days")
        return cols

    def calculate(self, ts_codes: list[str], calc_date: str,
                  recalc_start: str = None,
                  quote_groups: dict = None) -> CalcResult:
        result = CalcResult()
        latest_fps = load_latest_fingerprints(self.con, self.dws_table, ts_codes)
        latest_specs = load_latest_spec_versions(self.con, self.dws_table, ts_codes)
        if quote_groups is None:
            load_start = None
            if recalc_start:
                from backend.etl.recalc_spec import resolve_load_start
                load_start = resolve_load_start(self.con, recalc_start, self.freq)
            groups = load_quote_groups(
                self.con, self.src_table, self.freq,
                self.quote_load_columns(self.freq), ts_codes,
                start_date=load_start,
            )
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
        df = self._compute_volume_core(df)
        return self._compute_volume_derived(df, zone_seed=zone_seed)

    def _compute_volume_core(self, df: pd.DataFrame) -> pd.DataFrame:
        v = df["vol"].values.astype(float)
        df["ma_vol_5"] = sma(v, 5)
        df["volume_ratio"] = self._compute_volume_ratio(df)
        df["pct_vol_rank"] = _compute_pct_rank_vectorized(df["ma_vol_5"].values, 120)
        return df

    def _compute_volume_derived(
        self, df: pd.DataFrame, zone_seed: Optional[str] = None,
        trend_target_indices: Optional[list] = None,
    ) -> pd.DataFrame:
        v = df["vol"].values.astype(float)
        df["zone"] = self._compute_zone(df, zone_seed=zone_seed)
        vol_for_trend = v
        if getattr(self, "freq", "daily") == "weekly" and "active_days" in df.columns:
            ad = df["active_days"].astype(float).values
            vol_for_trend = v * ad / 5.0
        df["trend"] = self._compute_trend(
            vol_for_trend, 10, target_indices=trend_target_indices,
        )
        window = 10
        df["trend_strength"] = self._compute_trend_strength(df["vol"].values, window=window)
        df["divergence"] = self._compute_divergence(df)
        return df

    def _compute_indicators_append(self, df: pd.DataFrame,
                                    new_bars: list,
                                    zone_seed: Optional[str] = None,
                                    ts_code: str = "") -> pd.DataFrame:
        """Compute volume indicators for append / tail-window mode.

        Delegates to _compute_indicators with the zone_seed initialising the
        hysteresis state.  All rolling functions (MA5, pct_vol_rank, trend,
        divergence) are causal: the last bar's value depends only on its
        trailing window, which the caller-supplied tail df fully contains.
        The zone state at the first bar of df is seeded from zone_seed
        (the stored DWS zone of the bar immediately before df starts).

        ``new_bars`` restricts ``volume_trend_v2`` to those indices only
        (O(k) vs O(n²) expanding); other derived columns still use full tail.

        Callers must supply df with >= 120 bars for pct_vol_rank accuracy.
        """
        df = self._compute_volume_core(df)
        trend_target = require_trend_target_indices(
            df, new_bars, ts_code=ts_code,
        )
        return self._compute_volume_derived(
            df, zone_seed=zone_seed, trend_target_indices=trend_target,
        )

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
        df = self._compute_indicators_append(
            df, new_bars, zone_seed=zone_seed, ts_code=ts_code,
        )
        fp = compute_history_signature(df, self.SIGNATURE_COLS)
        if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                        write_start=new_bars[0], write_end=new_bars[-1]):
            result.calculated += 1
        return result

    @staticmethod
    def _compute_pct_rank(ma_vol_5: np.ndarray, window: int) -> np.ndarray:
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

    def _compute_trend(
        self,
        vol_series: np.ndarray,
        window: int,
        target_indices: Optional[list] = None,
    ) -> list:
        """B4 trend via 123 ``volume_trend_v2`` (daily anchor=60, weekly=30).

        ``window`` kept for API compatibility; v2 uses its own tail windows.
        """
        params = (
            VOLUME_TREND_V2_DAILY
            if getattr(self, "freq", "daily") == "daily"
            else VOLUME_TREND_V2_WEEKLY
        )
        return compute_volume_trend_series(
            vol_series, params, target_indices=target_indices,
        )

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
        return insert_dws_batch(self.con, self.dws_table, df, ts_code, calc_date,
                                self.DWS_COLS, self.FLOAT_COLS,
                                spec_version=self.SPEC_VERSION,
                                input_fingerprint=input_fingerprint,
                                write_start=write_start, write_end=write_end)
