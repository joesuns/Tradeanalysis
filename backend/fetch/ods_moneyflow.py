import logging
logger = logging.getLogger(__name__)

def fetch_moneyflow_batch(client, con, ts_codes: list[str], start: str, end: str) -> tuple[int, list[str]]:
    """Fetch moneyflow data for a batch of stocks. UPSERT into ods_moneyflow.
    Returns (total_rows, list_of_failed_ts_codes)."""
    failed = []
    rows = 0
    for ts_code in ts_codes:
        try:
            recs = client.call("moneyflow", ts_code=ts_code, start_date=start, end_date=end)
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
                rows += 1
        except Exception as e:
            logger.error(f"Failed moneyflow for {ts_code}: {e}")
            failed.append(ts_code)
    return rows, failed
