"""CLI entry point for the Tradeanalysis ETL and query tool.

Usage:
    python -m backend.cli check
    python -m backend.cli etl --step build-all
    python -m backend.cli query --ts-code 000001.SZ
    python -m backend.cli export --date 20251231 --output analysis.xlsx
    python -m backend.cli status
"""

import argparse
import sys

from backend.log_config import setup_logging

setup_logging()


def cmd_check(_args):
    """Check environment connectivity: DuckDB + tushare."""
    from backend.db.connection import check_connectivity
    from backend.fetch.client import TushareClient

    db = check_connectivity()
    print(f"DuckDB: {db['duckdb']} (v{db['version']})")
    print(f"Disk free: {db['disk_free_mb']} MB | DB size: {db['db_size_mb']} MB")
    try:
        TushareClient().call("stock_basic", exchange="", list_status="L", limit=1)
        print("tushare: connected")
    except Exception as e:
        print(f"tushare: error — {e}")


def cmd_etl(args):
    """Run the ETL pipeline."""
    from backend.etl.orchestrator import run_etl

    ts_codes = [args.ts_code] if args.ts_code else None
    run_etl(
        step=args.step,
        ts_codes=ts_codes,
        start=args.start,
        end=args.end,
        batch_size=args.batch_size,
        force_full=args.force_full,
    )


def cmd_query(args):
    """Query DWS indicators for a stock."""
    from backend.db.connection import get_connection

    con = get_connection(read_only=True)
    try:
        view = f"v_dws_macd_{args.freq}_latest"
        sql = (
            f"SELECT * FROM {view} "
            f"WHERE ts_code = ? "
            f"AND trade_date = (SELECT MAX(trade_date) FROM {view} WHERE ts_code = ?)"
        )
        row = con.execute(sql, (args.ts_code, args.ts_code)).fetchone()
        if row:
            cols = [d[0] for d in con.description]
            for c, v in zip(cols, row):
                print(f"{c}: {v}")
        else:
            print(f"No data for {args.ts_code}")
    finally:
        con.close()


def cmd_export(args):
    """Export wide table to Excel.  Requires backend.export_wide (Task 18)."""
    from backend.export_wide import export_wide_to_excel

    db_path = args.db_path or "data/tradeanalysis.duckdb"
    n = export_wide_to_excel(
        db_path, args.date, args.output, freq=args.freq,
        filter_st=not args.include_st,
    )
    print(f"Exported {n} rows -> {args.output}")


def cmd_status(_args):
    """Show database table statistics."""
    from backend.db.connection import get_connection

    con = get_connection(read_only=True)
    try:
        tables = [
            "ods_daily", "ods_daily_basic", "ods_moneyflow",
            "dwd_daily_quote", "dws_macd_daily", "dws_ma_daily",
            "dws_kpattern_daily", "dws_dde_daily", "dws_volume_daily",
        ]
        for table in tables:
            try:
                cnt = con.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                latest = con.execute(
                    f"SELECT MAX(trade_date) FROM {table}"
                ).fetchone()[0]
                print(f"{table:25s} {cnt:>12,}  {latest or 'N/A'}")
            except Exception:
                print(f"{table:25s}  (not found)")
    finally:
        con.close()


def main():
    p = argparse.ArgumentParser(prog="tradeanalysis")
    sp = p.add_subparsers(dest="command")

    sp.add_parser("check", help="Check environment connectivity")

    ep = sp.add_parser("etl", help="Run ETL pipeline")
    ep.add_argument(
        "--step", default="build-all",
        choices=["fetch-ods", "build-dim", "build-dwd", "calc-dws", "build-all"],
    )
    ep.add_argument("--start")
    ep.add_argument("--end")
    ep.add_argument("--ts-code", help="Process only this stock (e.g. 000001.SZ)")
    ep.add_argument("--batch-size", type=int, default=100)
    ep.add_argument("--force-full", action="store_true")

    qp = sp.add_parser("query", help="Query DWS indicators")
    qp.add_argument("--ts-code", required=True)
    qp.add_argument("--freq", default="daily")

    xp = sp.add_parser("export", help="Export wide table to Excel")
    xp.add_argument("--date", required=True)
    xp.add_argument("--output", default="analysis.xlsx")
    xp.add_argument("--freq", default="daily")
    xp.add_argument("--db-path")
    xp.add_argument("--include-st", action="store_true")

    sp.add_parser("status", help="Show database table stats")

    args = p.parse_args()
    handlers = {
        "check": cmd_check,
        "etl": cmd_etl,
        "query": cmd_query,
        "export": cmd_export,
        "status": cmd_status,
    }
    handler = handlers.get(args.command)
    if handler:
        handler(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
