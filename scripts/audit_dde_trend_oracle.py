#!/usr/bin/env python3
"""只读审计：DWS stored dde_trend vs B4 moneyflow recompute oracle。

用法:
    python scripts/audit_dde_trend_oracle.py --date 20260612 --freq daily --sample 500
    python scripts/audit_dde_trend_oracle.py --date 20260612 --freq daily \
        --ts-code 600831.SH 000691.SZ
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import DUCKDB_PATH
from backend.etl.calc_dde import DDECalculator


def recompute_daily_trend(
    calc: DDECalculator, df, tail_bars: Optional[int] = None,
) -> Optional[str]:
    """Recompute last-bar daily trend using the same domain as production FULL calc.

    Default ``tail_bars=None`` uses the full loaded history. Do **not** default to
    255: DDE trend uses EMA(60) on DDX1 — truncating the series shifts the oracle
    away from ``DDECalculator.calculate()`` / ``_compute_indicators(full_df)``.
    Pass an explicit ``tail_bars`` only for narrow diagnostics.
    """
    work = df.copy()
    if tail_bars is not None and len(work) > tail_bars:
        work = work.tail(tail_bars).reset_index(drop=True)
    out = calc._compute_indicators(work)
    if out.empty or out["trend"].isna().all():
        return None
    return out["trend"].iloc[-1]


def audit_oracle(
    con,
    calc_date: str,
    freq: str = "daily",
    ts_codes: Optional[List[str]] = None,
    sample: Optional[int] = None,
) -> Tuple[int, int, List[dict]]:
    """Return (matched, mismatched, mismatch_rows)."""
    view = f"v_dws_dde_{freq}_latest"
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        rows = con.execute(
            f"SELECT ts_code, trade_date, trend FROM {view} "
            f"WHERE ts_code IN ({ph}) AND trend IS NOT NULL",
            list(ts_codes),
        ).fetchall()
    elif sample:
        rows = con.execute(
            f"""
            SELECT ts_code, trade_date, trend FROM {view}
            WHERE trend IS NOT NULL
              AND trade_date = (SELECT MAX(trade_date) FROM {view})
            ORDER BY ts_code LIMIT ?
            """,
            [sample],
        ).fetchall()
    else:
        rows = con.execute(
            f"""
            SELECT ts_code, trade_date, trend FROM {view}
            WHERE trend IS NOT NULL
              AND trade_date = (SELECT MAX(trade_date) FROM {view})
            """
        ).fetchall()

    calc = DDECalculator(con, freq)
    matched = mismatched = 0
    details = []
    for ts_code, trade_date, stored in rows:
        if freq == "daily":
            df = calc._load_daily(ts_code)
            df = df[df["trade_date"] <= trade_date]
            rc = recompute_daily_trend(calc, df)
        else:
            daily = calc._load_daily_for_trend(ts_code, end_date=trade_date)
            wq = con.execute(
                """
                SELECT w.trade_date FROM dwd_weekly_quote w
                JOIN dim_date dd ON w.trade_date = dd.trade_date AND dd.is_week_end = 1
                WHERE w.ts_code = ? AND w.trade_date <= ?
                ORDER BY w.trade_date
                """,
                [ts_code, trade_date],
            ).fetchdf()
            trends = calc._weekly_trend_from_daily(daily, wq["trade_date"].tolist())
            rc = trends[-1] if trends else None
        if stored == rc:
            matched += 1
        else:
            mismatched += 1
            details.append({
                "ts_code": ts_code,
                "trade_date": trade_date,
                "stored": stored,
                "recompute": rc,
            })
    return matched, mismatched, details


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="DDE trend content oracle")
    p.add_argument("--date", required=True, help="Analysis date YYYYMMDD (for logging)")
    p.add_argument("--freq", choices=["daily", "weekly"], default="daily")
    p.add_argument("--ts-code", nargs="+")
    p.add_argument("--sample", type=int)
    p.add_argument("--db-path", default=DUCKDB_PATH)
    args = p.parse_args(argv)
    con = duckdb.connect(args.db_path, read_only=True)
    try:
        matched, mismatched, details = audit_oracle(
            con, args.date, args.freq, args.ts_code, args.sample,
        )
        print(f"matched={matched} mismatched={mismatched}")
        for d in details[:20]:
            print(d)
        if mismatched:
            return 1
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
