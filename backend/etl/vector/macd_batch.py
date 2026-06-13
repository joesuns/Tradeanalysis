"""Cross-stock MACD EMA batch recurrence — matches base.ema(seed=...) per row."""
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


def ema_seeded_matrix(
    values: np.ndarray,
    period: int,
    seeds: np.ndarray,
) -> np.ndarray:
    """Batch EMA with per-row seeds. values: (n_stocks, n_bars); seeds: (n_stocks,)."""
    n_stocks, n_bars = values.shape
    alpha = 2.0 / (period + 1)
    result = np.full((n_stocks, n_bars), np.nan, dtype=float)
    prev = np.asarray(seeds, dtype=float).copy()
    for j in range(n_bars):
        col = values[:, j]
        valid = np.isfinite(col)
        computed = alpha * col + (1.0 - alpha) * prev
        result[:, j] = np.where(valid, computed, prev)
        prev = np.where(valid, result[:, j], prev)
    return result


def _stack_close_matrix(
    ts_codes: List[str],
    quote_groups: dict,
) -> Tuple[List[str], np.ndarray]:
    """Stack close_qfq rows for stocks sharing the same tail length."""
    lengths = {}
    for code in ts_codes:
        df = quote_groups.get(code)
        if df is None or len(df) == 0:
            continue
        n = len(df)
        lengths.setdefault(n, []).append(code)
    if not lengths:
        return [], np.empty((0, 0))
    # Dominant length first (typical 245-bar tail).
    n_bars = max(lengths.keys(), key=lambda k: len(lengths[k]))
    codes = lengths[n_bars]
    mat = np.stack([
        quote_groups[c]["close_qfq"].values.astype(float) for c in codes
    ])
    return codes, mat


def batch_macd_ema_core(
    ts_codes: List[str],
    quote_groups: dict,
    seeds_by_code: Dict[str, dict],
) -> Dict[str, dict]:
    """Compute ema_12/26/dea/dif/macd_bar for a batch of stocks.

    Returns {ts_code: {col_name: np.ndarray}} for stocks with aligned tails.
    Stocks skipped (missing data / no seed) are omitted.
    """
    codes, close_mat = _stack_close_matrix(ts_codes, quote_groups)
    if not codes:
        return {}

    n = len(codes)
    seed12 = np.array([
        (seeds_by_code.get(c) or {}).get("ema_12", np.nan) for c in codes
    ], dtype=float)
    seed26 = np.array([
        (seeds_by_code.get(c) or {}).get("ema_26", np.nan) for c in codes
    ], dtype=float)

    ema12 = ema_seeded_matrix(close_mat, 12, seed12)
    ema26 = ema_seeded_matrix(close_mat, 26, seed26)
    dif = ema12 - ema26
    seed_dea = np.array([
        (seeds_by_code.get(c) or {}).get("dea", np.nan) for c in codes
    ], dtype=float)
    dea = ema_seeded_matrix(dif, 9, seed_dea)
    macd_bar = 2.0 * (dif - dea)

    out: Dict[str, dict] = {}
    for i, code in enumerate(codes):
        out[code] = {
            "ema_12": ema12[i],
            "ema_26": ema26[i],
            "dif": dif[i],
            "dea": dea[i],
            "macd_bar": macd_bar[i],
        }
    return out


def attach_macd_core_to_df(df: pd.DataFrame, core: dict) -> pd.DataFrame:
    """Attach vector-computed MACD core columns to a quote tail frame."""
    df = df.copy()
    df["ema_12"] = core["ema_12"]
    df["ema_26"] = core["ema_26"]
    df["dif"] = core["dif"]
    df["dea"] = core["dea"]
    df["macd_bar"] = core["macd_bar"]
    df["zone"] = np.where(
        df["macd_bar"] > 0, "bull",
        np.where(df["macd_bar"] < 0, "bear", None),
    )
    return df
