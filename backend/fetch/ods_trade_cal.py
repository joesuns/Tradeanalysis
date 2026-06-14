import logging

logger = logging.getLogger(__name__)


def fetch_trade_cal(client, con, start: str = "20150101", end: str = "20301231") -> int:
    """Fetch trading calendar from tushare. UPSERT into ods_trade_cal."""
    logger.info("progress fetch.trade_cal: started | range=%s~%s", start, end)
    records = client.call("trade_cal", exchange="SSE", start_date=start, end_date=end)
    for r in records:
        con.execute("""INSERT OR REPLACE INTO ods_trade_cal (cal_date, is_open, pretrade_date)
            VALUES (?,?,?)""", (r["cal_date"], r["is_open"], r.get("pretrade_date","")))
    logger.info("progress fetch.trade_cal: done | rows=%d", len(records))
    return len(records)
