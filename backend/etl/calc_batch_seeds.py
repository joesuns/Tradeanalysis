"""Batch load EMA/state seeds for cross-stock APPEND."""
from typing import Dict, List, Optional, Tuple


def load_ema_seeds_batch(
    con,
    dws_table: str,
    ts_codes: List[str],
    before_trade_date: str,
    seed_cols: Tuple[str, ...],
) -> Dict[str, dict]:
    """Return {ts_code: {col: value}} for the latest DWS row with trade_date < before_trade_date."""
    if not ts_codes:
        return {}
    ph = ",".join(["?"] * len(ts_codes))
    cols = ", ".join(seed_cols)
    rows = con.execute(f"""
        SELECT ts_code, {cols} FROM (
            SELECT ts_code, {cols},
                   ROW_NUMBER() OVER (
                       PARTITION BY ts_code ORDER BY calc_date DESC, trade_date DESC
                   ) AS rn
            FROM {dws_table}
            WHERE ts_code IN ({ph}) AND trade_date < ?
        ) WHERE rn = 1
    """, list(ts_codes) + [before_trade_date]).fetchall()
    out = {}
    for row in rows:
        code = row[0]
        out[code] = {seed_cols[i]: row[i + 1] for i in range(len(seed_cols))}
    return out


def load_zone_seeds_batch(
    con,
    dws_table: str,
    ts_codes: List[str],
    before_trade_date: str,
) -> Dict[str, Optional[str]]:
    """Return {ts_code: zone} for latest DWS bar with trade_date < before_trade_date."""
    if not ts_codes:
        return {}
    ph = ",".join(["?"] * len(ts_codes))
    rows = con.execute(f"""
        SELECT ts_code, zone FROM (
            SELECT ts_code, zone,
                   ROW_NUMBER() OVER (
                       PARTITION BY ts_code
                       ORDER BY trade_date DESC, calc_date DESC
                   ) AS rn
            FROM {dws_table}
            WHERE ts_code IN ({ph}) AND trade_date < ?
              AND zone IS NOT NULL
        ) WHERE rn = 1
    """, list(ts_codes) + [before_trade_date]).fetchall()
    return {r[0]: r[1] for r in rows}
