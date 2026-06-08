import json
import logging

logger = logging.getLogger(__name__)


def fetch_stock_basic(client, con) -> int:
    """Fetch full A-share stock list. UPSERT into ods_stock_basic. Returns row count."""
    logger.info("progress fetch.stock_basic: started")
    records = client.call("stock_basic", exchange="", list_status="L",
        fields="ts_code,symbol,name,area,industry,exchange,list_date,delist_date")
    for r in records:
        con.execute("""INSERT OR REPLACE INTO ods_stock_basic
            (ts_code, symbol, name, area, industry, exchange, list_date, delist_date, raw_json, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,now())""",
            (r["ts_code"], r["symbol"], r["name"], r.get("area",""), r.get("industry",""),
             r["exchange"], r.get("list_date",""), r.get("delist_date",""),
             json.dumps(r, ensure_ascii=False)))
    logger.info("progress fetch.stock_basic: done | rows=%d", len(records))
    return len(records)
