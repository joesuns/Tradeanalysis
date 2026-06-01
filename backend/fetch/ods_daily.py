import logging
logger = logging.getLogger(__name__)

def fetch_daily_batch(client, con, ts_codes: list[str], start: str, end: str) -> tuple[int, list[str]]:
    """Fetch daily OHLCV + daily_basic for a batch of stocks. UPSERT into ODS.
    Returns (total_rows_written, list_of_failed_ts_codes)."""
    failed = []
    rows = 0
    for ts_code in ts_codes:
        try:
            # Fetch daily OHLCV
            recs = client.call("daily", ts_code=ts_code, start_date=start, end_date=end)
            # Fetch adj_factor separately (tushare daily API doesn't include it)
            adj_recs = client.call("adj_factor", ts_code=ts_code, start_date=start, end_date=end)
            adj_map = {a["trade_date"]: a.get("adj_factor") for a in adj_recs}
            for r in recs:
                adj = adj_map.get(r["trade_date"])
                con.execute("""INSERT OR REPLACE INTO ods_daily
                    (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,now())""",
                    (r["ts_code"], r["trade_date"], r["open"], r["high"], r["low"],
                     r["close"], r["vol"], r["amount"], r["pct_chg"], adj))
                rows += 1

            # Fetch daily_basic (PE, market cap, etc.)
            basics = client.call("daily_basic", ts_code=ts_code, start_date=start, end_date=end)
            for r in basics:
                con.execute("""INSERT OR REPLACE INTO ods_daily_basic
                    (ts_code, trade_date, total_mv, pe_ttm, turnover_rate, volume_ratio, fetched_at)
                    VALUES (?,?,?,?,?,?,now())""",
                    (r["ts_code"], r["trade_date"], r.get("total_mv"), r.get("pe_ttm"),
                     r.get("turnover_rate"), r.get("volume_ratio")))
                rows += 1

        except Exception as e:
            logger.error(f"Failed to fetch {ts_code}: {e}")
            failed.append(ts_code)

    return rows, failed

def get_all_active_codes(con) -> list[str]:
    """Get all ts_codes that need daily data (not delisted)."""
    return [r[0] for r in con.execute(
        "SELECT ts_code FROM ods_stock_basic WHERE delist_date IS NULL OR delist_date=''").fetchall()]
