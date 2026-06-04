"""Fetch daily OHLCV + daily_basic + moneyflow by TRADE_DATE.

Key insight: tushare daily/daily_basic/moneyflow APIs all support trade_date param,
returning ALL stocks for that date in ONE call. This is ~100x faster than per-stock.

Multi-threaded: each thread handles a chunk of trading days with its own
TushareClient + DuckDB connection. WAL mode allows concurrent writers.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from backend.config import DUCKDB_PATH
import duckdb

logger = logging.getLogger(__name__)


def fetch_by_date_range(client, con, start: str, end: str) -> int:
    """Fetch daily + daily_basic + moneyflow for all stocks, batched by trade_date.

    Returns total rows written across all three ODS tables.
    Incremental: skips dates already present in ods_daily.
    """
    import time
    days = _get_trading_days(client, start, end, con=con)
    if not days:
        logger.info("All dates already in DB — nothing to fetch (%s~%s)", start, end)
        return 0
    logger.info("Fetching data for %d trading days (%s~%s)", len(days), start, end)

    t0 = time.time()
    total = 0
    for i, trade_date in enumerate(days):
        if i > 0 and i % 20 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed * 60 if elapsed > 0 else 0
            eta = (len(days) - i) / rate if rate > 0 else 0
            logger.info("Progress: %d/%d days (%d%%) | %.0fs elapsed | "
                        "%.1f days/min | eta %.0fs",
                        i, len(days), i * 100 // len(days), elapsed, rate, eta)

        try:
            # 0. Fetch adj_factor FIRST — build lookup for daily INSERT
            adj_recs = client.call("adj_factor", trade_date=trade_date)
            adj_map = {a["ts_code"]: a.get("adj_factor") for a in adj_recs}
            total += len(adj_recs)

            # 1. Daily OHLCV — all stocks in one call, with adj_factor from lookup
            recs = client.call("daily", trade_date=trade_date)
            for r in recs:
                adj = adj_map.get(r["ts_code"])
                con.execute("""INSERT OR REPLACE INTO ods_daily
                    (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,now())""",
                    (r["ts_code"], r["trade_date"], r["open"], r["high"], r["low"],
                     r["close"], r["vol"], r["amount"], r["pct_chg"], adj))
                total += 1

            # 2. Daily basic — all stocks in one call
            recs = client.call("daily_basic", trade_date=trade_date)
            for r in recs:
                con.execute("""INSERT OR REPLACE INTO ods_daily_basic
                    (ts_code, trade_date, total_mv, pe_ttm, turnover_rate, volume_ratio, fetched_at)
                    VALUES (?,?,?,?,?,?,now())""",
                    (r["ts_code"], r["trade_date"], r.get("total_mv"), r.get("pe_ttm"),
                     r.get("turnover_rate"), r.get("volume_ratio")))
                total += 1

            # 3. Moneyflow — all stocks in one call
            recs = client.call("moneyflow", trade_date=trade_date)
            for r in recs:
                con.execute("""INSERT OR REPLACE INTO ods_moneyflow
                    (ts_code, trade_date, buy_sm_vol, buy_sm_amount, sell_sm_vol, sell_sm_amount,
                     buy_md_vol, buy_md_amount, sell_md_vol, sell_md_amount,
                     buy_lg_vol, buy_lg_amount, sell_lg_vol, sell_lg_amount,
                     buy_elg_vol, buy_elg_amount, sell_elg_vol, sell_elg_amount,
                     net_mf_vol, net_mf_amount, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,now())""",
                    (r["ts_code"], r["trade_date"],
                     r.get("buy_sm_vol"), r.get("buy_sm_amount"),
                     r.get("sell_sm_vol"), r.get("sell_sm_amount"),
                     r.get("buy_md_vol"), r.get("buy_md_amount"),
                     r.get("sell_md_vol"), r.get("sell_md_amount"),
                     r.get("buy_lg_vol"), r.get("buy_lg_amount"),
                     r.get("sell_lg_vol"), r.get("sell_lg_amount"),
                     r.get("buy_elg_vol"), r.get("buy_elg_amount"),
                     r.get("sell_elg_vol"), r.get("sell_elg_amount"),
                     r.get("net_mf_vol"), r.get("net_mf_amount")))
                total += 1

        except Exception as e:
            logger.error(f"Failed trade_date={trade_date}: {e}")

    return total


def _get_trading_days(client, start: str, end: str,
                      con=None) -> list[str]:
    """Get list of trading days in date range from tushare trade_cal.

    When con is provided, queries ods_daily for already-fetched dates
    and returns only new dates (incremental fetch).
    """
    recs = client.call("trade_cal", exchange="SSE", start_date=start, end_date=end,
                       is_open=1)
    days = sorted([r["cal_date"] for r in recs])

    if con:
        existing = set(r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM ods_daily "
            "WHERE trade_date >= ? AND trade_date <= ?",
            (start, end)
        ).fetchall())
        if existing:
            new_days = [d for d in days if d not in existing]
            logger.info("Incremental: %d/%d dates already in DB, %d new to fetch "
                        "(%s~%s)", len(existing), len(days), len(new_days), start, end)
            return new_days

    return days


def _get_missing_days_for_stock(con, ts_code: str,
                                all_trading_days: list[str]) -> list[str]:
    """返回该股票在交易日列表中缺失的日期。"""
    if not all_trading_days:
        return []
    existing = set(r[0] for r in con.execute(
        "SELECT trade_date FROM ods_daily WHERE ts_code = ? "
        "AND trade_date >= ? AND trade_date <= ?",
        (ts_code, all_trading_days[0], all_trading_days[-1])
    ).fetchall())
    return [d for d in all_trading_days if d not in existing]


def _get_missing_ranges_per_stock(con, ts_code: str,
                                   all_trading_days: list[str]) -> list[tuple[str, str]]:
    """返回该股票缺失的连续日期段列表，每个元素为 (start, end)。
    连续缺失的日期合并为一个 range，减少 API 调用次数。"""
    missing = _get_missing_days_for_stock(con, ts_code, all_trading_days)
    if not missing:
        return []

    day_to_idx = {d: i for i, d in enumerate(all_trading_days)}

    ranges = []
    seg_start = missing[0]
    prev = missing[0]
    for d in missing[1:]:
        idx_prev = day_to_idx[prev]
        idx_curr = day_to_idx[d]
        if idx_curr - idx_prev > 1:
            ranges.append((seg_start, prev))
            seg_start = d
        prev = d
    ranges.append((seg_start, prev))
    return ranges


def fetch_stocks_incremental(client, con, ts_codes: list[str],
                              start: str = None,
                              end: str = "20991231") -> int:
    """Stock-batched 增量拉取：每只股票独立检测缺失日期。

    对每只股票：
      1. 查询 ods_daily 已有日期
      2. 连续缺失段合并为 (start, end)
      3. 调用 daily(ts_code=, start_date=, end_date=) 补拉

    返回写入的总行数。
    """
    import time

    if start is None:
        # Default: 90 calendar-day lookback (~60 trading days) instead of 2015-01-01.
        # Callers needing precise warmup should pass explicit start.
        from datetime import datetime as dt, timedelta
        end_dt = dt.strptime(end[:8], "%Y%m%d") if len(end) >= 8 else dt.now()
        start = (end_dt - timedelta(days=90)).strftime("%Y%m%d")

    try:
        cal = client.call("trade_cal", exchange="SSE", start_date=start, end_date=end, is_open=1)
    except Exception as e:
        logger.error("fetch_stocks_incremental: trade_cal API failed — %s", e)
        return 0
    all_days = sorted([r["cal_date"] for r in cal])
    if not all_days:
        return 0

    total = 0
    t0 = time.time()
    for i, ts_code in enumerate(ts_codes):
        ranges = _get_missing_ranges_per_stock(con, ts_code, all_days)
        if not ranges:
            continue

        for seg_start, seg_end in ranges:
            try:
                # 0. Fetch adj_factor per-stock first — daily API doesn't include it
                adj_recs = client.call("adj_factor", ts_code=ts_code,
                                       start_date=seg_start, end_date=seg_end)
                adj_map = {a["trade_date"]: a.get("adj_factor") for a in adj_recs}

                # 1. Daily OHLCV — per-stock
                recs = client.call("daily", ts_code=ts_code,
                                   start_date=seg_start, end_date=seg_end)
                for r in recs:
                    adj = adj_map.get(r["trade_date"])
                    con.execute("""INSERT OR REPLACE INTO ods_daily
                        (ts_code, trade_date, open, high, low, close, vol,
                         amount, pct_chg, adj_factor, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,now())""",
                        (r["ts_code"], r["trade_date"], r["open"], r["high"],
                         r["low"], r["close"], r["vol"], r["amount"],
                         r["pct_chg"], adj))
                    total += 1
            except Exception as e:
                logger.error("fetch_stocks_incremental %s [%s~%s]: %s",
                             ts_code, seg_start, seg_end, e)

            try:
                recs = client.call("daily_basic", ts_code=ts_code,
                                   start_date=seg_start, end_date=seg_end)
                for r in recs:
                    con.execute("""INSERT OR REPLACE INTO ods_daily_basic
                        (ts_code, trade_date, total_mv, pe_ttm,
                         turnover_rate, volume_ratio, fetched_at)
                        VALUES (?,?,?,?,?,?,now())""",
                        (r["ts_code"], r["trade_date"], r.get("total_mv"),
                         r.get("pe_ttm"), r.get("turnover_rate"),
                         r.get("volume_ratio")))
                    total += 1
            except Exception as e:
                logger.warning("fetch_stocks_incremental %s [%s~%s] daily_basic skipped: %s",
                              ts_code, seg_start, seg_end, e)

            try:
                recs = client.call("moneyflow", ts_code=ts_code,
                                   start_date=seg_start, end_date=seg_end)
                for r in recs:
                    con.execute("""INSERT OR REPLACE INTO ods_moneyflow
                        (ts_code, trade_date, buy_sm_vol, buy_sm_amount,
                         sell_sm_vol, sell_sm_amount, buy_md_vol, buy_md_amount,
                         sell_md_vol, sell_md_amount, buy_lg_vol, buy_lg_amount,
                         sell_lg_vol, sell_lg_amount, buy_elg_vol, buy_elg_amount,
                         sell_elg_vol, sell_elg_amount, net_mf_vol, net_mf_amount,
                         fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,now())""",
                        (r["ts_code"], r["trade_date"],
                         r.get("buy_sm_vol"), r.get("buy_sm_amount"),
                         r.get("sell_sm_vol"), r.get("sell_sm_amount"),
                         r.get("buy_md_vol"), r.get("buy_md_amount"),
                         r.get("sell_md_vol"), r.get("sell_md_amount"),
                         r.get("buy_lg_vol"), r.get("buy_lg_amount"),
                         r.get("sell_lg_vol"), r.get("sell_lg_amount"),
                         r.get("buy_elg_vol"), r.get("buy_elg_amount"),
                         r.get("sell_elg_vol"), r.get("sell_elg_amount"),
                         r.get("net_mf_vol"), r.get("net_mf_amount")))
                    total += 1
            except Exception as e:
                logger.warning("fetch_stocks_incremental %s [%s~%s] moneyflow skipped: %s",
                              ts_code, seg_start, seg_end, e)

        if (i + 1) % 10 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            logger.info("Stock fetch: %d/%d stocks | %.0fs | %.1f stk/s",
                        i + 1, len(ts_codes), elapsed, rate)

    elapsed = time.time() - t0
    logger.info("Stock fetch complete: %d stocks, %d rows, %.0fs",
                len(ts_codes), total, elapsed)
    return total


def get_all_active_codes(con) -> list[str]:
    """Get all ts_codes that need daily data (not delisted)."""
    return [r[0] for r in con.execute(
        "SELECT ts_code FROM ods_stock_basic WHERE delist_date IS NULL OR delist_date=''"
    ).fetchall()]


def fetch_by_date_range_parallel(start: str, end: str, workers: int = 3,
                                 ts_codes: list[str] = None,
                                 con=None) -> int:
    """Multi-threaded version: each thread processes a chunk of trading days.

    Each thread has its own TushareClient + DuckDB connection.
    DuckDB WAL mode allows concurrent writers.

    Parameters
    ----------
    ts_codes : list[str], optional
        If provided, only INSERT rows for these stocks (API still returns all stocks).
    con : duckdb.DuckDBPyConnection, optional
        Existing connection (used for incremental date detection).
    """
    from backend.fetch.client import TushareClient
    import time

    # Get trading days with incremental skip (single-threaded, 1 API call)
    client = TushareClient()
    days = _get_trading_days(client, start, end, con=con)
    if not days:
        logger.info("All dates already in DB — nothing to fetch (%s~%s)", start, end)
        return 0
    code_set = set(ts_codes) if ts_codes else None
    filter_msg = f" ({len(ts_codes)} stocks)" if ts_codes else ""
    logger.info("Fetching %d trading days with %d threads (%s~%s)%s",
                len(days), workers, start, end, filter_msg)
    t0 = time.time()

    # Split days into chunks
    chunk_size = max(1, len(days) // workers)
    chunks = [days[i:i + chunk_size] for i in range(0, len(days), chunk_size)]

    progress_lock = threading.Lock()
    progress_done = [0]  # mutable counter shared across threads

    def _fetch_chunk(trade_dates: list[str]) -> int:
        """Process one chunk of trading days in a thread."""
        thread_client = TushareClient()
        thread_con = duckdb.connect(DUCKDB_PATH)
        total = 0
        for trade_date in trade_dates:
            try:
                adj_recs = thread_client.call("adj_factor", trade_date=trade_date)
                adj_map = {a["ts_code"]: a.get("adj_factor") for a in adj_recs}
                total += len(adj_recs)

                recs = thread_client.call("daily", trade_date=trade_date)
                for r in recs:
                    if code_set and r["ts_code"] not in code_set:
                        continue
                    adj = adj_map.get(r["ts_code"])
                    thread_con.execute("""INSERT OR REPLACE INTO ods_daily
                        (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,now())""",
                        (r["ts_code"], r["trade_date"], r["open"], r["high"], r["low"],
                         r["close"], r["vol"], r["amount"], r["pct_chg"], adj))
                    total += 1

                recs = thread_client.call("daily_basic", trade_date=trade_date)
                for r in recs:
                    if code_set and r["ts_code"] not in code_set:
                        continue
                    thread_con.execute("""INSERT OR REPLACE INTO ods_daily_basic
                        (ts_code, trade_date, total_mv, pe_ttm, turnover_rate, volume_ratio, fetched_at)
                        VALUES (?,?,?,?,?,?,now())""",
                        (r["ts_code"], r["trade_date"], r.get("total_mv"), r.get("pe_ttm"),
                         r.get("turnover_rate"), r.get("volume_ratio")))
                    total += 1

                recs = thread_client.call("moneyflow", trade_date=trade_date)
                for r in recs:
                    if code_set and r["ts_code"] not in code_set:
                        continue
                    thread_con.execute("""INSERT OR REPLACE INTO ods_moneyflow
                        (ts_code, trade_date, buy_sm_vol, buy_sm_amount, sell_sm_vol, sell_sm_amount,
                         buy_md_vol, buy_md_amount, sell_md_vol, sell_md_amount,
                         buy_lg_vol, buy_lg_amount, sell_lg_vol, sell_lg_amount,
                         buy_elg_vol, buy_elg_amount, sell_elg_vol, sell_elg_amount,
                         net_mf_vol, net_mf_amount, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,now())""",
                        (r["ts_code"], r["trade_date"],
                         r.get("buy_sm_vol"), r.get("buy_sm_amount"),
                         r.get("sell_sm_vol"), r.get("sell_sm_amount"),
                         r.get("buy_md_vol"), r.get("buy_md_amount"),
                         r.get("sell_md_vol"), r.get("sell_md_amount"),
                         r.get("buy_lg_vol"), r.get("buy_lg_amount"),
                         r.get("sell_lg_vol"), r.get("sell_lg_amount"),
                         r.get("buy_elg_vol"), r.get("buy_elg_amount"),
                         r.get("sell_elg_vol"), r.get("sell_elg_amount"),
                         r.get("net_mf_vol"), r.get("net_mf_amount")))
                    total += 1

                # Thread-safe progress: log every 20 days
                with progress_lock:
                    progress_done[0] += 1
                    done = progress_done[0]
                if done % 20 == 0 or done == 1:
                    elapsed = time.time() - t0
                    rate = done / elapsed * 60 if elapsed > 0 else 0
                    eta = (len(days) - done) / rate if rate > 0 else 0
                    logger.info("ODS fetch: %d/%d days (%d%%) | %.0fs elapsed | "
                                "%.1f days/min | eta %.0fs",
                                done, len(days), done * 100 // len(days),
                                elapsed, rate, eta)

            except Exception as e:
                logger.error(f"Thread failed trade_date={trade_date}: {e}")
        thread_con.close()
        return total

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_fetch_chunk, chunks))

    total_rows = sum(results)
    elapsed = time.time() - t0
    logger.info("ODS fetch complete: %d rows in %.0fs (%.1f days/min)",
                total_rows, elapsed, len(days) / elapsed * 60 if elapsed > 0 else 0)
    return total_rows
