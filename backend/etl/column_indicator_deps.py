"""Map ODS column change events to calc indicator names (Wave 5 run-path narrowing)."""
from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from backend.fetch.fetch_result import FetchResult
from backend.fetch.ods_diff import (
    ODS_DAILY_BASIC_DIFF_COLS,
    ODS_DAILY_DIFF_COLS,
    ODS_MONEYFLOW_DIFF_COLS,
)

# (ts_code, trade_date, ods_table, column, is_insert)
ChangedFieldEvent = Tuple[str, str, str, str, bool]

# P0: ODS column patches that change DDE trend inputs without new quote bars.
DDE_PATCH_TABLE_COLUMNS = frozenset({
    ("ods_moneyflow", "net_amount_dc"),
    ("ods_daily_basic", "circ_mv"),
})

QUOTE_INDICATORS = frozenset({
    "macd", "ma", "kpattern", "volume", "priceposition",
})
ALL_INDICATORS = frozenset({
    "macd", "ma", "kpattern", "volume", "priceposition", "dde",
})

_CLOSE_INDICATORS = frozenset({
    "macd", "ma", "kpattern", "volume", "priceposition", "dde",
})


def _build_ods_column_map() -> Dict[Tuple[str, str], FrozenSet[str]]:
    mapping: Dict[Tuple[str, str], FrozenSet[str]] = {
        ("ods_daily", "open"): frozenset({"kpattern"}),
        ("ods_daily", "high"): frozenset({"kpattern"}),
        ("ods_daily", "low"): frozenset({"kpattern"}),
        ("ods_daily", "close"): _CLOSE_INDICATORS,
        ("ods_daily", "vol"): frozenset({"kpattern", "volume"}),
        ("ods_daily", "pct_chg"): frozenset({"kpattern"}),
        ("ods_daily", "amount"): frozenset(),
        ("ods_daily", "adj_factor"): frozenset(),  # G4 handled in resolver
        ("ods_daily_basic", "circ_mv"): frozenset({"dde"}),
        ("ods_daily_basic", "total_mv"): frozenset(),
        ("ods_daily_basic", "pe_ttm"): frozenset(),
        ("ods_daily_basic", "turnover_rate"): frozenset(),
        ("ods_daily_basic", "volume_ratio"): frozenset(),
    }
    for col in ODS_MONEYFLOW_DIFF_COLS:
        mapping[("ods_moneyflow", col)] = frozenset({"dde"})
    # Sanity: every diff col in ods_diff modules is registered.
    for col in ODS_DAILY_DIFF_COLS:
        assert ("ods_daily", col) in mapping, col
    for col in ODS_DAILY_BASIC_DIFF_COLS:
        assert ("ods_daily_basic", col) in mapping, col
    return mapping


ODS_COLUMN_TO_INDICATORS = _build_ods_column_map()


def dde_patch_ts_codes(events: Sequence[ChangedFieldEvent]) -> List[str]:
    """Return sorted unique ts_codes with net_amount_dc or circ_mv ODS patch events."""
    codes: Set[str] = set()
    for ts_code, _td, table, col, _ins in events:
        if (table, col) in DDE_PATCH_TABLE_COLUMNS:
            codes.add(ts_code)
    return sorted(codes)


def resolve_affected_indicators(
    events: Sequence[ChangedFieldEvent],
) -> Optional[Set[str]]:
    """Return affected indicator names, or None when narrowing must not apply."""
    if not events:
        return None
    if any(ev[3] == "adj_factor" for ev in events):
        return None
    if any(ev[2] == "ods_daily" and ev[4] for ev in events):
        return None
    out: Set[str] = set()
    for _code, _td, table, col, _ins in events:
        out |= set(ODS_COLUMN_TO_INDICATORS.get((table, col), frozenset()))
    if not out:
        return None
    if out == ALL_INDICATORS:
        return None
    return out


def _spec_stale_indicator_names(con) -> Set[str]:
    from backend.etl.calc_spec_gate import count_spec_stale_by_indicator

    stale: Set[str] = set()
    for key, count in count_spec_stale_by_indicator(con).items():
        if count <= 0:
            continue
        ind, _freq = key.rsplit("_", 1)
        stale.add(ind)
    return stale


def resolve_run_calc_indicator_filter(
    con,
    fetch_result: FetchResult,
    *,
    changed_codes: List[str],
    stale_extra_codes: List[str],
    qfq_codes: List[str],
    force: bool = False,
) -> Optional[List[str]]:
    """Resolve run-path indicator filter; None = all 12 CALC routes (no narrow)."""
    from backend.config import CALC_COLUMN_NARROW

    _ = changed_codes  # reserved for future per-stock narrowing
    if not CALC_COLUMN_NARROW or force:
        return None
    if fetch_result.rows_written <= 0:
        return None
    if qfq_codes or stale_extra_codes:
        return None
    col_inds = resolve_affected_indicators(fetch_result.changed_field_events)
    if col_inds is None:
        return None
    merged = set(col_inds) | _spec_stale_indicator_names(con)
    if merged == ALL_INDICATORS:
        return None
    return sorted(merged)


def calc_routes_narrowed(indicator_filter: Optional[List[str]]) -> bool:
    return indicator_filter is not None and len(indicator_filter) < len(ALL_INDICATORS)


def needs_quote_tails(indicator_filter: Optional[List[str]]) -> bool:
    if indicator_filter is None:
        return True
    return bool(set(indicator_filter) & QUOTE_INDICATORS)


def needs_dde_tails(indicator_filter: Optional[List[str]]) -> bool:
    if indicator_filter is None:
        return True
    return "dde" in indicator_filter


def active_route_specs(indicator_filter: Optional[List[str]] = None):
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS
    from backend.etl.refresh_pipeline import resolve_refresh_routes

    if indicator_filter is None:
        return list(CALC_ROUTE_SPECS)
    active = set(resolve_refresh_routes(indicator_filter))
    return [
        spec for spec in CALC_ROUTE_SPECS
        if (spec[0], spec[1]) in active
    ]


def active_route_keys(indicator_filter: Optional[List[str]] = None) -> Optional[List[str]]:
    if indicator_filter is None:
        return None
    from backend.etl.refresh_pipeline import resolve_refresh_routes

    return [f"{ind}_{freq}" for ind, freq in resolve_refresh_routes(indicator_filter)]
