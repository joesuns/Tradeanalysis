"""ETL orchestrator — wires together fetch, DIM, DWD, and DWS steps.

Usage:
    from backend.etl.orchestrator import run_etl
    run_etl()                             # full pipeline
    run_etl(step="fetch-ods")             # fetch only
    run_etl(step="calc-dws", ts_codes=["000001.SZ"])  # specific stocks
"""

import logging
import time
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

from backend.db.connection import get_connection, check_connectivity, run_checkpoint
from backend.etl.error_handler import (
    log_etl_start, log_etl_end, log_etl_error, check_data_completeness as _check_ods_completeness,
)
from backend.fetch.client import TushareClient
from backend.fetch.ods_daily import fetch_by_date_range_parallel, get_all_active_codes
from backend.etl.build_dim import build_dim_stock, build_dim_date, build_dim_concept
from backend.etl.build_dwd import build_dwd_daily_quote, build_dwd_daily_moneyflow, build_dwd_weekly_quote
from backend.etl.calc_macd import MACDCalculator
from backend.etl.calc_ma import MACalculator
from backend.etl.calc_kpattern import KPatternCalculator
from backend.etl.calc_dde import DDECalculator
from backend.etl.calc_volume import VolumeCalculator
from backend.etl.calc_price_position import PricePositionCalculator

CALCULATORS = [MACDCalculator, MACalculator, KPatternCalculator,
               DDECalculator, VolumeCalculator, PricePositionCalculator]


