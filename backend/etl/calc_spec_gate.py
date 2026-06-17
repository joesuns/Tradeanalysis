"""Spec-version staleness detection for calc routing and data-quality gates.

Two detection domains (do not conflate):

- **Routing domain:** ``dws_calc_state.spec_version`` and per-stock global-latest
  DWS row (``load_latest_spec_versions`` in base.py) — used by calc APPEND routing.
- **Analysis/export domain:** ``v_dws_*_latest`` at an anchor ``trade_date`` (daily bar
  or weekly week-end) — matches Excel / ``v_ads_analysis_wide_*`` consumption.
"""
from typing import Dict, List, Optional, Set, Tuple

import duckdb

from backend.etl.calc_indicators import (
    CALC_ROUTE_SPECS,
    dws_latest_view,
)


def resolve_weekly_anchor_trade_date(con, trade_date: str) -> Optional[str]:
    """Latest week-end bar with trade_date <= anchor (matches export weekly load)."""
    row = con.execute(
        """
        SELECT MAX(trade_date) FROM dim_date
        WHERE trade_date <= ? AND is_week_end = 1
        """,
        [trade_date],
    ).fetchone()
    return row[0] if row and row[0] else None


def _anchor_trade_date_for_freq(con, freq: str, trade_date: str) -> Optional[str]:
    if freq == "daily":
        return trade_date
    return resolve_weekly_anchor_trade_date(con, trade_date)


def count_spec_stale_by_indicator(con) -> Dict[str, int]:
    """Count dws_calc_state rows whose spec_version lags code expectation."""
    counts: Dict[str, int] = {}
    for indicator_name, freq, CalcCls, _, _ in CALC_ROUTE_SPECS:
        expected = getattr(CalcCls, "SPEC_VERSION", "v1")
        row = con.execute(
            """
            SELECT COUNT(*) FROM dws_calc_state
            WHERE indicator = ? AND freq = ?
              AND COALESCE(spec_version, 'v1') <> ?
            """,
            [indicator_name, freq, expected],
        ).fetchone()
        counts[f"{indicator_name}_{freq}"] = int(row[0] or 0)
    return counts


def _exists_stale_on_view(
    con,
    view_name: str,
    anchor_trade_date: str,
    expected: str,
    ts_codes: Optional[List[str]] = None,
) -> bool:
    sql = f"""
        SELECT 1 FROM {view_name}
        WHERE trade_date = ?
          AND COALESCE(spec_version, 'v1') <> ?
    """
    params: list = [anchor_trade_date, expected]
    if ts_codes:
        placeholders = ",".join("?" * len(ts_codes))
        sql += f" AND ts_code IN ({placeholders})"
        params.extend(ts_codes)
    sql += " LIMIT 1"
    try:
        row = con.execute(sql, params).fetchone()
    except duckdb.CatalogException:
        return False
    return row is not None


def has_spec_stale_on_trade_date(
    con,
    trade_date: str,
    ts_codes: Optional[List[str]] = None,
) -> bool:
    """Fast EXISTS on v_*_latest @ anchor trade_date (export/analysis domain)."""
    for indicator_name, freq, CalcCls, _, _ in CALC_ROUTE_SPECS:
        anchor = _anchor_trade_date_for_freq(con, freq, trade_date)
        if not anchor:
            continue
        expected = getattr(CalcCls, "SPEC_VERSION", "v1")
        view = dws_latest_view(indicator_name, freq)
        if _exists_stale_on_view(con, view, anchor, expected, ts_codes):
            return True
    return False


def count_dws_spec_stale_on_trade_date(
    con,
    trade_date: str,
    ts_codes: Optional[List[str]] = None,
) -> Dict[str, int]:
    """Count stale rows on v_*_latest @ anchor trade_date (scoped, no full-table scan)."""
    counts: Dict[str, int] = {}
    for indicator_name, freq, CalcCls, _, _ in CALC_ROUTE_SPECS:
        anchor = _anchor_trade_date_for_freq(con, freq, trade_date)
        key = f"{indicator_name}_{freq}"
        if not anchor:
            counts[key] = 0
            continue
        expected = getattr(CalcCls, "SPEC_VERSION", "v1")
        view = dws_latest_view(indicator_name, freq)
        sql = f"""
            SELECT COUNT(*) FROM {view}
            WHERE trade_date = ?
              AND COALESCE(spec_version, 'v1') <> ?
        """
        params: list = [anchor, expected]
        if ts_codes:
            placeholders = ",".join("?" * len(ts_codes))
            sql += f" AND ts_code IN ({placeholders})"
            params.extend(ts_codes)
        try:
            row = con.execute(sql, params).fetchone()
        except duckdb.CatalogException:
            row = (0,)
        counts[key] = int(row[0] or 0)
    return counts


def has_spec_stale_indicators(
    con,
    trade_date: Optional[str] = None,
    ts_codes: Optional[List[str]] = None,
) -> bool:
    """True when spec lags code expectation.

    With ``trade_date``: routing state stale OR analysis-section stale (fast EXISTS).
    Without ``trade_date``: routing state only (lightweight export/health pre-check).
    """
    state_counts = count_spec_stale_by_indicator(con)
    if any(n > 0 for n in state_counts.values()):
        return True
    if trade_date:
        return has_spec_stale_on_trade_date(con, trade_date, ts_codes)
    return False


