"""CLI entry point for the Tradeanalysis data pipeline.

Usage:
    python -m backend.cli check
    python -m backend.cli fetch --all
    python -m backend.cli fetch --ts-code 000543.SZ --start 20150101
    python -m backend.cli calc --all
    python -m backend.cli calc --ts-code 000543.SZ
    python -m backend.cli export --date 20260529 --ts-code 000543.SZ --output analysis.xlsx
    python -m backend.cli status
"""

import argparse
import sys

from backend.log_config import setup_logging

setup_logging()


# ── check ──

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


# ── fetch ──

def cmd_fetch(args):
    """Pull ODS data into DuckDB.

    --ts-code mode (recommended for <=500 stocks): stock-batched, each stock
      independently detects missing dates.
    --all mode (default): date-batched, iterates trading days for the full market.
    """
    from backend.db.connection import get_connection
    from backend.fetch.client import TushareClient
    from backend.fetch.ods_daily import (
        fetch_by_date_range_parallel,
        fetch_stocks_incremental,
        get_all_active_codes,
    )

    client = TushareClient()
    con = get_connection()
    try:
        start = args.start or "20150101"
        end = args.end or "20991231"

        if args.ts_code:
            codes = args.ts_code if isinstance(args.ts_code, list) else [args.ts_code]
            print(f"Stock-batched fetch: {len(codes)} stocks, {start}~{end}")
            n = fetch_stocks_incremental(client, con, codes, start=start, end=end)
        else:
            codes = get_all_active_codes(con)
            print(f"Date-batched fetch: {len(codes)} active stocks, {start}~{end}")
            n = fetch_by_date_range_parallel(
                start, end, workers=3, con=con
            )
        print(f"Fetched {n} rows")
    finally:
        con.close()


# ── calc ──

def cmd_calc(args):
    """Compute DWS indicators.

    Pre-check: validates DWD data completeness per stock.
    Missing data → auto-fetch (unless --no-auto-fetch) or error.
    """
    from backend.db.connection import get_connection
    from backend.etl.orchestrator import run_calc

    con = get_connection()
    try:
        ts_codes = args.ts_code if args.ts_code else None
        run_calc(
            con,
            ts_codes=ts_codes,
            auto_fetch=not args.no_auto_fetch,
        )
    finally:
        con.close()


# ── export ──

def cmd_export(args):
    """Export analysis wide table to Excel.

    Default: export directly from latest views (no recalculation).
    --recalc: re-run calc-dws before exporting.
    """
    from backend.db.connection import get_connection
    from backend.export_wide import export_wide_to_excel

    if args.output is None:
        from datetime import datetime
        gen_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"analysis_{args.date}_gen{gen_ts}.xlsx"

    ts_codes = args.ts_code if args.ts_code else None

    if args.recalc:
        print("Recalculating DWS before export...")
        from backend.etl.orchestrator import run_calc
        con = get_connection()
        try:
            run_calc(con, ts_codes=ts_codes, auto_fetch=not args.no_auto_fetch)
        finally:
            con.close()

    n = export_wide_to_excel(
        args.db_path or "data/tradeanalysis.duckdb",
        args.date,
        args.output,
        filter_st=not args.include_st,
        include_index=not args.no_index,
        ts_codes=ts_codes,
    )
    print(f"Exported {n} rows -> {args.output}")


# ── query / status ──

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


def cmd_status(_args):
    """Show database table statistics."""
    from backend.db.connection import get_connection

    con = get_connection(read_only=True)
    try:
        tables = [
            "ods_daily", "ods_daily_basic", "ods_moneyflow",
            "dwd_daily_quote", "dwd_weekly_quote",
            "dws_macd_daily", "dws_ma_daily", "dws_kpattern_daily",
            "dws_dde_daily", "dws_volume_daily", "dws_price_position_daily",
        ]
        for table in tables:
            try:
                cnt = con.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                latest = con.execute(
                    f"SELECT MAX(trade_date) FROM {table}"
                ).fetchone()[0]
                print(f"{table:30s} {cnt:>12,}  {latest or 'N/A'}")
            except Exception:
                print(f"{table:30s}  (not found)")
    finally:
        con.close()


# ── main ──

def main():
    p = argparse.ArgumentParser(prog="tradeanalysis")
    sp = p.add_subparsers(dest="command")

    sp.add_parser("check", help="Check environment connectivity")

    # fetch
    fp = sp.add_parser("fetch", help="Pull ODS data into DuckDB")
    fp.add_argument("--ts-code", nargs="+", help="Stock code(s) to fetch (stock-batched mode)")
    fp.add_argument("--start", help="Start date YYYYMMDD (default 20150101)")
    fp.add_argument("--end", help="End date YYYYMMDD (default today)")
    fp.add_argument("--all", action="store_true",
                    help="Fetch all active stocks (date-batched mode, default)")

    # calc
    cp = sp.add_parser("calc", help="Compute DWS indicators")
    cp.add_argument("--ts-code", nargs="+", help="Stock codes to calculate")
    cp.add_argument("--all", action="store_true",
                    help="Calculate all active stocks (default)")
    cp.add_argument("--no-auto-fetch", action="store_true",
                    help="Disable auto-fetch when data is missing")

    # export
    xp = sp.add_parser("export", help="Export analysis wide table to Excel")
    xp.add_argument("--date", required=True, help="Analysis date YYYYMMDD")
    xp.add_argument("--output", default=None,
                    help="Output Excel path. Default: analysis_{date}_gen{now}.xlsx")
    xp.add_argument("--ts-code", nargs="+", help="Stock codes to export")
    xp.add_argument("--db-path")
    xp.add_argument("--include-st", action="store_true")
    xp.add_argument("--no-index", action="store_true")
    xp.add_argument("--recalc", action="store_true",
                    help="Recalculate DWS before export")
    xp.add_argument("--no-auto-fetch", action="store_true",
                    help="Disable auto-fetch when recalculating")

    # query
    qp = sp.add_parser("query", help="Query DWS indicators")
    qp.add_argument("--ts-code", required=True)
    qp.add_argument("--freq", default="daily")

    sp.add_parser("status", help="Show database table stats")

    args = p.parse_args()
    handlers = {
        "check": cmd_check,
        "fetch": cmd_fetch,
        "calc": cmd_calc,
        "export": cmd_export,
        "query": cmd_query,
        "status": cmd_status,
    }
    handler = handlers.get(args.command)
    if handler:
        handler(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
