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
from backend.etl.base import SkipReason, CalcResult
from backend.fetch.client import TushareClient
from backend.fetch.ods_daily import fetch_by_date_range_parallel, get_all_active_codes
from backend.etl.build_dim import build_dim_stock, build_dim_date, build_dim_concept
from backend.etl.build_dwd import rebuild_all_dwd
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

    NOTE: Legacy full-pipeline entry point. For calc-only with skip-classification
    and auto-fetch, use run_calc(). This function ignores CalcResult from calculate().

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
            lid, t0 = log_etl_start(con, "build_dwd")
            try:
                result = rebuild_all_dwd(con, codes)
                for name, n in result.items():
                    log_etl_end(con, lid, f"build_dwd_{name}", t0, "success", row_count=n)
            except Exception as e:
                log_etl_error(con, lid, "build_dwd", t0, 0, e)
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


WARMUP_TDAYS = 250  # max across all indicators: PP250d window (pp120=120, divergence=60, MACD=27)


def check_data_completeness(con, ts_codes: list[str],
                             min_daily_rows: int = WARMUP_TDAYS) -> dict:
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

    if not ts_codes:
        return {"ok": ok, "missing": missing}

    # Batch query: one GROUP BY instead of per-stock loop
    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(f"""
        SELECT ts_code, COUNT(*), MIN(trade_date), MAX(trade_date)
        FROM dwd_daily_quote WHERE ts_code IN ({placeholders})
        GROUP BY ts_code
    """, ts_codes).fetchall()

    dwd_data = {r[0]: {"dwd_rows": r[1], "min_date": r[2], "max_date": r[3]}
                for r in rows}

    for ts_code in ts_codes:
        info = dwd_data.get(ts_code)
        if info is not None and info["dwd_rows"] >= min_daily_rows:
            ok.append(ts_code)
        else:
            missing[ts_code] = {
                "dwd_rows": info["dwd_rows"] if info else 0,
                "min_date": info["min_date"] if info else None,
                "max_date": info["max_date"] if info else None,
            }

    return {"ok": ok, "missing": missing}



def _compute_fetch_range(con, ts_code: str, calc_date: str,
                          lookback_tdays: int = WARMUP_TDAYS) -> tuple:
    """Compute the date range that needs to be fetched for a stock.

    Start = max(list_date, calc_date往前推lookback_tdays个交易日)
    End   = min(calc_date, delist_date)

    Returns (needed_start, needed_end) or (None, None) if already covered.
    """
    # 1. stock lifecycle dates
    row = con.execute("""
        SELECT list_date, delist_date FROM dim_stock WHERE ts_code = ?
    """, (ts_code,)).fetchone()
    if not row:
        logger.warning("_compute_fetch_range: %s not found in dim_stock, skipping", ts_code)
        return (None, None)
    list_date, delist_date = row

    # 2. end_date: delisted stock stops at delist_date, otherwise calc_date
    end_date = calc_date
    if delist_date and delist_date < calc_date:
        end_date = delist_date

    # 3. needed start: end_date往前推lookback_tdays个交易日
    needed = con.execute("""
        SELECT trade_date FROM (
            SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
            FROM dim_date WHERE is_trade_day = 1 AND trade_date <= ?
        ) WHERE rn = ?
    """, (end_date, lookback_tdays)).fetchone()
    needed_start = needed[0] if needed else None
    if not needed_start:
        return (None, None)

    # 4. clamp to list_date
    if list_date and list_date > needed_start:
        needed_start = list_date

    # 5. check if already covered by existing ODS data
    actual = con.execute("""
        SELECT COUNT(DISTINCT trade_date) FROM ods_daily
        WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ?
    """, (ts_code, needed_start, end_date)).fetchone()[0]

    expected = con.execute("""
        SELECT COUNT(*) FROM dim_date
        WHERE is_trade_day = 1 AND trade_date >= ? AND trade_date <= ?
    """, (needed_start, end_date)).fetchone()[0]

    if actual > 0 and actual >= expected:
        return (None, None)  # 100% coverage required (aligned with 123 project strict check)

    return (needed_start, end_date)


