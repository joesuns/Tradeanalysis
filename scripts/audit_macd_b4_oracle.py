#!/usr/bin/env python3
"""只读审计：DWS stored MACD B4 trend/turning_point vs recompute oracle。

用法:
    python scripts/audit_macd_b4_oracle.py --date 20260616 --freq weekly \\
        --ts-code-file docs/superpowers/plans/evidence/2026-06-17-m5-pilot-codes.txt
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import DUCKDB_PATH
from backend.etl.b4_macd import b4_weekly_series_from_daily
from backend.etl.calc_compute_domain import resolve_compute_indices
from backend.etl.calc_fast_skip import batch_load_quote_tails
from backend.etl.calc_indicators import quote_tail_columns
from backend.etl.calc_macd import MACDCalculator
from backend.etl.orchestrator import resolve_recalc_start
from backend.db.connection import get_connection


def _norm(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val)
    if s in ("None", "nan", "<NA>"):
        return None
    return s


def audit_macd_b4(
    con,
    calc_date: str,
    ts_codes: List[str],
    freq: str,
) -> Tuple[int, int, List[dict]]:
    """Compare stored DWS vs expanding B4 oracle on compute write window."""
    recalc_start = resolve_recalc_start(con, calc_date, freq)
    tails = batch_load_quote_tails(
        con, ts_codes, freq, quote_tail_columns(freq),
    )
    calc = MACDCalculator(con, freq)
    daily_b4_map = {}
    if freq == "weekly":
        b4_start = calc._weekly_b4_daily_start(calc_date)
        daily_b4_map = calc._load_daily_for_b4_batch(
            ts_codes, start_date=b4_start, end_date=calc_date,
        )

    table = f"dws_macd_{freq}"
    matched = mismatched = 0
    details: List[dict] = []

    for ts_code in ts_codes:
        df = tails.get(ts_code)
        if df is None or len(df) == 0:
            details.append({
                "ts_code": ts_code, "freq": freq, "reason": "no_tail",
            })
            mismatched += 1
            continue

        idxs = resolve_compute_indices(df, recalc_start, calc_date)
        if not idxs:
            continue

        if freq == "daily":
            full = calc._compute_indicators(df.copy())
            exp_trends = full["trend"].tolist()
            exp_cross = full["turning_point"].tolist()
        else:
            daily_b4 = daily_b4_map.get(ts_code)
            if daily_b4 is None or daily_b4.empty:
                details.append({
                    "ts_code": ts_code, "freq": freq, "reason": "no_daily_b4",
                })
                mismatched += 1
                continue
            week_ends = df["trade_date"].astype(str).tolist()
            exp_trends, exp_cross = b4_weekly_series_from_daily(
                daily_b4, week_ends,
            )

        stored_rows = con.execute(
            f"""
            SELECT trade_date, trend, turning_point FROM {table}
            WHERE ts_code = ? AND calc_date = ?
              AND trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date
            """,
            [
                ts_code, calc_date,
                str(df.iloc[idxs[0]]["trade_date"]), calc_date,
            ],
        ).fetchall()
        stored = {r[0]: (_norm(r[1]), _norm(r[2])) for r in stored_rows}

        for i in idxs:
            td = str(df.iloc[i]["trade_date"])
            exp = (_norm(exp_trends[i]), _norm(exp_cross[i]))
            got = stored.get(td)
            if got is None:
                mismatched += 1
                details.append({
                    "ts_code": ts_code, "freq": freq,
                    "trade_date": td, "reason": "missing_stored",
                    "expected": exp,
                })
            elif got != exp:
                mismatched += 1
                details.append({
                    "ts_code": ts_code, "freq": freq,
                    "trade_date": td, "stored": got, "expected": exp,
                })
            else:
                matched += 1

    return matched, mismatched, details


def main() -> int:
    parser = argparse.ArgumentParser(description="MACD B4 stored vs oracle audit")
    parser.add_argument("--date", required=True, help="calc_date YYYYMMDD")
    parser.add_argument("--freq", choices=["daily", "weekly", "both"], default="both")
    parser.add_argument("--ts-code", nargs="*", default=None)
    parser.add_argument("--ts-code-file", default=None)
    args = parser.parse_args()

    codes: List[str] = list(args.ts_code or [])
    if args.ts_code_file:
        with open(args.ts_code_file) as f:
            codes.extend(ln.strip() for ln in f if ln.strip())
    if not codes:
        print("No ts_code provided", file=sys.stderr)
        return 2

    freqs = ["daily", "weekly"] if args.freq == "both" else [args.freq]
    con = get_connection(read_only=True)
    try:
        total_m = total_x = 0
        all_details: List[dict] = []
        for freq in freqs:
            m, x, d = audit_macd_b4(con, args.date, codes, freq)
            total_m += m
            total_x += x
            all_details.extend(d)
            print(f"{freq}: matched={m} mismatched={x} stocks={len(codes)}")
        print(f"TOTAL matched={total_m} mismatched={total_x}")
        if all_details:
            print("--- mismatches (first 20) ---")
            for row in all_details[:20]:
                print(row)
        return 1 if total_x else 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
