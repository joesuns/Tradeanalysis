"""Read/write helpers for dws_calc_state (append-only calc routing)."""
from typing import Optional, List, Dict

import pandas as pd

from backend.config import CALC_SKIP_STATE_REFRESH
from backend.etl.calc_router import state_signature


def load_calc_state(con, freq: str, indicator: str, ts_codes: List[str]) -> Dict[str, dict]:
    """Return {ts_code: {last_trade_date, history_fp, quote_latest_adj}} for (freq, indicator)."""
    if not ts_codes:
        return {}
    ph = ",".join(["?"] * len(ts_codes))
    rows = con.execute(f"""
        SELECT ts_code, last_trade_date, history_fp, quote_latest_adj,
               spec_version, updated_calc_date
        FROM dws_calc_state
        WHERE freq = ? AND indicator = ? AND ts_code IN ({ph})
    """, [freq, indicator] + list(ts_codes)).fetchall()
    return {
        r[0]: {
            "last_trade_date": r[1],
            "history_fp": r[2],
            "quote_latest_adj": r[3],
            "spec_version": r[4] or "v1",
            "updated_calc_date": r[5],
        }
        for r in rows
    }


def load_calc_state_batch(con, ts_codes: List[str]) -> Dict[tuple, dict]:
    """Return {(ts_code, freq, indicator): state_dict} for all states in chunk."""
    if not ts_codes:
        return {}
    ph = ",".join(["?"] * len(ts_codes))
    rows = con.execute(f"""
        SELECT ts_code, freq, indicator, last_trade_date, history_fp,
               quote_latest_adj, spec_version, updated_calc_date
        FROM dws_calc_state
        WHERE ts_code IN ({ph})
    """, list(ts_codes)).fetchall()
    return {
        (r[0], r[1], r[2]): {
            "last_trade_date": r[3],
            "history_fp": r[4],
            "quote_latest_adj": r[5],
            "spec_version": r[6] or "v1",
            "updated_calc_date": r[7],
        }
        for r in rows
    }


def upsert_calc_state(con, ts_code: str, freq: str, indicator: str,
                      last_trade_date: str, history_fp: str, calc_date: str,
                      quote_latest_adj: Optional[float] = None,
                      spec_version: str = "v1"):
    """Insert-or-replace one (ts_code, freq, indicator) state row."""
    con.execute("""
        INSERT OR REPLACE INTO dws_calc_state
            (ts_code, freq, indicator, last_trade_date, history_fp, quote_latest_adj,
             spec_version, updated_calc_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [ts_code, freq, indicator, last_trade_date, history_fp, quote_latest_adj,
          spec_version, calc_date])


def should_refresh_calc_state(
    state: Optional[dict],
    calc_date: str,
    new_history_fp: str,
) -> bool:
    """Skip UPSERT when same calc_date run already has identical history_fp."""
    if state is None:
        return True
    if state.get("updated_calc_date") == calc_date and state.get("history_fp") == new_history_fp:
        return False
    return True


def upsert_calc_state_batch(con, records: list) -> int:
    """Bulk INSERT OR REPLACE into dws_calc_state.

    records: 8-tuples
    (ts_code, freq, indicator, last_trade_date, history_fp, calc_date,
     quote_latest_adj, spec_version). Legacy 7-tuples default spec_version to v1.
    """
    if not records:
        return 0
    normalized = []
    for rec in records:
        if len(rec) >= 8:
            normalized.append(rec[:8])
        else:
            normalized.append((*rec, "v1"))
    df = pd.DataFrame(normalized, columns=[
        "ts_code", "freq", "indicator", "last_trade_date", "history_fp",
        "updated_calc_date", "quote_latest_adj", "spec_version",
    ])
    con.register("_calc_state_batch", df)
    con.execute("""
        INSERT OR REPLACE INTO dws_calc_state
            (ts_code, freq, indicator, last_trade_date, history_fp, quote_latest_adj,
             spec_version, updated_calc_date)
        SELECT ts_code, freq, indicator, last_trade_date, history_fp, quote_latest_adj,
               spec_version, updated_calc_date
        FROM _calc_state_batch
    """)
    con.unregister("_calc_state_batch")
    return len(df)


def build_append_state_records(
    stock_rows: list,
    freq: str,
    indicator: str,
    calc_date: str,
    spec_version: str = "v1",
) -> list:
    """Build dws_calc_state upsert tuples from batch APPEND/FULL stock_rows."""
    records = []
    for ts_code, out, fp, _w0, _w1 in stock_rows:
        if out is None:
            continue
        if hasattr(out, "empty") and out.empty:
            continue
        anchor = str(out["trade_date"].max())
        records.append((
            ts_code, freq, indicator, anchor, fp,
            calc_date, None, spec_version,
        ))
    return records


def write_calc_state_from_df(
    con,
    ts_code: str,
    freq: str,
    indicator: str,
    df: Optional[pd.DataFrame],
    sig_cols: list,
    calc_date: str,
    last_trade_date: Optional[str] = None,
    spec_version: str = "v1",
) -> bool:
    """Persist routing state from a loaded tail/window frame (SKIP/APPEND/FULL)."""
    if df is None or df.empty:
        return False
    anchor = last_trade_date or str(df["trade_date"].max())
    fp = state_signature(df, anchor, sig_cols)
    if CALC_SKIP_STATE_REFRESH:
        existing = load_calc_state(con, freq, indicator, [ts_code]).get(ts_code)
        if not should_refresh_calc_state(existing, calc_date, fp):
            return False
    upsert_calc_state(
        con, ts_code, freq, indicator,
        last_trade_date=anchor, history_fp=fp, calc_date=calc_date,
        spec_version=spec_version,
    )
    return True


def invalidate_weekly_calc_state(con) -> int:
    """Drop weekly routing state after repair-weekly (forces one FULL weekly pass)."""
    before = con.execute("SELECT COUNT(*) FROM dws_calc_state WHERE freq = 'weekly'").fetchone()[0]
    con.execute("DELETE FROM dws_calc_state WHERE freq = 'weekly'")
    return before