def find_spec_stale_codes(
    con,
    indicator_names: Optional[List[str]] = None,
    ts_codes: Optional[List[str]] = None,
) -> Dict[Tuple[str, str], List[str]]:
    """Return {(indicator, freq): [ts_codes]} with stale dws_calc_state spec."""
    want = set(indicator_names) if indicator_names else None
    groups: Dict[Tuple[str, str], List[str]] = {}
    for indicator_name, freq, CalcCls, _, _ in CALC_ROUTE_SPECS:
        if want is not None and indicator_name not in want:
            continue
        expected = getattr(CalcCls, "SPEC_VERSION", "v1")
        sql = """
            SELECT ts_code FROM dws_calc_state
            WHERE indicator = ? AND freq = ?
              AND COALESCE(spec_version, 'v1') <> ?
        """
        params: list = [indicator_name, freq, expected]
        if ts_codes:
            placeholders = ",".join("?" * len(ts_codes))
            sql += f" AND ts_code IN ({placeholders})"
            params.extend(ts_codes)
        rows = con.execute(sql, params).fetchall()
        if rows:
            groups[(indicator_name, freq)] = [r[0] for r in rows]
    return groups


def find_dws_spec_stale_codes(
    con,
    indicator_names: Optional[List[str]] = None,
    ts_codes: Optional[List[str]] = None,
    trade_date: Optional[str] = None,
) -> Dict[Tuple[str, str], List[str]]:
    """Stale spec on v_*_latest at anchor trade_date (analysis/export domain)."""
    want = set(indicator_names) if indicator_names else None
    groups: Dict[Tuple[str, str], List[str]] = {}
    for indicator_name, freq, CalcCls, _, _ in CALC_ROUTE_SPECS:
        if want is not None and indicator_name not in want:
            continue
        expected = getattr(CalcCls, "SPEC_VERSION", "v1")
        if trade_date:
            anchor = _anchor_trade_date_for_freq(con, freq, trade_date)
            if not anchor:
                continue
            view = dws_latest_view(indicator_name, freq)
            sql = f"""
                SELECT ts_code FROM {view}
                WHERE trade_date = ?
                  AND COALESCE(spec_version, 'v1') <> ?
            """
            params: list = [anchor, expected]
            if ts_codes:
                placeholders = ",".join("?" * len(ts_codes))
                sql += f" AND ts_code IN ({placeholders})"
                params.extend(ts_codes)
            try:
                rows = con.execute(sql, params).fetchall()
            except duckdb.CatalogException:
                rows = []
            stale = [r[0] for r in rows]
        else:
            from backend.etl.base import load_latest_spec_versions
            from backend.etl.calc_indicators import INDICATOR_DWS_PREFIX

            prefix = INDICATOR_DWS_PREFIX.get(indicator_name, indicator_name)
            table = f"dws_{prefix}_{freq}"
            if ts_codes:
                latest = load_latest_spec_versions(con, table, ts_codes)
                stale = [c for c, sv in latest.items() if (sv or "v1") != expected]
            else:
                stale = []
        if stale:
            groups[(indicator_name, freq)] = sorted(set(stale))
    return groups


def find_spec_stale_codes_merged(
    con,
    indicator_names: Optional[List[str]] = None,
    ts_codes: Optional[List[str]] = None,
    trade_date: Optional[str] = None,
    indicator_filter: Optional[List[str]] = None,
) -> Dict[Tuple[str, str], List[str]]:
    """Union of state-stale and DWS-section-stale ts_codes per (indicator, freq).

    When ``indicator_filter`` is set (Wave5): refresh indicators in
    ``indicator_filter ∪ {state-stale indicators}`` only.
    """
    state_groups = find_spec_stale_codes(con, None, ts_codes)
    if indicator_filter is not None:
        allowed = set(indicator_filter) | {ind for ind, _ in state_groups}
        state_groups = {
            k: v for k, v in state_groups.items() if k[0] in allowed
        }
        dws_indicator_names = sorted(allowed) if allowed else list(indicator_filter)
    else:
        dws_indicator_names = indicator_names

    dws_groups = find_dws_spec_stale_codes(
        con, dws_indicator_names, ts_codes, trade_date=trade_date,
    )
    if indicator_names is not None:
        want = set(indicator_names)
        state_groups = {k: v for k, v in state_groups.items() if k[0] in want}
        dws_groups = {k: v for k, v in dws_groups.items() if k[0] in want}

    merged: Dict[Tuple[str, str], Set[str]] = {}
    for key, codes in state_groups.items():
        merged.setdefault(key, set()).update(codes)
    for key, codes in dws_groups.items():
        merged.setdefault(key, set()).update(codes)
    return {k: sorted(v) for k, v in merged.items() if v}


def export_spec_freshness_warnings(
    con,
    trade_date: Optional[str] = None,
) -> List[str]:
    """Human-readable warnings when spec is stale."""
    msgs: List[str] = []
    state = {k: v for k, v in count_spec_stale_by_indicator(con).items() if v > 0}
    if state:
        msgs.append(f"dws_calc_state spec stale counts: {state}")
    if trade_date:
        dws = {
            k: v for k, v in count_dws_spec_stale_on_trade_date(con, trade_date).items()
            if v > 0
        }
        if dws:
            msgs.append(
                f"DWS section spec stale @ {trade_date} (counts): {dws}"
            )
    return msgs