def _count_trading_days(con, start: str, end: str) -> int:
    """Return number of trading days between start and end (inclusive)."""
    row = con.execute("""
        SELECT COUNT(*) FROM dim_date
        WHERE is_trade_day = 1 AND trade_date >= ? AND trade_date <= ?
    """, (start, end)).fetchone()
    return row[0] if row else 0


def _choose_fetch_strategy(n_stocks: int, n_tdays: int) -> bool:
    """Choose between date-batched (True) and stock-batched (False) mode.

    Stock-batched cost: n_stocks × 4 API calls (per-stock adj_factor/daily/daily_basic/moneyflow).
    Date-batched cost: n_tdays × 4 API calls (per-date, returns all stocks).

    Pick the mode with fewer API calls. When equal, stock-batched is more targeted.
    """
    return n_stocks > n_tdays


def _filter_delisted(con, ts_codes: list[str], calc_date: str) -> tuple:
    """Filter out delisted stocks that already have DWS data.

    Returns (active_codes, delisted_dict).
    """
    active = []
    delisted = {}
    for ts_code in ts_codes:
        row = con.execute(
            "SELECT delist_date FROM dim_stock WHERE ts_code = ?", (ts_code,)
        ).fetchone()
        if not row or not row[0]:
            active.append(ts_code)
            continue
        delist_date = row[0]
        if delist_date >= calc_date:
            active.append(ts_code)
            continue
        # delisted: check if DWS already exists
        has_dws = con.execute(
            "SELECT 1 FROM dws_macd_daily WHERE ts_code = ? LIMIT 1", (ts_code,)
        ).fetchone()
        if has_dws:
            delisted[ts_code] = f"delisted={delist_date}, DWS exists, skip"
        else:
            active.append(ts_code)
            logger.info("Delisted stock %s (%s) — first calc, including",
                        ts_code, delist_date)
    return active, delisted


def _classify_still_missing(con, missing: dict) -> dict:
    """Classify still-missing stocks after fetch+rebuild into root cause categories."""
    classified = {}
    for ts_code, info in missing.items():
        dwd_rows = info["dwd_rows"]
        if dwd_rows == 0:
            if ts_code.endswith(".BJ"):
                reason = SkipReason.SOURCE_UNAVAILABLE
                detail = "BSE stock: DWD data unavailable (tushare may not support)"
            else:
                reason = SkipReason.NO_DWD_DATA
                detail = "DWD rows=0 after fetch+rebuild"
        else:
            reason = SkipReason.INSUFFICIENT_ROWS
            detail = (f"DWD rows={dwd_rows} "
                      f"(min_date={info['min_date']}, max_date={info['max_date']})")
        if reason not in classified:
            classified[reason] = []
        classified[reason].append((ts_code, detail))
    return classified


