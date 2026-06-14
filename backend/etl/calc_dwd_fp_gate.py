"""Downgrade spurious FULL routing when DWS input fingerprint is unchanged."""
from typing import Dict, List, Optional, Tuple

Key = Tuple[str, str]  # (indicator_name, freq)


def build_dwd_fp_cache(con, codes: List[str], calc_date: str) -> dict:
    """Preload latest DWS input fingerprints per (indicator, freq) for gate checks."""
    from backend.config import CALC_DWD_FP_GATE
    from backend.etl.base import load_latest_fingerprints, load_latest_spec_versions
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS
    from backend.etl.orchestrator import resolve_recalc_start

    if not CALC_DWD_FP_GATE or not codes:
        return {}

    daily_recalc = resolve_recalc_start(con, calc_date, "daily")
    weekly_recalc = resolve_recalc_start(con, calc_date, "weekly")
    cache: Dict[Key, dict] = {}
    seen: Dict[Key, type] = {}

    for indicator_name, freq, CalcCls, _sig_cols, _source in CALC_ROUTE_SPECS:
        key = (indicator_name, freq)
        if key in seen:
            continue
        seen[key] = CalcCls
        calc = CalcCls(con, freq)
        spec_ver = getattr(CalcCls, "SPEC_VERSION", "v1")
        check_spec = spec_ver != "v1"
        entry = {
            "recalc_start": daily_recalc if freq == "daily" else weekly_recalc,
            "latest_fps": load_latest_fingerprints(con, calc.dws_table, codes),
            "spec_version": spec_ver,
        }
        if check_spec:
            entry["latest_specs"] = load_latest_spec_versions(con, calc.dws_table, codes)
        else:
            entry["latest_specs"] = None
        cache[key] = entry
    return cache


def apply_dwd_fp_gate(
    mode: str,
    new_bars: list,
    cur_fp: Optional[str],
    *,
    con,
    ts_code: str,
    CalcCls,
    freq: str,
    df,
    dwd_fp_cache: Optional[dict],
    indicator_name: str,
) -> Tuple[str, list, Optional[str]]:
    """If history_fp says FULL but DWS input unchanged and no new bars → SKIP."""
    from backend.config import CALC_DWD_FP_GATE
    from backend.etl.base import check_dwd_unchanged

    if not CALC_DWD_FP_GATE:
        return mode, new_bars, cur_fp
    if mode != "FULL" or new_bars or cur_fp is None or con is None or df is None:
        return mode, new_bars, cur_fp
    if dwd_fp_cache is None:
        return mode, new_bars, cur_fp

    entry = dwd_fp_cache.get((indicator_name, freq))
    if entry is None:
        return mode, new_bars, cur_fp

    calc = CalcCls(con, freq)
    unchanged_kwargs = {
        "latest_fps": entry["latest_fps"],
        "recalc_start": entry["recalc_start"],
        "expected_spec_version": entry["spec_version"],
    }
    if entry.get("latest_specs") is not None:
        unchanged_kwargs["latest_specs"] = entry["latest_specs"]

    if check_dwd_unchanged(con, calc.dws_table, ts_code, df, **unchanged_kwargs):
        return "SKIP", [], cur_fp
    return mode, new_bars, cur_fp
