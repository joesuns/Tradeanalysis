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
    sig_cols: Optional[list] = None,
) -> list:
    """Build dws_calc_state upsert tuples from batch APPEND/FULL stock_rows.

    When *sig_cols* is provided, ``history_fp`` is recomputed via
    ``state_signature(out, anchor, sig_cols, 245)`` so the stored fingerprint
    matches the domain used by ``classify_calc_mode_detail`` (DWD input tail
    window).  The caller-provided ``fp`` in stock_rows is ignored for
    ``history_fp`` in that case — it remains used for the DWS
    ``input_fingerprint`` column via ``insert_dws_batch_multi``.
    """
    records = []
    for ts_code, out, fp, _w0, _w1 in stock_rows:
        if out is None:
            continue
        if hasattr(out, "empty") and out.empty:
            continue
        anchor = str(out["trade_date"].max())
        if sig_cols:
            history_fp = state_signature(out, anchor, sig_cols, 245)
        else:
            history_fp = fp
        records.append((
            ts_code, freq, indicator, anchor, history_fp,
            calc_date, None, spec_version,
        ))
    return records


def recover_calc_state_from_dws(
    con,
    ts_codes: list,
    calc_date: str,
    state_map: dict,
    sig_window: int = 245,
) -> tuple:
    """Batch-recover missing ``dws_calc_state`` from existing DWS data rows.

    For each ``(ts_code, freq, indicator)`` absent from *state_map*, checks
    whether the corresponding DWS table already has rows for *calc_date*.
    When DWS data is found the function rebuilds ``history_fp`` from the
    **DWD input tail** (not the DWS output — DWS tables lack the raw input
    columns that ``classify_calc_mode_detail`` hashes), upserts into
    ``dws_calc_state``, and patches *state_map* in-memory.

    Weekly DDE recovery is skipped — the preflight uses an aggregated
    moneyflow tail that is non-trivial to replicate here.  Those stocks
    safely fall through to FULL on the next run.

    Returns ``(recovered_count, state_map)``.  *state_map* is mutated in
    place but also returned for convenience.
    """
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS, INDICATOR_DWS_PREFIX

    # ---- Phase 1: find which (code, indicator, freq) tuples have DWS data ----
    # Build (indicator_name, freq) → (dws_table, sig_cols, source, dwd_table)
    indicator_meta: dict = {}
    for indicator_name, freq, _CalcCls, sig_cols, source in CALC_ROUTE_SPECS:
        prefix = INDICATOR_DWS_PREFIX.get(indicator_name, indicator_name)
        dws_table = f"dws_{prefix}_{freq}"
        # Map to the DWD table whose tail the preflight hashes
        if source == "quote":
            dwd_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        else:  # dde
            if freq == "weekly":
                # Skip — weekly DDE preflight uses _load_weekly aggregation
                dwd_table = None
            else:
                dwd_table = "dwd_daily_moneyflow"
        indicator_meta[(indicator_name, freq)] = (
            dws_table, sig_cols, source, dwd_table,
        )

    # Phase 2: per-indicator GROUP BY on DWS to find codes with existing data
    candidates: dict = {}  # dwd_table → [(ts_code, indicator_name, freq, sig_cols, max_td)]
    for (indicator_name, freq), (dws_table, sig_cols, source, dwd_table) \
            in indicator_meta.items():
        if dwd_table is None:
            continue  # skip weekly DDE (complex aggregation)
        missing = [
            c for c in ts_codes
            if (c, freq, indicator_name) not in state_map
        ]
        if not missing:
            continue

        ph = ",".join(["?"] * len(missing))
        rows = con.execute(f"""
            SELECT ts_code, MAX(trade_date) AS max_td
            FROM {dws_table}
            WHERE ts_code IN ({ph}) AND calc_date = ?
            GROUP BY ts_code
        """, [*missing, calc_date]).fetchall()

        for ts_code_found, max_td in rows:
            candidates.setdefault(dwd_table, []).append(
                (ts_code_found, indicator_name, freq, sig_cols, str(max_td)),
            )

    if not candidates:
        return 0, state_map

    # Phase 3: batch-load DWD tails per table and compute signatures
    recovered = 0
    for dwd_table, entries in candidates.items():
        codes_in_table = list({e[0] for e in entries})
        # Collect all needed columns: trade_date + union of sig_cols across
        # indicators that share this DWD table
        needed_cols = {"trade_date"}
        for _, _, _, sig_cols, _ in entries:
            needed_cols.update(sig_cols)
        col_list = sorted(needed_cols)

        ph = ",".join(["?"] * len(codes_in_table))
        # Batch-load tail windows per stock using a single window-function query
        tail_df = con.execute(f"""
            WITH ranked AS (
                SELECT ts_code, {','.join(col_list)},
                       ROW_NUMBER() OVER (
                           PARTITION BY ts_code ORDER BY trade_date DESC
                       ) AS rn
                FROM {dwd_table}
                WHERE ts_code IN ({ph})
            )
            SELECT ts_code, {','.join(col_list)}
            FROM ranked
            WHERE rn <= {sig_window}
            ORDER BY ts_code, trade_date
        """, [*codes_in_table]).df()

        if tail_df.empty:
            continue

        groups = tail_df.groupby("ts_code")
        recs = []
        for ts_code_found, indicator_name, freq, sig_cols, max_td in entries:
            grp = groups.get_group(ts_code_found)
            if grp is None or grp.empty:
                continue
            grp = grp[grp["trade_date"] <= max_td].tail(sig_window)
            if grp.empty:
                continue
            history_fp = state_signature(grp, max_td, sig_cols, sig_window)
            recs.append((
                ts_code_found, freq, indicator_name, max_td, history_fp,
                calc_date, None, "v1",
            ))

        if recs:
            upsert_calc_state_batch(con, recs)
            recovered += len(recs)
            for ts_code_found, freq, indicator_name, anchor, history_fp, \
                    _, _, _ in recs:
                state_map[(ts_code_found, freq, indicator_name)] = {
                    "last_trade_date": anchor,
                    "history_fp": history_fp,
                    "spec_version": "v1",
                    "updated_calc_date": calc_date,
                    "quote_latest_adj": None,
                }

    return recovered, state_map


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
