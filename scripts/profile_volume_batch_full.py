"""Profile VolumeCalculator batch_full throughput.

Usage:
    python scripts/profile_volume_batch_full.py --sample 500
"""
import argparse
import time

from backend.db.connection import get_connection
from backend.etl.calc_volume import VolumeCalculator
from backend.etl.calc_batch_append import batch_full_volume
from backend.etl.base import load_quote_groups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=500,
                        help="Number of stocks to profile")
    args = parser.parse_args()

    from datetime import datetime, timedelta

    con = get_connection()
    calc_date = "20260615"

    # Recalc start matching run_batch_full_phase behaviour
    recalc_dt = datetime.strptime(calc_date, "%Y%m%d") - timedelta(days=400)
    recalc_start = recalc_dt.strftime("%Y%m%d")
    load_dt = datetime.strptime(calc_date, "%Y%m%d") - timedelta(days=500)
    load_start = load_dt.strftime("%Y%m%d")

    # Resolve active stocks
    rows = con.execute(
        "SELECT DISTINCT ts_code FROM dwd_daily_quote "
        "WHERE trade_date <= ? ORDER BY ts_code",
        [calc_date],
    ).fetchall()
    all_codes = [r[0] for r in rows]
    ts_codes = all_codes[: args.sample]

    calc = VolumeCalculator(con, "daily")
    load_cols = calc.quote_load_columns("daily")

    t0 = time.monotonic()
    quote_groups = load_quote_groups(
        con, calc.src_table, "daily", load_cols, ts_codes,
        start_date=load_start,
    )
    t1 = time.monotonic()
    print(f"load_quote_groups: {t1 - t0:.1f}s for {len(ts_codes)} stocks")

    t0 = time.monotonic()
    agg, stock_rows = batch_full_volume(
        con, "daily", ts_codes, calc_date, recalc_start, quote_groups,
    )
    t1 = time.monotonic()
    elapsed = t1 - t0
    rate = len(ts_codes) / elapsed if elapsed > 0 else float("inf")
    print(f"batch_full_volume: {elapsed:.1f}s for {len(ts_codes)} stocks "
          f"({rate:.1f} stocks/s)")
    print(f"calculated={agg.calculated} skipped={agg.total_skipped}")

    con.close()


if __name__ == "__main__":
    main()
