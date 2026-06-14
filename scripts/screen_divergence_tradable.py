#!/usr/bin/env python3
"""Screen stocks with tradable MACD/DDE divergence on a given date."""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import duckdb

from backend.etl.divergence_tradable import (
    evaluate_tradable_for_case,
    reject_reason_zh,
    tradable_label_zh,
)
from backend.log_config import set_run_id, setup_logging

logger = setup_logging(__name__)

DB_DEFAULT = Path(__file__).resolve().parents[1] / "data" / "tradeanalysis.duckdb"

VIEW_MAP = {
    ("macd", "daily"): "v_dws_macd_daily_latest",
    ("macd", "weekly"): "v_dws_macd_weekly_latest",
    ("dde", "daily"): "v_dws_dde_daily_latest",
    ("dde", "weekly"): "v_dws_dde_weekly_latest",
}


def _list_l1_events(
    db_path: str,
    trade_date: str,
    freq: str,
    indicator: str,
    tradable_only: bool,
) -> tuple[list[dict], int, int]:
    """Return (output_rows, l1_total, tradable_total)."""
    view = VIEW_MAP[(indicator, freq)]
    td_norm = trade_date.replace("-", "")[:8]
    con = duckdb.connect(db_path, read_only=True)
    out = []
    try:
        rows = con.execute(
            f"""
            SELECT ts_code, trade_date, divergence AS l1
            FROM {view}
            WHERE trade_date = ? AND divergence IS NOT NULL
            ORDER BY ts_code
            """,
            [td_norm],
        ).fetchall()
        for ts_code, td, l1 in rows:
            verdict = evaluate_tradable_for_case(
                db_path, ts_code, td, freq, indicator, con=con,
            )
            out.append(
                {
                    "ts_code": ts_code,
                    "trade_date": td,
                    "l1": l1,
                    "tradable": verdict.tradable_label,
                    "reject_reason": verdict.reject_reason,
                }
            )
    finally:
        con.close()
    l1_total = len(out)
    tradable_total = sum(1 for r in out if r["tradable"])
    if tradable_only:
        out = [r for r in out if r["tradable"]]
    return out, l1_total, tradable_total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screen tradable MACD/DDE divergence")
    parser.add_argument("--date", required=True, help="Trade date YYYYMMDD")
    parser.add_argument(
        "--freq",
        choices=["daily", "weekly"],
        default="daily",
        help="Bar frequency (default: daily)",
    )
    parser.add_argument(
        "--indicator",
        choices=["macd", "dde"],
        default="macd",
        help="Indicator (default: macd)",
    )
    parser.add_argument(
        "--tradable-only",
        action="store_true",
        help="Only show rows passing tradable gates",
    )
    parser.add_argument(
        "--db",
        default=str(DB_DEFAULT),
        help=f"DuckDB path (default: {DB_DEFAULT})",
    )
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 1

    set_run_id(uuid.uuid4().hex[:8])
    logger.info(
        "progress screening.tradable: started | date=%s freq=%s indicator=%s",
        args.date, args.freq, args.indicator,
    )
    t0 = time.monotonic()
    rows, l1_total, tradable_total = _list_l1_events(
        args.db, args.date, args.freq, args.indicator, args.tradable_only,
    )
    logger.info(
        "progress screening.tradable: done | l1=%d tradable=%d shown=%d | %.1fs",
        l1_total,
        tradable_total,
        len(rows),
        time.monotonic() - t0,
    )
    print("ts_code\ttrade_date\tl1\ttradable\treject_reason")
    for row in rows:
        print(
            f"{row['ts_code']}\t{row['trade_date']}\t"
            f"{tradable_label_zh(row['l1'])}\t"
            f"{tradable_label_zh(row['tradable'])}\t"
            f"{reject_reason_zh(row['reject_reason'])}"
        )
    print(f"# total={len(rows)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
