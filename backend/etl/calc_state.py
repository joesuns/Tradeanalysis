"""Read/write helpers for dws_calc_state (append-only calc routing)."""
from typing import Optional, List, Dict

import pandas as pd

from backend.etl.calc_router import state_signature


def load_calc_state(con, freq: str, indicator: str, ts_codes: List[str]) -> Dict[str, dict]:
    """Return {ts_code: {last_trade_date, history_fp, quote_latest_adj}} for (freq, indicator)."""
    if not ts_codes:
        return {}
    ph = ",".join(["?"] * len(ts_codes))
    rows = con.execute(f"""
        SELECT ts_code, last_trade_date, history_fp, quote_latest_adj, updated_calc_date
        FROM dws_calc_state
        WHERE freq = ? AND indicator = ? AND ts_code IN ({ph})
    """, [freq, indicator] + list(ts_codes)).fetchall()
    return {
        r[0]: {
            "last_trade_date": r[1],
            "history_fp": r[2],
            "quote_latest_adj": r[3],
            "updated_calc_date": r[4],
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
               quote_latest_adj, updated_calc_date
        FROM dws_calc_state
        WHERE ts_code IN ({ph})
    """, list(ts_codes)).fetchall()
    return {
        (r[0], r[1], r[2]): {
            "last_trade_date": r[3],
            "history_fp": r[4],
            "quote_latest_adj": r[5],
            "updated_calc_date": r[6],
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


def write_calc_state_from_df(
    con,
    ts_code: str,
    freq: str,
    indicator: str,
    df: Optional[pd.DataFrame],
    sig_cols: list,
    calc_date: str,
    last_trade_date: Optional[str] = None,
) -> bool:
    """Persist routing state from a loaded tail/window frame (SKIP/APPEND/FULL)."""
    if df is None or df.empty:
        return False
    anchor = last_trade_date or str(df["trade_date"].max())
    fp = state_signature(df, anchor, sig_cols)
    upsert_calc_state(
        con, ts_code, freq, indicator,
        last_trade_date=anchor, history_fp=fp, calc_date=calc_date,
    )
    return True


def invalidate_weekly_calc_state(con) -> int:
    """Drop weekly routing state after repair-weekly (forces one FULL weekly pass)."""
    before = con.execute("SELECT COUNT(*) FROM dws_calc_state WHERE freq = 'weekly'").fetchone()[0]
    con.execute("DELETE FROM dws_calc_state WHERE freq = 'weekly'")
    return before
