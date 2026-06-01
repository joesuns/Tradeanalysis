"""Fetch daily OHLCV + daily_basic + moneyflow by TRADE_DATE.

Key insight: tushare daily/daily_basic/moneyflow APIs all support trade_date param,
returning ALL stocks for that date in ONE call. This is ~100x faster than per-stock.

Multi-threaded: each thread handles a chunk of trading days with its own
TushareClient + DuckDB connection. WAL mode allows concurrent writers.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from backend.config import DUCKDB_PATH
import duckdb

logger = logging.getLogger(__name__)


def fetch_by_date_range(client, con, start: str, end: str) -> int:
    """Fetch daily + daily_basic + moneyflow for all stocks, batched by trade_date.

    Returns total rows written across all three ODS tables.
    """
    # Get the list of trading days in range
    days = _get_trading_days(client, start, end)
    logger.info(f"Fetching data for {len(days)} trading days ({start}~{end})")

    total = 0
    for i, trade_date in enumerate(days):
        if (i + 1) % 50 == 0:
            logger.info(f"  Progress: {i+1}/{len(days)} days")

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


def _get_trading_days(client, start: str, end: str) -> list[str]:
    """Get list of trading days in date range from tushare trade_cal."""
    recs = client.call("trade_cal", exchange="SSE", start_date=start, end_date=end,
                       is_open=1)
    return sorted([r["cal_date"] for r in recs])


def get_all_active_codes(con) -> list[str]:
    """Get all ts_codes that need daily data (not delisted)."""
    return [r[0] for r in con.execute(
        "SELECT ts_code FROM ods_stock_basic WHERE delist_date IS NULL OR delist_date=''"
    ).fetchall()]


def fetch_by_date_range_parallel(start: str, end: str, workers: int = 3) -> int:
    """Multi-threaded version: each thread processes a chunk of trading days.

    Each thread has its own TushareClient + DuckDB connection.
    DuckDB WAL mode allows concurrent writers.
    """
    from backend.fetch.client import TushareClient

    # Get trading days (single-threaded, 1 API call)
    client = TushareClient()
    days = _get_trading_days(client, start, end)
    logger.info(f"Fetching {len(days)} trading days with {workers} threads ({start}~{end})")

    # Split days into chunks
    chunk_size = max(1, len(days) // workers)
    chunks = [days[i:i + chunk_size] for i in range(0, len(days), chunk_size)]

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
                    adj = adj_map.get(r["ts_code"])
                    thread_con.execute("""INSERT OR REPLACE INTO ods_daily
                        (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,now())""",
                        (r["ts_code"], r["trade_date"], r["open"], r["high"], r["low"],
                         r["close"], r["vol"], r["amount"], r["pct_chg"], adj))
                    total += 1

                recs = thread_client.call("daily_basic", trade_date=trade_date)
                for r in recs:
                    thread_con.execute("""INSERT OR REPLACE INTO ods_daily_basic
                        (ts_code, trade_date, total_mv, pe_ttm, turnover_rate, volume_ratio, fetched_at)
                        VALUES (?,?,?,?,?,?,now())""",
                        (r["ts_code"], r["trade_date"], r.get("total_mv"), r.get("pe_ttm"),
                         r.get("turnover_rate"), r.get("volume_ratio")))
                    total += 1

                recs = thread_client.call("moneyflow", trade_date=trade_date)
                for r in recs:
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
            except Exception as e:
                logger.error(f"Thread failed trade_date={trade_date}: {e}")
        thread_con.close()
        return total

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_fetch_chunk, chunks))

    return sum(results)
