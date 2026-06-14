"""Spec-version staleness detection for calc routing and data-quality gates."""
from typing import Dict, List, Optional, Tuple

from backend.etl.calc_indicators import CALC_ROUTE_SPECS, INDICATOR_SPEC_VERSIONS


def count_spec_stale_by_indicator(con) -> Dict[str, int]:
    """Count dws_calc_state rows whose spec_version lags code expectation.

    Returns keys like ``ma_daily``, ``volume_weekly``.
    """
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
        key = f"{indicator_name}_{freq}"
        counts[key] = int(row[0] or 0)
    return counts


def has_spec_stale_indicators(con) -> bool:
    """True when any routing state row lags Calculator.SPEC_VERSION."""
    counts = count_spec_stale_by_indicator(con)
    return any(n > 0 for n in counts.values())


def find_spec_stale_codes(
    con,
    indicator_names: Optional[List[str]] = None,
    ts_codes: Optional[List[str]] = None,
) -> Dict[Tuple[str, str], List[str]]:
    """Return {(indicator, freq): [ts_codes]} needing spec refresh."""
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
