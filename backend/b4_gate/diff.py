"""Compare B4 frames and report mismatches."""
from typing import Any, Dict, List, Optional, Set

import pandas as pd

from backend.b4_gate.columns import (
    B4_HARD_DAILY_FIELDS,
    B4_HARD_WEEKLY_FIELDS,
    weekly_field_name,
)
from backend.b4_gate.enums import normalize_value
from backend.b4_gate.sample import skip_dde_compare


DDE_FIELDS = {"dde_trend"}


def _norm(val: Any, field: str, source: str) -> Optional[str]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return normalize_value(field, str(val).strip(), source)


def diff_b4_frames(
    ta: pd.DataFrame,
    ref: pd.DataFrame,
    skip_dde_ts: Optional[Set[str]] = None,
    bucket_by_ts: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """Row-level mismatches on B4 hard-gate columns (10; excludes ma_alignment, dde_alert)."""
    skip_dde_ts = skip_dde_ts or set()
    bucket_by_ts = bucket_by_ts or {}
    mismatches: List[dict] = []
    merged = ta.merge(ref, on="ts_code", how="inner", suffixes=("_ta", "_ref"))
    for _, row in merged.iterrows():
        ts = row["ts_code"]
        bucket = bucket_by_ts.get(ts, "")
        skip_dde = ts in skip_dde_ts or skip_dde_compare(ts, bucket)
        for field in B4_HARD_DAILY_FIELDS:
            if skip_dde and field in DDE_FIELDS:
                continue
            ta_val = _norm(
                row.get(f"{field}_ta", row.get(field)), field, "ta"
            )
            ref_val = _norm(
                row.get(f"{field}_ref", row.get(field)), field, "123"
            )
            if ta_val != ref_val:
                mismatches.append({
                    "ts_code": ts,
                    "bucket": bucket,
                    "field": field,
                    "ta": ta_val,
                    "ref": ref_val,
                    "freq": "daily",
                })
        for field in B4_HARD_WEEKLY_FIELDS:
            if skip_dde and field in DDE_FIELDS:
                continue
            w = weekly_field_name(field)
            ta_val = _norm(row.get(f"{w}_ta", row.get(w)), w, "ta")
            ref_val = _norm(row.get(f"{w}_ref", row.get(w)), w, "123")
            if ta_val != ref_val:
                mismatches.append({
                    "ts_code": ts,
                    "bucket": bucket,
                    "field": w,
                    "ta": ta_val,
                    "ref": ref_val,
                    "freq": "weekly",
                })
    return mismatches
