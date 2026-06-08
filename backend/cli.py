"""CLI entry point for the Tradeanalysis data pipeline.

Usage:
    python -m backend.cli run
    python -m backend.cli run --date 20260604
    python -m backend.cli check
    python -m backend.cli fetch [--ts-code 000543.SZ] [--start 20150101]
    python -m backend.cli calc [--date 20260605] [--ts-code 000543.SZ]
    python -m backend.cli export --date 20260529 [--ts-code 000543.SZ]
    python -m backend.cli query --ts-code 000001.SZ
    python -m backend.cli prune [--keep 5]
    python -m backend.cli repair-weekly [--execute]
    python -m backend.cli status
"""

import argparse
import logging
import sys
import uuid
from datetime import datetime

from backend.log_config import setup_logging, set_run_id

setup_logging()
logger = logging.getLogger(__name__)


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
        logger.warning("%s is not a trading day, using %s instead", date, trade_date)
    return trade_date


def _warn_export_coverage(db_path: str, trade_date: str, n_rows: int,
                        filter_st: bool, ts_codes=None):
    """Log WARNING when export rows are far below expected active stocks."""
    from backend.db.connection import get_connection

    con = get_connection(read_only=True)
    try:
        if ts_codes:
            expected = len(ts_codes)
        else:
            st_clause = " AND is_st = 0" if filter_st else ""
            expected = con.execute(f"""
                SELECT COUNT(*) FROM dim_stock
                WHERE list_date <= ?
                  AND (delist_date IS NULL OR delist_date >= ?)
                  {st_clause}
            """, [trade_date, trade_date]).fetchone()[0]
        ods_count = con.execute(
            "SELECT COUNT(*) FROM ods_daily WHERE trade_date = ?", [trade_date]
        ).fetchone()[0]
        threshold = int(expected * 0.8)
        if n_rows < threshold:
            logger.warning(
                "Export row count %d is far below expected ~%d for %s "
                "(ods_daily=%d on date). Check fetch/calc logs.",
                n_rows, expected, trade_date, ods_count,
            )
    finally:
        con.close()


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
    from backend.etl.error_handler import log_etl_end, log_etl_start
    from backend.fetch.client import TushareClient
    from backend.fetch.ods_daily import (
        fetch_by_date_range_parallel,
        fetch_stocks_incremental,
        get_all_active_codes,
    )

    client = TushareClient()
    con = get_connection()
    lid, t0 = log_etl_start(con, "cli_fetch")
    try:
        start = args.start or "20150101"
        end = args.end or "20991231"

        if args.ts_code:
            codes = args.ts_code if isinstance(args.ts_code, list) else [args.ts_code]
            logger.info("Stock-batched fetch: %d stocks, %s~%s", len(codes), start, end)
            n = fetch_stocks_incremental(client, con, codes, start=start, end=end)
            mode = "stock"
        else:
            codes = get_all_active_codes(con)
            logger.info("Date-batched fetch: %d active stocks, %s~%s", len(codes), start, end)
            n = fetch_by_date_range_parallel(
                start, end, workers=3, ts_codes=codes, con=con
            )
            mode = "date"
        log_etl_end(
            con, lid, "cli_fetch", t0, "success", row_count=n,
            data_completeness={"mode": mode, "start": start, "end": end},
        )
        logger.info("Fetch complete: %d ODS rows", n)
    except Exception:
        log_etl_end(con, lid, "cli_fetch", t0, "failed")
        raise
    finally:
        con.close()


# ── calc ──

def cmd_calc(args, skip_stale_fetch=False):
    """Compute DWS indicators.

    Auto-fetches missing warmup + stale latest-day ODS before calculating.
    No --ts-code: calculate all active stocks.
    """
    from backend.db.connection import get_connection
    from backend.etl.orchestrator import run_calc

    con = get_connection()
    try:
        calc_date = None
        if getattr(args, "date", None):
            calc_date = _ensure_trade_date(
                con, _resolve_trade_date(con, args.date))
        ts_codes = args.ts_code if args.ts_code else None
        run_calc(
            con,
            ts_codes=ts_codes,
            auto_fetch=True,
            calc_date=calc_date,
            skip_stale_fetch=skip_stale_fetch,
            force=getattr(args, "force", False),
        )
    finally:
        con.close()


# ── export ──

def cmd_export(args):
    """Export analysis wide table to Excel.

    Reads DWS data directly from the database. No recalculation.
    Use 'calc' then 'export' separately if fresh data is needed.
    """
    from backend.export_wide import default_export_path, export_wide_to_excel

    args.output = default_export_path(args.date, args.output)

    ts_codes = args.ts_code if args.ts_code else None

    n = export_wide_to_excel(
        args.db_path or "data/tradeanalysis.duckdb",
        args.date,
        args.output,
        filter_st=not args.include_st,
        include_index=not args.no_index,
        ts_codes=ts_codes,
    )
    _warn_export_coverage(
        args.db_path or "data/tradeanalysis.duckdb",
        args.date, n, filter_st=not args.include_st, ts_codes=ts_codes,
    )
    print(f"Exported {n} rows -> {args.output}")


