"""Read/write helpers for dws_calc_state (append-only calc routing)."""
from typing import Optional, List, Dict


def load_calc_state(con, freq: str, indicator: str, ts_codes: List[str]) -> Dict[str, dict]:
    """Return {ts_code: {last_trade_date, history_fp, quote_latest_adj}} for (freq, indicator)."""
    if not ts_codes:
        return {}
    ph = ",".join(["?"] * len(ts_codes))
    rows = con.execute(f"""
        SELECT ts_code, last_trade_date, history_fp, quote_latest_adj
        FROM dws_calc_state
        WHERE freq = ? AND indicator = ? AND ts_code IN ({ph})
    """, [freq, indicator] + list(ts_codes)).fetchall()
    return {
        r[0]: {"last_trade_date": r[1], "history_fp": r[2], "quote_latest_adj": r[3]}
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