def run_etl(step: str = "build-all", ts_codes: Optional[list[str]] = None,
            start: Optional[str] = None, end: Optional[str] = None,
            batch_size: int = 100, force_full: bool = False):
    """Run the ETL pipeline.

    Parameters
    ----------
    step : str
        One of "fetch-ods", "build-dim", "build-dwd", "calc-dws", "build-all".
    ts_codes : Optional[list[str]]
        Stock codes to process. If None, all active codes are used.
    start, end : Optional[str]
        Date range in YYYYMMDD format for fetch step.
    batch_size : int
        Number of stocks per batch (default 100).
    force_full : bool
        Currently unused — reserved for future forced-full-recalc logic.
    """
    con = get_connection()
    try:
        # 0. Self-check
        health = check_connectivity()
        lid, t0 = log_etl_start(con, "health_check")
        if "fatal" in health.get("duckdb", ""):
            log_etl_end(con, lid, "health_check", t0, "failed",
                        error_msg=health["duckdb"])
            raise RuntimeError(health["duckdb"])
        log_etl_end(con, lid, "health_check", t0, "success",
                    error_msg=f"DuckDB v{health['version']}, "
                              f"{health['disk_free_mb']}MB free")

        # 1. Determine what to run
        if step in ("fetch-ods", "build-all"):
            client = TushareClient()
            # Global dimension data — always needed regardless of --ts-code
            from backend.fetch.ods_stock_basic import fetch_stock_basic
            from backend.fetch.ods_trade_cal import fetch_trade_cal
            from backend.fetch.ods_concept import fetch_concept_detail

            lid, t0 = log_etl_start(con, "fetch_stock_basic")
            n = fetch_stock_basic(client, con)
            log_etl_end(con, lid, "fetch_stock_basic", t0, "success", row_count=n)

            lid, t0 = log_etl_start(con, "fetch_trade_cal")
            n = fetch_trade_cal(client, con)
            log_etl_end(con, lid, "fetch_trade_cal", t0, "success", row_count=n)

            codes = ts_codes or get_all_active_codes(con)
            # Date-based batch fetch FIRST — 4 API calls/day for ALL stocks (~30s)
            lid, t0 = log_etl_start(con, "fetch_market_data",
                                     min_trade_date=start, max_trade_date=end)
            rows = fetch_by_date_range_parallel(
                start or "20150101", end or "20991231", workers=3,
                ts_codes=ts_codes, con=con)
            log_etl_end(con, lid, "fetch_market_data", t0, "success",
                        row_count=rows,
                        min_trade_date=start, max_trade_date=end)

            # Concept detail LAST — per-stock calls, low priority, skip on failure
            lid, t0 = log_etl_start(con, "fetch_concept_detail")
            try:
                n = fetch_concept_detail(client, con, ts_codes=codes)
                log_etl_end(con, lid, "fetch_concept_detail", t0, "success", row_count=n)
            except Exception as e:
                log_etl_end(con, lid, "fetch_concept_detail", t0, "degraded",
                            error_msg=f"skipped (rate limited): {e}")

            # Run data completeness check after fetch
            lid, t0 = log_etl_start(con, "data_completeness_check")
            comp = _check_ods_completeness(con)
            log_etl_end(con, lid, "data_completeness_check", t0, "success",
                        data_completeness=comp)

        if step in ("build-dim", "build-all"):
            for dim_step, fn in [
                ("build_dim_stock", build_dim_stock),
                ("build_dim_date", build_dim_date),
                ("build_dim_concept", build_dim_concept),
            ]:
                lid, t0 = log_etl_start(con, dim_step)
                try:
                    if dim_step == "build_dim_concept":
                        nc, nm = fn(con)
                        n = nc + nm
                    else:
                        n = fn(con)
                    log_etl_end(con, lid, dim_step, t0, "success", row_count=n)
                except Exception as e:
                    log_etl_error(con, lid, dim_step, t0, 0, e)
                    raise

        if step in ("build-dwd", "build-all"):
            codes = ts_codes or get_all_active_codes(con)
            for dwd_step, fn in [
                ("build_dwd_daily_quote", build_dwd_daily_quote),
                ("build_dwd_weekly_quote", build_dwd_weekly_quote),
                ("build_dwd_daily_moneyflow", build_dwd_daily_moneyflow),
            ]:
                lid, t0 = log_etl_start(con, dwd_step)
                try:
                    n = fn(con, codes)
                    log_etl_end(con, lid, dwd_step, t0, "success", row_count=n)
                except Exception as e:
                    log_etl_error(con, lid, dwd_step, t0, 0, e)
                    raise

        if step in ("calc-dws", "build-all"):
            codes = ts_codes or get_all_active_codes(con)
            lid, t0 = log_etl_start(con, "calc_dws")
            try:
                calc_date = datetime.now().strftime("%Y%m%d")
                n_batches = (len(codes) + batch_size - 1) // batch_size
                grand_total = 0
                calc_start = time.monotonic()

                for CalcCls in CALCULATORS:
                    for freq in ("daily", "weekly"):
                        calc = CalcCls(con, freq)
                        label = f"{CalcCls.__name__} {freq}"
                        t0 = time.monotonic()
                        last_pct = -1

                        for i in range(0, len(codes), batch_size):
                            batch = codes[i:i + batch_size]
                            calc.calculate(batch, calc_date)

                            done = min(i + batch_size, len(codes))
                            pct = done * 100 // len(codes)
                            # Log every 5% milestone (and 100%)
                            if pct - last_pct >= 5 or pct == 100:
                                last_pct = pct
                                elapsed = time.monotonic() - t0
                                rate = done / elapsed if elapsed > 0 else 0
                                logger.info(
                                    "calc_dws %-30s %d/%d (%d%%) — %.0fs, %.0f stk/s",
                                    label, done, len(codes), pct, elapsed, rate,
                                )

                        elapsed = time.monotonic() - t0
                        n = con.execute(
                            f"SELECT COUNT(*) FROM {calc.dws_table} "
                            f"WHERE calc_date = ?", (calc_date,),
                        ).fetchone()[0]
                        grand_total += n
                        logger.info(
                            "calc_dws %-30s DONE — %d rows, %.0fs",
                            label, n, elapsed,
                        )

                total_elapsed = time.monotonic() - calc_start
                logger.info(
                    "calc_dws ALL DONE — %d stocks × 5 indicators × 2 freqs, "
                    "%d rows, %.0fs",
                    len(codes), grand_total, total_elapsed,
                )
                log_etl_end(con, lid, "calc_dws", t0, "success", row_count=grand_total)
            except Exception as e:
                log_etl_error(con, lid, "calc_dws", t0, 0, e)
                raise

        # Final checkpoint
        run_checkpoint(con)
    finally:
        con.close()


