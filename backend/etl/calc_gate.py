"""Calc date validation against ODS tail — prevents phantom calc runs."""
import json
from typing import Any, Dict, Optional
import logging

import duckdb

logger = logging.getLogger(__name__)

_MUTATING_ETL_STEPS = (
    "cli_fetch",
    "fetch_market_data",
    "run_fetch",
    "refresh_fetch",
    "build_dwd",
    "run_rebuild_dwd",
    "refresh_rebuild_dwd",
    "run_refresh_state",
    "refresh_refresh_state",
)

_FETCH_STEPS = frozenset({
    "cli_fetch", "fetch_market_data", "run_fetch", "refresh_fetch",
})
_REBUILD_STEPS = frozenset({"build_dwd", "run_rebuild_dwd"})
_REFRESH_STATE_STEPS = frozenset({"run_refresh_state"})


def get_ods_max_trade_date(con) -> Optional[str]:
    try:
        row = con.execute("SELECT MAX(trade_date) FROM ods_daily").fetchone()
    except duckdb.CatalogException:
        return None
    return row[0] if row and row[0] else None


def resolve_effective_calc_date(con, requested: str, cap_to_ods: bool = True) -> str:
    """Return min(requested, ods_max) when cap_to_ods and ods_max exists."""
    if not cap_to_ods:
        return requested
    ods_max = get_ods_max_trade_date(con)
    if ods_max and requested > ods_max:
        logger.warning(
            "calc_date %s ahead of ods_max %s — capping to ods_max",
            requested, ods_max,
        )
        return ods_max
    return requested


def assert_calc_date_ready(con, calc_date: str, strict: bool = True) -> None:
    ods_max = get_ods_max_trade_date(con)
    if ods_max and calc_date > ods_max:
        msg = (
            f"calc_date {calc_date} > ods_max {ods_max}: "
            f"no market data for requested date. "
            f"Run fetch first or use --date {ods_max}."
        )
        if strict:
            raise ValueError(msg)
        logger.warning(msg)


def _parse_calc_completeness(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def get_last_calc_log(con, calc_date: str) -> Optional[Dict[str, Any]]:
    """Latest successful calc_dws log row for calc_date, or None."""
    row = con.execute("""
        SELECT started_at, data_completeness
        FROM ods_etl_log
        WHERE step_name = 'calc_dws' AND status = 'success'
          AND data_completeness IS NOT NULL AND data_completeness != ''
          AND (
            json_extract_string(data_completeness, '$.calc_date') = ?
            OR data_completeness LIKE ?
          )
        ORDER BY started_at DESC LIMIT 1
    """, [calc_date, f'%"calc_date": "{calc_date}"%']).fetchone()
    if not row:
        return None
    comp = _parse_calc_completeness(row[1])
    if comp.get("calc_date") != calc_date:
        return None
    return {
        "started_at": row[0],
        "data_completeness": comp,
    }


def has_prior_calc_snapshot(con, calc_date: str) -> bool:
    """True when calc_date has a prior successful ETL log or mature DWS snapshot."""
    if get_last_calc_log(con, calc_date):
        return True
    n = con.execute(
        "SELECT COUNT(DISTINCT ts_code) FROM dws_macd_daily WHERE calc_date = ?",
        [calc_date],
    ).fetchone()[0]
    return n >= 4000


def _step_caused_data_mutation(step_name: str, row_count: int, comp: Dict[str, Any]) -> bool:
    """True when an ETL log step actually changed data (not compare-only fetch)."""
    if step_name in _FETCH_STEPS:
        if "ods_rows_written" in comp or "changed_codes_count" in comp:
            written = int(comp.get("ods_rows_written") or 0)
            changed = int(comp.get("changed_codes_count") or 0)
            return written > 0 or changed > 0
        return row_count > 0

    if step_name in _REBUILD_STEPS:
        if comp.get("skipped") is True or comp.get("pipeline_shortcut") is True:
            return False
        return row_count > 0

    if step_name in _REFRESH_STATE_STEPS:
        records = int(comp.get("records_written") or 0)
        return records > 0 or row_count > 0

    return row_count > 0


def data_mutated_since_last_calc(con, calc_date: str) -> bool:
    """True when ODS tail or fetch/rebuild ran after the last calc for calc_date."""
    last = get_last_calc_log(con, calc_date)
    if not last:
        return True
    prior_ods = last["data_completeness"].get("ods_max")
    current_ods = get_ods_max_trade_date(con)
    if prior_ods and current_ods and prior_ods != current_ods:
        return True
    started_at = last["started_at"]
    placeholders = ",".join(["?"] * len(_MUTATING_ETL_STEPS))
    rows = con.execute(f"""
        SELECT step_name, row_count, data_completeness
        FROM ods_etl_log
        WHERE started_at > ? AND status = 'success'
          AND step_name IN ({placeholders})
        ORDER BY started_at
    """, [started_at, *_MUTATING_ETL_STEPS]).fetchall()
    for step_name, row_count, raw_comp in rows:
        comp = _parse_calc_completeness(raw_comp)
        if _step_caused_data_mutation(step_name, int(row_count or 0), comp):
            return True
    return False
