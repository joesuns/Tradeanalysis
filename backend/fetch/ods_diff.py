"""Compare incoming ODS rows with DB — write only changed PKs."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple
import math

FLOAT_ABS_TOL = 1e-4  # price / rate fields (yuan, %)
FLOAT_LARGE_ABS_TOL = 1.0  # vol / amount / mv (hands, 万元)
FLOAT_RTOL = 1e-5

# (ts_code, trade_date, ods_table, column, is_insert)
ChangedFieldEvent = Tuple[str, str, str, str, bool]

ODS_DAILY_DIFF_COLS = (
    "open", "high", "low", "close", "vol", "amount", "pct_chg", "adj_factor",
)
ODS_DAILY_BASIC_DIFF_COLS = (
    "total_mv", "circ_mv", "pe_ttm", "turnover_rate", "volume_ratio",
)
ODS_MONEYFLOW_DIFF_COLS = (
    "buy_sm_vol", "buy_sm_amount", "sell_sm_vol", "sell_sm_amount",
    "buy_md_vol", "buy_md_amount", "sell_md_vol", "sell_md_amount",
    "buy_lg_vol", "buy_lg_amount", "sell_lg_vol", "sell_lg_amount",
    "buy_elg_vol", "buy_elg_amount", "sell_elg_vol", "sell_elg_amount",
    "net_mf_vol", "net_mf_amount", "net_amount_dc",
)


def _is_missing(v) -> bool:
    if v is None:
        return True
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def values_equal(a, b, atol: Optional[float] = None) -> bool:
    """Compare ODS field values; tolerate DuckDB float32 vs tushare API roundtrip."""
    if _is_missing(a) and _is_missing(b):
        return True
    if _is_missing(a) or _is_missing(b):
        return False
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    diff = abs(fa - fb)
    if diff <= 1e-12:
        return True
    if atol is not None:
        return diff <= atol
    scale = max(abs(fa), abs(fb), 1.0)
    if scale < 1000:
        return diff <= FLOAT_ABS_TOL
    return diff <= max(FLOAT_LARGE_ABS_TOL, scale * FLOAT_RTOL)


def row_differs(incoming: dict, existing: dict, cols: Sequence[str]) -> bool:
    for col in cols:
        if not values_equal(incoming.get(col), existing.get(col)):
            return True
    return False


def _load_existing_map(
    con,
    table: str,
    cols: Sequence[str],
    trade_date: str,
    ts_codes: Optional[Iterable[str]] = None,
) -> Dict[str, dict]:
    select_cols = ", ".join(["ts_code"] + list(cols))
    sql = f"SELECT {select_cols} FROM {table} WHERE trade_date = ?"
    params: list = [trade_date]
    if ts_codes is not None:
        codes = list(ts_codes)
        if not codes:
            return {}
        placeholders = ",".join(["?"] * len(codes))
        sql += f" AND ts_code IN ({placeholders})"
        params.extend(codes)
    rows = con.execute(sql, params).fetchall()
    col_names = ["ts_code"] + list(cols)
    return {r[0]: dict(zip(col_names, r)) for r in rows}


def diff_changed_columns(
    incoming: dict,
    existing: Optional[dict],
    cols: Sequence[str],
) -> List[str]:
    """Return column names that differ; INSERT (no existing) → all diff cols."""
    if existing is None:
        return list(cols)
    return [
        col for col in cols
        if not values_equal(incoming.get(col), existing.get(col))
    ]


def _field_events_for_row(
    row: dict,
    existing: Optional[dict],
    table: str,
    diff_cols: Sequence[str],
) -> List[ChangedFieldEvent]:
    ts_code = row["ts_code"]
    trade_date = row["trade_date"]
    is_insert = existing is None
    changed_cols = diff_changed_columns(row, existing, diff_cols)
    if not changed_cols and not is_insert:
        return []
    if is_insert:
        changed_cols = list(diff_cols)
    return [
        (ts_code, trade_date, table, col, is_insert)
        for col in changed_cols
    ]


def partition_changed_rows_detailed(
    con,
    table: str,
    diff_cols: Sequence[str],
    rows: List[dict],
    trade_date: Optional[str] = None,
) -> Tuple[List[dict], int, List[ChangedFieldEvent]]:
    """Return (rows_to_write, unchanged_count, column-level change events)."""
    if not rows:
        return [], 0, []
    td = trade_date or rows[0]["trade_date"]
    codes = [r["ts_code"] for r in rows]
    existing = _load_existing_map(con, table, diff_cols, td, codes)
    changed: List[dict] = []
    events: List[ChangedFieldEvent] = []
    unchanged = 0
    for row in rows:
        ex = existing.get(row["ts_code"])
        row_events = _field_events_for_row(row, ex, table, diff_cols)
        if row_events:
            changed.append(row)
            events.extend(row_events)
        else:
            unchanged += 1
    return changed, unchanged, events


def partition_changed_rows(
    con,
    table: str,
    diff_cols: Sequence[str],
    rows: List[dict],
    trade_date: Optional[str] = None,
) -> Tuple[List[dict], int]:
    """Return (rows_to_write, unchanged_count) for one ODS table batch."""
    changed, unchanged, _ = partition_changed_rows_detailed(
        con, table, diff_cols, rows, trade_date=trade_date,
    )
    return changed, unchanged


def partition_changed_daily(con, rows: List[dict]) -> Tuple[List[dict], int]:
    return partition_changed_rows(con, "ods_daily", ODS_DAILY_DIFF_COLS, rows)


def partition_changed_daily_basic(con, rows: List[dict]) -> Tuple[List[dict], int]:
    return partition_changed_rows(
        con, "ods_daily_basic", ODS_DAILY_BASIC_DIFF_COLS, rows,
    )


def partition_changed_moneyflow(con, rows: List[dict]) -> Tuple[List[dict], int]:
    return partition_changed_rows(
        con, "ods_moneyflow", ODS_MONEYFLOW_DIFF_COLS, rows,
    )
