"""Cross-stock volume MA5 / pct_vol_rank batch compute."""
from typing import Dict, List, Tuple

import numpy as np


def _stack_vol_matrix(
    ts_codes: List[str],
    quote_groups: dict,
) -> Tuple[List[str], np.ndarray]:
    lengths = {}
    for code in ts_codes:
        df = quote_groups.get(code)
        if df is None or len(df) == 0:
            continue
        n = len(df)
        lengths.setdefault(n, []).append(code)
    if not lengths:
        return [], np.empty((0, 0))
    n_bars = max(lengths.keys(), key=lambda k: len(lengths[k]))
    codes = lengths[n_bars]
    mat = np.stack([quote_groups[c]["vol"].values.astype(float) for c in codes])
    return codes, mat


def sma_matrix(values: np.ndarray, period: int) -> np.ndarray:
    """Row-wise SMA matching base.sma semantics."""
    n_stocks, n_bars = values.shape
    out = np.full_like(values, np.nan)
    for i in range(period - 1, n_bars):
        w = values[:, i - period + 1:i + 1]
        valid_count = np.sum(~np.isnan(w), axis=1)
        with np.errstate(all="ignore"):
            means = np.nanmean(w, axis=1)
        mask = valid_count > 0
        out[mask, i] = means[mask]
    return out


def pct_rank_matrix(ma_vol_5: np.ndarray, window: int = 120) -> np.ndarray:
    """Row-wise percentile rank — matches VolumeCalculator._compute_pct_rank."""
    n_stocks, n_bars = ma_vol_5.shape
    result = np.full_like(ma_vol_5, np.nan)
    for i in range(window - 1, n_bars):
        start = max(0, i - window + 1)
        w = ma_vol_5[:, start:i + 1]
        cur = ma_vol_5[:, i]
        finite = ~np.isnan(w)
        valid_count = finite.sum(axis=1)
        le = (w <= cur[:, np.newaxis]) & finite
        rank = le.sum(axis=1) / valid_count * 100.0
        mask = (valid_count >= 2) & np.isfinite(cur)
        result[mask, i] = rank[mask]
    return result


def batch_volume_rolling_core(
    ts_codes: List[str],
    quote_groups: dict,
    pct_window: int = 120,
    ma_period: int = 5,
) -> Dict[str, dict]:
    """Batch ma_vol_5, volume_ratio, pct_vol_rank for aligned quote tails."""
    codes, vol_mat = _stack_vol_matrix(ts_codes, quote_groups)
    if not codes:
        return {}

    ma_vol_5 = sma_matrix(vol_mat, ma_period)
    pct_vol_rank = pct_rank_matrix(ma_vol_5, pct_window)
    vol_ratio = np.full_like(vol_mat, np.nan)
    mask = ~np.isnan(ma_vol_5) & (ma_vol_5 > 0)
    vol_ratio[mask] = vol_mat[mask] / ma_vol_5[mask]

    out: Dict[str, dict] = {}
    for i, code in enumerate(codes):
        out[code] = {
            "ma_vol_5": ma_vol_5[i],
            "pct_vol_rank": pct_vol_rank[i],
            "volume_ratio": vol_ratio[i],
        }
    return out


def attach_volume_core_to_df(df, core: dict):
    """Attach vector-computed rolling volume columns."""
    df = df.copy()
    df["ma_vol_5"] = core["ma_vol_5"]
    df["pct_vol_rank"] = core["pct_vol_rank"]
    df["volume_ratio"] = core["volume_ratio"]
    return df
