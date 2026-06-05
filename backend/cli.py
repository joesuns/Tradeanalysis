"""CLI entry point for the Tradeanalysis data pipeline.

Usage:
    python -m backend.cli run
    python -m backend.cli run --date 20260604
    python -m backend.cli check
    python -m backend.cli fetch [--ts-code 000543.SZ] [--start 20150101]
    python -m backend.cli calc [--ts-code 000543.SZ]
    python -m backend.cli export --date 20260529 [--ts-code 000543.SZ]
    python -m backend.cli query --ts-code 000001.SZ
    python -m backend.cli status
"""

import argparse
import sys

from backend.log_config import setup_logging

setup_logging()


def _resolve_trade_date(con, date: str = None) -> str:
    """解析分析日期：指定则用指定，不指定则用今天。

    Returns YYYYMMDD string.
    """
    if date:
        return date
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d")


def _ensure_trade_date(con, date: str) -> str:
    """确保 date 是交易日；不是则往前找最近交易日。

    Queries dim_date to validate and rollback. Prints a warning if
    the original date was not a trading day.
    """
    row = con.execute(
        "SELECT MAX(trade_date) FROM dim_date "
        "WHERE trade_date <= ? AND is_trade_day = 1",
        (date,),
    ).fetchone()
    if not row or not row[0]:
        return date  # dim_date may be empty — trust the caller
    trade_date = row[0]
    if trade_date != date:
        print(f"Warning: {date} is not a trading day, using {trade_date} instead")
    return trade_date


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

    No --ts-code: date-batched parallel mode for full market.
    --ts-code: stock-batched mode, per-stock incremental detection.
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

    Auto-fetches missing data before calculating.
    No --ts-code: calculate all active stocks.
    """
    from backend.db.connection import get_connection
    from backend.etl.orchestrator import run_calc

    con = get_connection()
    try:
        ts_codes = args.ts_code if args.ts_code else None
        run_calc(con, ts_codes=ts_codes, auto_fetch=True)
    finally:
        con.close()


# ── export ──

def cmd_export(args):
    """Export analysis wide table to Excel.

    Reads DWS data directly from the database. No recalculation.
    Use 'calc' then 'export' separately if fresh data is needed.
    """
    from backend.export_wide import export_wide_to_excel

    if args.output is None:
        from datetime import datetime
        gen_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"analysis_{args.date}_gen{gen_ts}.xlsx"

    ts_codes = args.ts_code if args.ts_code else None

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
    fp.add_argument("--ts-code", nargs="+",
                    help="Stock codes to fetch (omitted = all stocks)")
    fp.add_argument("--start", help="Start date YYYYMMDD (default 20150101)")
    fp.add_argument("--end", help="End date YYYYMMDD (default today)")

    # calc
    cp = sp.add_parser("calc", help="Compute DWS indicators")
    cp.add_argument("--ts-code", nargs="+",
                    help="Stock codes to calculate (omitted = all stocks)")

    # export
    xp = sp.add_parser("export", help="Export analysis wide table to Excel")
    xp.add_argument("--date", required=True, help="Analysis date YYYYMMDD")
    xp.add_argument("--output", default=None,
                    help="Output Excel path. Default: analysis_{date}_gen{now}.xlsx")
    xp.add_argument("--ts-code", nargs="+", help="Stock codes to export")
    xp.add_argument("--db-path")
    xp.add_argument("--include-st", action="store_true")
    xp.add_argument("--no-index", action="store_true")

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