def check_data_completeness(con, ts_codes: list[str],
                             min_daily_rows: int = 60) -> dict:
    """检查指定股票在 DWD 层的数据完整度。

    返回:
        {
            "ok": ["000001.SZ", ...],
            "missing": {
                "000543.SZ": {
                    "dwd_rows": 260,
                    "min_date": "20230601",
                    "max_date": "20260603",
                },
                ...
            },
        }
    """
    ok = []
    missing = {}

    for ts_code in ts_codes:
        row = con.execute("""
            SELECT COUNT(*), MIN(trade_date), MAX(trade_date)
            FROM dwd_daily_quote WHERE ts_code = ?
        """, (ts_code,)).fetchone()

        dwd_rows, min_date, max_date = row
        if dwd_rows is not None and dwd_rows >= min_daily_rows:
            ok.append(ts_code)
        else:
            missing[ts_code] = {
                "dwd_rows": dwd_rows or 0,
                "min_date": min_date,
                "max_date": max_date,
            }

    return {"ok": ok, "missing": missing}


def run_calc(con, ts_codes: list[str] = None, auto_fetch: bool = True,
             batch_size: int = 100):
    """执行 DWS 计算流程。

    1. 如果未指定 ts_codes，获取全市场活跃股票
    2. 数据完整度检查
    3. 缺数据 → auto_fetch 补拉 或 报错退出
    4. 逐 Calculator 计算 DWS
    """
    import time
    from datetime import datetime
    from backend.fetch.ods_daily import get_all_active_codes
    from backend.fetch.client import TushareClient
    from backend.fetch.ods_daily import fetch_stocks_incremental
    from backend.etl.error_handler import log_etl_start, log_etl_end
    from backend.db.connection import run_checkpoint

    if ts_codes is None:
        ts_codes = get_all_active_codes(con)
    if not ts_codes:
        logger.warning("No stocks to calculate")
        return

    # 1. 数据完整度检查
    completeness = check_data_completeness(con, ts_codes)
    if completeness["missing"]:
        missing_codes = list(completeness["missing"].keys())
        logger.warning("%d stocks have insufficient DWD data (< %d rows)",
                       len(missing_codes), 60)

        if auto_fetch and len(missing_codes) <= 50:
            logger.info("Auto-fetching missing data for %d stocks...",
                        len(missing_codes))
            client = TushareClient()
            n = fetch_stocks_incremental(client, con, missing_codes)
            logger.info("Fetched %d ODS rows, rebuilding DWD...", n)
            if n > 0:
                from backend.etl.build_dwd import build_dwd_daily_quote
                build_dwd_daily_quote(con, missing_codes)
                # Re-check after fetch
                completeness = check_data_completeness(con, ts_codes)
        elif auto_fetch and len(missing_codes) > 50:
            logger.error(
                "%d stocks missing data (threshold: 50). "
                "Run 'python -m backend.cli fetch --ts-code ...' manually, "
                "or use --no-auto-fetch to skip these stocks.",
                len(missing_codes)
            )
            for code, info in completeness["missing"].items():
                logger.error("  %s: %d DWD rows (%s~%s)",
                             code, info["dwd_rows"],
                             info["min_date"] or "N/A",
                             info["max_date"] or "N/A")
            # Still calculate stocks with sufficient data

    # 2. 只计算数据充足的股票
    codes_to_calc = completeness["ok"]
    if not codes_to_calc:
        logger.warning("No stocks with sufficient data to calculate")
        return

    # 3. 计算 DWS
    calc_date = datetime.now().strftime("%Y%m%d")
    lid, t0 = log_etl_start(con, "calc_dws")
    grand_total = 0
    calc_start = time.monotonic()

    for CalcCls in CALCULATORS:
        for freq in ("daily", "weekly"):
            calc = CalcCls(con, freq)
            label = f"{CalcCls.__name__} {freq}"
            t1 = time.monotonic()

            for i in range(0, len(codes_to_calc), batch_size):
                batch = codes_to_calc[i:i + batch_size]
                calc.calculate(batch, calc_date)

            elapsed = time.monotonic() - t1
            n = con.execute(
                f"SELECT COUNT(*) FROM {calc.dws_table} "
                f"WHERE calc_date = ?", (calc_date,),
            ).fetchone()[0]
            grand_total += n
            logger.info("calc %-30s DONE — %d rows, %.0fs", label, n, elapsed)

    total_elapsed = time.monotonic() - calc_start
    logger.info("calc ALL DONE — %d stocks, %d rows, %.0fs",
                len(codes_to_calc), grand_total, total_elapsed)
    log_etl_end(con, lid, "calc_dws", t0, "success", row_count=grand_total)
    run_checkpoint(con)
