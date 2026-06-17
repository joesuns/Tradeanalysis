"""Compute-domain helpers: align batch FULL compute with narrow write window."""
from typing import List, Optional

import pandas as pd


def resolve_compute_indices(
    df: pd.DataFrame,
    recalc_start: Optional[str],
    calc_date: str,
) -> List[int]:
    """Row indices with recalc_start <= trade_date <= calc_date (inclusive).

    When recalc_start is None, all rows in df are included.
    """
    if df is None or len(df) == 0:
        return []
    tds = df["trade_date"].astype(str).tolist()
    if recalc_start is None:
        return list(range(len(tds)))
    out: List[int] = []
    for i, td in enumerate(tds):
        if recalc_start <= td <= calc_date:
            out.append(i)
    return out