def _write_skip_log_batch(con, calc_date: str, indicator: str, freq: str,
                           classified: dict):
    """Write classified skip reasons to ods_calc_skip_log."""
    rows = []
    for reason, items in classified.items():
        for ts_code, detail in items:
            rows.append((calc_date, ts_code, indicator, freq, reason.value, detail))
    if rows:
        con.executemany(
            """INSERT OR REPLACE INTO ods_calc_skip_log
               (calc_date, ts_code, indicator, freq, reason, detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )


def run_calc(con, ts_codes: list[str] = None, auto_fetch: bool = True,
             batch_size: int = 100):
    """执行 DWS 计算流程。

    1. 如果未指定 ts_codes，获取全市场活跃股票
    2. 退市股过滤
    3. 数据完整度检查
    4. 缺数据 → auto_fetch 补拉（warmup=27 tdays，熔断器：连续5次失败中止）
    5. 逐 Calculator 计算 DWS
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

    # 0. 退市股过滤
    calc_date = datetime.now().strftime("%Y%m%d")
    ts_codes, delisted = _filter_delisted(con, ts_codes, calc_date)
    if delisted:
        classified = {SkipReason.DELISTED: [(c, d) for c, d in delisted.items()]}
        _write_skip_log_batch(con, calc_date, "dwd", "both", classified)
        logger.info("Pre-calc: %d delisted stocks skipped (DWS already exists)",
                    len(delisted))
    if not ts_codes:
        logger.warning("No stocks to calculate (all delisted or empty input)")
        return

    # 1. 数据完整度检查
    completeness = check_data_completeness(con, ts_codes)
    if completeness["missing"]:
        missing_codes = list(completeness["missing"].keys())
        missing_pct = len(missing_codes) * 100.0 / len(ts_codes)
        logger.info("DWD completeness: %d/%d stocks (%.1f%%) lack sufficient data (< %d rows)",
                    len(missing_codes), len(ts_codes), missing_pct, WARMUP_TDAYS)

        if auto_fetch:
            # Compute per-stock fetch ranges based on warmup
            to_fetch = []
            for ts_code in missing_codes:
                needed_start, needed_end = _compute_fetch_range(con, ts_code, calc_date)
                if needed_start is None:
                    continue
                to_fetch.append((ts_code, needed_start, needed_end))

            if to_fetch:
                logger.info("Auto-fetching %d stocks (warmup=%d tdays, per-stock ranges)...",
                            len(to_fetch), WARMUP_TDAYS)
                client = TushareClient()

                # Group stocks by (start, end) range for batch efficiency
                from collections import defaultdict
                range_buckets = defaultdict(list)
                for ts_code, seg_start, seg_end in to_fetch:
                    range_buckets[(seg_start, seg_end)].append(ts_code)

                consecutive_errors = 0
                n_fetched = 0
                all_fetch_codes = {c for c, _, _ in to_fetch}
                attempted_codes = set()

                for (seg_start, seg_end), bucket_codes in range_buckets.items():
                    try:
                        tdays = _count_trading_days(con, seg_start, seg_end)
                        if _choose_fetch_strategy(len(bucket_codes), tdays):
                            logger.info(
                                "Auto-fetch bucket [%s~%s]: %d stocks, %d tdays "
                                "→ date-batched parallel mode",
                                seg_start, seg_end, len(bucket_codes), tdays,
                            )
                            rows = fetch_by_date_range_parallel(
                                seg_start, seg_end, workers=3,
                                ts_codes=bucket_codes, con=con,
                            )
                        else:
                            logger.info(
                                "Auto-fetch bucket [%s~%s]: %d stocks, %d tdays "
                                "→ stock-batched sequential mode",
                                seg_start, seg_end, len(bucket_codes), tdays,
                            )
                            rows = fetch_stocks_incremental(
                                client, con, bucket_codes,
                                start=seg_start, end=seg_end)
                        attempted_codes.update(bucket_codes)
                        if rows > 0:
                            n_fetched += rows
                            consecutive_errors = 0
                    except Exception as e:
                        attempted_codes.update(bucket_codes)
                        consecutive_errors += 1
                        logger.warning("fetch failed for batch [%s~%s] (%d stocks): %s",
                                       seg_start, seg_end, len(bucket_codes), e)

                    if consecutive_errors >= 5:
                        logger.error("Circuit breaker: %d consecutive fetch errors. "
                                     "tushare may be down. Aborting auto-fetch.",
                                     consecutive_errors)
                        break

                # Mark stocks whose fetch was never attempted as FETCH_FAILED
                fetch_failed_codes = all_fetch_codes - attempted_codes
                if fetch_failed_codes:
                    cf = {SkipReason.FETCH_FAILED: [
                        (c, "auto-fetch aborted by circuit breaker") for c in fetch_failed_codes
                    ]}
                    _write_skip_log_batch(con, calc_date, "dwd", "both", cf)
                    logger.warning("Pre-calc skip: %d stocks — fetch_failed (circuit breaker)",
                                   len(fetch_failed_codes))
                    # Remove from missing so _classify_still_missing doesn't re-classify them
                    for c in fetch_failed_codes:
                        if c in completeness["missing"]:
                            del completeness["missing"][c]

                logger.info("Auto-fetch complete: %d ODS rows fetched (%d batches)",
                            n_fetched, len(range_buckets))
                if n_fetched > 0:
                    rebuild_all_dwd(con, missing_codes)
                    completeness = check_data_completeness(con, ts_codes)
            else:
                logger.info("All missing stocks already have ODS data. Rebuilding DWD...")
                rebuild_all_dwd(con, missing_codes)
                completeness = check_data_completeness(con, ts_codes)
        else:
            logger.info("Auto-fetch disabled. Skipping %d stocks.", len(missing_codes))

    # 2. 分类仍缺失的股票 + 写 skip_log
    if completeness["missing"]:
        classified = _classify_still_missing(con, completeness["missing"])
        _write_skip_log_batch(con, calc_date, "dwd", "both", classified)

        for reason, items in classified.items():
            count = len(items)
            level = "info" if reason in (SkipReason.SOURCE_UNAVAILABLE,
                                          SkipReason.INSUFFICIENT_ROWS) else "warning"
            getattr(logger, level)(
                "Pre-calc skip: %d stocks — %s", count, reason.value)
            sample = [c for c, _ in items[:10]]
            logger.info("  Sample: %s", ", ".join(sample))

    # 3. 只计算数据充足的股票
    codes_to_calc = completeness["ok"]
    if not codes_to_calc:
        logger.warning("No stocks with sufficient data to calculate")
        return

    # 4. 计算 DWS
    lid, t0 = log_etl_start(con, "calc_dws")
    grand_total = 0
    calc_start = time.monotonic()

    for CalcCls in CALCULATORS:
        indicator_name = CalcCls.__name__.replace("Calculator", "").lower()
        for freq in ("daily", "weekly"):
            calc = CalcCls(con, freq)
            label = f"{CalcCls.__name__} {freq}"
            t1 = time.monotonic()
            agg_result = CalcResult()

            for i in range(0, len(codes_to_calc), batch_size):
                batch = codes_to_calc[i:i + batch_size]
                batch_result = calc.calculate(batch, calc_date)
                agg_result.calculated += batch_result.calculated
                for reason, items in batch_result.skipped.items():
                    for ts_code, detail in items:
                        agg_result.add_skip(reason, ts_code, detail)

            _write_skip_log_batch(con, calc_date, indicator_name, freq,
                                  agg_result.skipped)

            elapsed = time.monotonic() - t1
            n = con.execute(
                f"SELECT COUNT(*) FROM {calc.dws_table} "
                f"WHERE calc_date = ?", (calc_date,),
            ).fetchone()[0]
            grand_total += n

            skip_parts = []
            for reason in SkipReason:
                items = agg_result.skipped.get(reason, [])
                if items:
                    skip_parts.append(f"{reason.value}={len(items)}")
            skip_str = ", ".join(skip_parts) if skip_parts else "none skipped"
            logger.info("calc %-30s DONE — %d rows (%d calculated), %s, %.0fs",
                        label, n, agg_result.calculated, skip_str, elapsed)

    total_elapsed = time.monotonic() - calc_start
    logger.info("calc ALL DONE — %d total DWS rows across %d indicator×freq pairs, %.0fs",
                grand_total, len(CALCULATORS) * 2, total_elapsed)
    logger.info("Skip details: SELECT reason, COUNT(*) FROM ods_calc_skip_log "
                "WHERE calc_date='%s' GROUP BY reason", calc_date)
    log_etl_end(con, lid, "calc_dws", t0, "success", row_count=grand_total)
    run_checkpoint(con)
