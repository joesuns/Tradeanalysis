"""Verify Tradeanalysis against frozen B4 golden CSVs (hard gate).

Usage:
    pytest tests/test_b4_gate_regression.py
    python -m scripts.verify_b4_gate
    python -m scripts.verify_b4_gate --export-golden --date 20260605
"""
import argparse
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import DUCKDB_PATH
from backend.b4_gate.verify import (
    export_golden,
    golden_path,
    load_dates,
    verify_all_dates,
)


def main():
    parser = argparse.ArgumentParser(description="B4 golden gate verification")
    parser.add_argument("--db", default=DUCKDB_PATH)
    parser.add_argument("--export-golden", action="store_true")
    parser.add_argument("--date", help="Single date for export")
    parser.add_argument(
        "--dates-file",
        default="tests/fixtures/b4_gate/dates.txt",
    )
    parser.add_argument(
        "--sample",
        default="tests/fixtures/b4_gate/sample_500.csv",
    )
    args = parser.parse_args()

    from pathlib import Path

    con = duckdb.connect(args.db, read_only=True)
    try:
        if args.export_golden:
            dates = [args.date] if args.date else load_dates(Path(args.dates_file))
            if not dates:
                print("No dates to export", file=sys.stderr)
                sys.exit(2)
            for d in dates:
                n = export_golden(
                    con, d, golden_path(d), sample_path=Path(args.sample),
                )
                print(f"Exported golden_{d}.csv ({n} rows)")
            return

        dates = load_dates(Path(args.dates_file))
        failures = verify_all_dates(con, dates)
        if failures:
            for f in failures[:50]:
                print(f)
            if len(failures) > 50:
                print(f"... and {len(failures) - 50} more")
            sys.exit(1)
        print(f"B4 golden OK ({len(dates)} dates)")
    finally:
        con.close()


if __name__ == "__main__":
    main()