# ── run ──

def _rebuild_dwd_for_run(con, codes: list[str], date: str, n_fetch: int) -> dict:
    """Rebuild DWD after run fetch step.

    n_fetch > 0: rebuild all codes (new ODS data).
    n_fetch == 0: rebuild only find_stale_dwd_codes; skip if none stale.
    Returns rebuild_all_dwd result dict, or {} if skipped.
    """
    from backend.etl.build_dwd import rebuild_all_dwd

    if n_fetch > 0:
        logger.info("Rebuilding DWD for %d stocks (fetch wrote %d ODS rows)", len(codes), n_fetch)
        return rebuild_all_dwd(con, codes)

    from backend.etl.orchestrator import find_stale_dwd_codes

    stale = find_stale_dwd_codes(con, codes, date)
    if not stale:
        logger.info("DWD fresh for %s — skip rebuild (%d stocks checked)", date, len(codes))
        return {}
    logger.info("DWD stale for %d/%d stocks on %s — rebuilding subset",
                len(stale), len(codes), date)
    return rebuild_all_dwd(con, stale)


def cmd_run(args):
    """One-command daily analysis: fetch → calc → export.

    Resolves the target trading date, fetches ODS for that day, rebuilds DWD,
    runs all DWS calculators, and exports the Excel report.
    """
    from backend.db.connection import get_connection
    from backend.etl.error_handler import log_etl_end, log_etl_start
    from backend.export_wide import default_export_path, export_wide_to_excel
    from backend.fetch.client import TushareClient
    from backend.fetch.ods_daily import (
        fetch_by_date_range_parallel,
        fetch_stocks_incremental,
        get_all_active_codes,
    )
    # Resolve date on a short-lived connection, then close before later steps.
    con = get_connection()
    try:
        date = _resolve_trade_date(con, args.date)
        date = _ensure_trade_date(con, date)
    finally:
        con.close()

    db_path = args.db_path or "data/tradeanalysis.duckdb"
    ts_codes = args.ts_code if args.ts_code else None

    logger.info("=== Step 1/3: Fetching market data for %s ===", date)
    con = get_connection()
    try:
        codes = ts_codes or get_all_active_codes(con)
        lid, t0 = log_etl_start(con, "run_fetch")
        if ts_codes:
            client = TushareClient()
            n_fetch = fetch_stocks_incremental(
                client, con, codes, start=date, end=date)
        else:
            n_fetch = fetch_by_date_range_parallel(
                date, date, workers=3, ts_codes=codes, con=con)
        logger.info("Fetch complete: %d ODS rows for %s", n_fetch, date)
        log_etl_end(
            con, lid, "run_fetch", t0, "success", row_count=n_fetch,
            data_completeness={"analysis_date": date, "stocks": len(codes)},
        )

        lid, t0 = log_etl_start(con, "run_rebuild_dwd")
        dwd_result = _rebuild_dwd_for_run(con, codes, date, n_fetch)
        rebuild_rows = sum(dwd_result.values()) if dwd_result else 0
        log_etl_end(
            con, lid, "run_rebuild_dwd", t0, "success", row_count=rebuild_rows,
            data_completeness={
                "analysis_date": date,
                "skipped": not dwd_result,
                "n_fetch": n_fetch,
            },
        )
    finally:
        con.close()

    logger.info("=== Step 2/3: Computing indicators for %s ===", date)
    args.date = date
    cmd_calc(args, skip_stale_fetch=True)

    if not getattr(args, "skip_export", False):
        logger.info("=== Step 3/3: Exporting analysis for %s ===", date)
        args.output = default_export_path(date, args.output)

        con = get_connection()
        try:
            lid, t0 = log_etl_start(con, "run_export")
            n = export_wide_to_excel(
                db_path,
                date,
                args.output,
                filter_st=not args.include_st,
                include_index=not args.no_index,
                ts_codes=ts_codes,
            )
            log_etl_end(
                con, lid, "run_export", t0, "success", row_count=n,
                data_completeness={"analysis_date": date},
            )
        finally:
            con.close()

        _warn_export_coverage(
            db_path, date, n, filter_st=not args.include_st, ts_codes=ts_codes,
        )
        print(f"Exported {n} rows -> {args.output}")
    else:
        logger.info("Skipping export (--skip-export)")
    logger.info("Done.")


# ── prune ──

