"""Cross-stock DDE ddx/ddx2 batch compute."""
from typing import Dict, List, Tuple

import numpy as np

from backend.etl.vector.macd_batch import ema_seeded_matrix


def _stack_dde_matrix(
    ts_codes: List[str],
    dde_groups: dict,
) -> Tuple[List[str], dict]:
    """Stack DDE input columns for stocks with the same tail length."""
    lengths = {}
    for code in ts_codes:
        df = dde_groups.get(code)
        if df is None or len(df) == 0:
            continue
        n = len(df)
        lengths.setdefault(n, []).append(code)
    if not lengths:
        return [], {}
    n_bars = max(lengths.keys(), key=lambda k: len(lengths[k]))
    codes = lengths[n_bars]
    mats = {}
    for col in (
        "buy_lg_vol", "sell_lg_vol", "buy_elg_vol", "sell_elg_vol",
        "total_vol", "net_mf_amount",
    ):
        mats[col] = np.stack([
            dde_groups[c][col].values.astype(float) for c in codes
        ])
    skip = np.stack([
        dde_groups[c].get("_skip_dde", __import__("pandas").Series([False] * n_bars))
        .values.astype(bool)
        for c in codes
    ])
    mats["_skip_dde"] = skip
    return codes, mats


def batch_ddx_ddx2_core(
    ts_codes: List[str],
    dde_groups: dict,
    seeds_by_code: Dict[str, dict],
) -> Dict[str, dict]:
    """Compute ddx and ddx2 for aligned DDE tail frames."""
    codes, mats = _stack_dde_matrix(ts_codes, dde_groups)
    if not codes:
        return {}

    buy_lg = mats["buy_lg_vol"]
    sell_lg = mats["sell_lg_vol"]
    buy_elg = mats["buy_elg_vol"]
    sell_elg = mats["sell_elg_vol"]
    total = mats["total_vol"]
    skip = mats["_skip_dde"]

    net_big = buy_lg + buy_elg - sell_lg - sell_elg
    ddx = np.full_like(net_big, np.nan)
    valid = (~skip) & (total != 0) & np.isfinite(total)
    ddx[valid] = net_big[valid] / total[valid]

    seed_ddx2 = np.array([
        (seeds_by_code.get(c) or {}).get("ddx2", np.nan) for c in codes
    ], dtype=float)
    ddx2 = ema_seeded_matrix(ddx, 5, seed_ddx2)

    out: Dict[str, dict] = {}
    for i, code in enumerate(codes):
        out[code] = {
            "ddx": ddx[i],
            "ddx2": ddx2[i],
            "net_mf_amount": mats["net_mf_amount"][i],
        }
    return out


def attach_dde_core_to_df(df, core: dict):
    """Attach vector-computed DDE core columns."""
    import pandas as pd

    df = df.copy()
    df["ddx"] = core["ddx"]
    df["ddx2"] = core["ddx2"]
    df["net_mf_amount"] = core["net_mf_amount"]
    return df
