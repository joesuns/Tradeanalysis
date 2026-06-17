"""MA alignment Layer 3 fallback semantic audit (read-only).

Checks latest daily MA rows for:
  1. NULL alignment where MA + slopes are computable (should use fallback)
  2. Stored alignment != recomputed _compute_alignment from DWS columns

Usage:
    python3 -m scripts.audit_ma_alignment_fallback
    python3 -m scripts.audit_ma_alignment_fallback --trade-date 20260612

Exit 0 when both counts are 0.
"""
import argparse
import os
import sys

import duckdb
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import DUCKDB_PATH
from backend.etl.calc_ma import MACalculator


def _latest_trade_date(con, trade_date: str = "") -> str:
    if trade_date:
        return trade_date
    row = con.execute(
        "SELECT MAX(trade_date) FROM dwd_daily_quote WHERE is_suspended=0"
    ).fetchone()
    return row[0] or ""


def count_null_alignment_with_ma(con, trade_date: str) -> int:
    """Rows with NULL alignment but all MA inputs present."""
    return con.execute(
        """
        SELECT COUNT(*) FROM v_dws_ma_daily_latest a
        JOIN dwd_daily_quote q ON a.ts_code = q.ts_code AND a.trade_date = q.trade_date
        WHERE a.trade_date = ?
          AND q.is_suspended = 0
          AND a.alignment IS NULL
          AND a.ma_5 IS NOT NULL AND a.ma_10 IS NOT NULL
          AND a.ma5_slope IS NOT NULL AND a.ma10_slope IS NOT NULL
        """,
        [trade_date],
    ).fetchone()[0]


def count_alignment_mismatch(con, trade_date: str, sample_limit: int = 5000) -> int:
    """Recompute alignment from 20-bar DWS tail; count mismatches on anchor bar."""
    codes = con.execute(
        """
        SELECT DISTINCT a.ts_code
        FROM v_dws_ma_daily_latest a
        JOIN dwd_daily_quote q ON a.ts_code = q.ts_code AND a.trade_date = q.trade_date
        WHERE a.trade_date = ? AND q.is_suspended = 0
          AND a.ma_5 IS NOT NULL AND a.ma_10 IS NOT NULL
          AND a.ma5_slope IS NOT NULL AND a.ma10_slope IS NOT NULL
        LIMIT ?
        """,
        [trade_date, sample_limit],
    ).fetchall()
    if not codes:
        return 0

    calc = MACalculator.__new__(MACalculator)
    mismatches = 0
    for (ts_code,) in codes:
        tail = con.execute(
            """
            SELECT trade_date, ma_5, ma_10, ma5_slope, ma10_slope, alignment
            FROM dws_ma_daily
            WHERE ts_code = ? AND trade_date <= ?
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY ts_code, trade_date ORDER BY calc_date DESC
            ) = 1
            ORDER BY trade_date DESC
            LIMIT 20
            """,
            [ts_code, trade_date],
        ).fetchdf()
        if tail.empty:
            continue
        tail = tail.sort_values("trade_date").reset_index(drop=True)
        stored = tail.iloc[-1]["alignment"]
        df = pd.DataFrame({
            "trade_date": tail["trade_date"].tolist(),
            "close_qfq": tail["ma_5"].astype(float).tolist(),
            "ma_5": tail["ma_5"].astype(float).tolist(),
            "ma_10": tail["ma_10"].astype(float).tolist(),
            "ma5_slope": tail["ma5_slope"].astype(float).tolist(),
            "ma10_slope": tail["ma10_slope"].astype(float).tolist(),
        })
        expected = calc._compute_alignment(df)[-1]
        if pd.isna(stored) and expected is None:
            continue
        if stored != expected:
            mismatches += 1
    return mismatches


def audit(con, trade_date: str = "") -> dict:
    td = _latest_trade_date(con, trade_date)
    if not td:
        return {"trade_date": "", "null_with_ma": 0, "mismatch": 0, "error": "no trade_date"}

    null_with_ma = count_null_alignment_with_ma(con, td)
    mismatch = count_alignment_mismatch(con, td)
    return {
        "trade_date": td,
        "null_with_ma": int(null_with_ma or 0),
        "mismatch": int(mismatch or 0),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MA alignment fallback semantic audit")
    parser.add_argument("--trade-date", default="", help="YYYYMMDD anchor (default: latest)")
    args = parser.parse_args(argv)

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        result = audit(con, args.trade_date)
    finally:
        con.close()

    if result.get("error"):
        print(f"ERROR: {result['error']}")
        return 1

    td = result["trade_date"]
    print(f"MA alignment audit @ {td}")
    print(f"  [1] NULL alignment with MA computable: {result['null_with_ma']:,}")
    print(f"  [2] stored != recomputed alignment:    {result['mismatch']:,}")

    ok = result["null_with_ma"] == 0 and result["mismatch"] == 0
    print("  PASS" if ok else "  FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
