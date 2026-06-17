"""Diff B4 hard-gate columns vs project 123 SQLite (transition period only).

Compares 10 hard-gate columns (excludes ``ma_alignment`` / ``w_ma_alignment`` and ``dde_alert`` / ``w_dde_alert`` soft layer).

Usage:
    export REF_123_SQLITE_PATH=/path/to/123/cache/stock_data.db
    python -m scripts.diff_vs_123 --date 20260605
    python -m scripts.diff_vs_123 --dates-file tests/fixtures/b4_gate/dates.txt --summary
"""
import argparse
import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import DUCKDB_PATH
from backend.b4_gate.diff import diff_b4_frames
from backend.b4_gate.extract import extract_ta_b4, extract_123_b4, resolve_week_end
from backend.b4_gate.ref import resolve_ref_123_sqlite_path
from backend.b4_gate.sample import load_sample, skip_dde_compare
from backend.b4_gate.verify import load_dates


def main():
    parser = argparse.ArgumentParser(
        description="Diff B4 hard-gate columns (10) vs 123 reference DB",
    )
    parser.add_argument("--date", help="Analysis date YYYYMMDD")
    parser.add_argument("--dates-file", help="File with one date per line")
    parser.add_argument("--sample", default="tests/fixtures/b4_gate/sample_500.csv")
    parser.add_argument("--out", help="Write mismatch CSV path")
    parser.add_argument("--summary", action="store_true", help="Only print totals")
    parser.add_argument(
        "--breakdown", action="store_true",
        help="Print mismatch counts per field",
    )
    parser.add_argument("--ta-db", default=DUCKDB_PATH)
    parser.add_argument(
        "--ref-db",
        default=None,
        help="123 SQLite path (default: REF_123_SQLITE_PATH)",
    )
    args = parser.parse_args()

    try:
        ref_path = resolve_ref_123_sqlite_path(args.ref_db)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    dates = []
    if args.date:
        dates.append(args.date)
    if args.dates_file:
        dates.extend(load_dates(Path=args.dates_file))
    if not dates:
        print("Provide --date or --dates-file", file=sys.stderr)
        sys.exit(2)

    sample_rows = load_sample(Path(args.sample))
    codes = [r.ts_code for r in sample_rows]
    bucket_by_ts = {r.ts_code: r.bucket for r in sample_rows}
    skip_dde_ts = {
        r.ts_code for r in sample_rows if skip_dde_compare(r.ts_code, r.bucket)
    }

    ta_con = duckdb.connect(args.ta_db, read_only=True)
    all_mismatches = []
    try:
        for date in dates:
            ta = extract_ta_b4(ta_con, date, codes)
            week_end = resolve_week_end(ta_con, date)
            ref = extract_123_b4(ref_path, date, codes, weekly_date=week_end)
            mm = diff_b4_frames(
                ta, ref, skip_dde_ts=skip_dde_ts, bucket_by_ts=bucket_by_ts
            )
            for m in mm:
                m["trade_date"] = date
            all_mismatches.extend(mm)
            if not args.summary:
                print(f"{date}: {len(mm)} mismatches")
    finally:
        ta_con.close()

    total = len(all_mismatches)
    print(f"TOTAL mismatches: {total}")
    if args.breakdown and all_mismatches:
        import pandas as pd

        by_field = pd.DataFrame(all_mismatches).groupby("field").size()
        for field, n in by_field.sort_values(ascending=False).items():
            print(f"  {field}: {n}")
    if args.out and all_mismatches:
        import pandas as pd

        pd.DataFrame(all_mismatches).to_csv(args.out, index=False)
        print(f"Wrote {args.out}")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