def cmd_prune(args):
    """Prune superseded DWS snapshots, keeping the last N runs.

    Deletes only rows made obsolete by newer calc_date snapshots; the
    latest-per-key value for every (ts_code, trade_date) is always kept,
    so v_*_latest views are unchanged. Runs a CHECKPOINT afterwards to
    reclaim space within the database file.
    """
    from backend.db.connection import get_connection, prune_dws_snapshots, run_checkpoint

    con = get_connection()
    try:
        deleted = prune_dws_snapshots(con, keep_runs=args.keep)
        run_checkpoint(con)
        total = sum(deleted.values())
        for table, n in deleted.items():
            print(f"{table:30s} {n:>12,}")
        print(f"{'TOTAL':30s} {total:>12,} rows pruned (keep_runs={args.keep})")
    finally:
        con.close()


# ── repair-weekly ──

def cmd_repair_weekly(args):
    """Repair weekly data after the date_trunc('week') partition fix.

    Default is a read-only dry-run that previews wrongly-marked week-ends and
    orphan DWS rows. Pass --execute to rebuild dim_date + dwd_weekly_quote and
    delete orphan rows. After executing, run `calc` to refresh stale week-end
    values (fingerprint auto-skips unchanged weeks).
    """
    from backend.db.connection import get_connection
    from backend.etl.repair_weekly import repair_weekly

    con = get_connection()
    try:
        res = repair_weekly(con, dry_run=not args.execute)
        print(f"Wrongly-marked week-ends: {len(res['wrongly_marked'])}")
        print(f"Newly-correct week-ends:  {len(res['newly_marked'])}")
        print("Orphan rows per weekly DWS table:")
        for tbl, n in res["orphans"].items():
            print(f"  {tbl:30s} {n:>12,}")
        if res["executed"]:
            print("EXECUTED — deleted orphan rows:")
            for tbl, n in res["deleted"].items():
                print(f"  {tbl:30s} {n:>12,}")
            print(f"Weekly calc_state invalidated: {res.get('weekly_state_invalidated', 0):,}")
            print("NOTE: run `python -m backend.cli calc` to refresh stale week-end values.")
        else:
            print("DRY-RUN (no changes). Re-run with --execute to apply.")
    finally:
        con.close()


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
    cp.add_argument("--date", help="Analysis date YYYYMMDD (default: today)")
    cp.add_argument("--ts-code", nargs="+",
                    help="Stock codes to calculate (omitted = all stocks)")
    cp.add_argument("--force", action="store_true",
                    help="Recalculate even if calc_date already completed")

    # export
    xp = sp.add_parser("export", help="Export analysis wide table to Excel")
    xp.add_argument("--date", required=True, help="Analysis date YYYYMMDD")
    xp.add_argument("--output", default=None,
                    help="Output Excel path. Default: exports/analysis_{date}_gen{now}.xlsx")
    xp.add_argument("--ts-code", nargs="+", help="Stock codes to export")
    xp.add_argument("--db-path")
    xp.add_argument("--include-st", action="store_true")
    xp.add_argument("--no-index", action="store_true")

    # query
    qp = sp.add_parser("query", help="Query DWS indicators")
    qp.add_argument("--ts-code", required=True)
    qp.add_argument("--freq", default="daily")

    # run
    rp = sp.add_parser("run", help="One-command daily analysis: fetch → calc → export")
    rp.add_argument("--date", help="Analysis date YYYYMMDD (default: today)")
    rp.add_argument("--ts-code", nargs="+", help="Stock codes (omitted = all stocks)")
    rp.add_argument("--output", default=None,
                    help="Output Excel path. Default: exports/analysis_{date}_gen{now}.xlsx")
    rp.add_argument("--db-path", help="DuckDB file path (default: data/tradeanalysis.duckdb)")
    rp.add_argument("--include-st", action="store_true")
    rp.add_argument("--no-index", action="store_true")
    rp.add_argument("--force", action="store_true",
                    help="Force recalc even if calc_date already completed")
    rp.add_argument("--skip-export", action="store_true",
                    help="Skip Excel export (same-day rerun when report unchanged)")

    # prune
    pp = sp.add_parser("prune", help="Prune superseded DWS snapshots (keep last N runs)")
    pp.add_argument("--keep", type=int, default=5,
                    help="Number of most recent calc runs to retain (default 5; 1 = latest only)")

    # repair-weekly
    rwp = sp.add_parser("repair-weekly",
                        help="Repair weekly data after date_trunc('week') fix (dry-run default)")
    rwp.add_argument("--execute", action="store_true",
                     help="Apply changes (default: dry-run preview only)")

    sp.add_parser("status", help="Show database table stats")

    args = p.parse_args()

    # Assign a unique run ID for this CLI invocation
    if args.command:
        set_run_id(uuid.uuid4().hex[:8])

    handlers = {
        "check": cmd_check,
        "fetch": cmd_fetch,
        "calc": cmd_calc,
        "export": cmd_export,
        "run": cmd_run,
        "prune": cmd_prune,
        "repair-weekly": cmd_repair_weekly,
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
