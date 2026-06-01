def fetch_concept_detail(client, con) -> int:
    """Fetch concept-stock mappings. UPSERT into ods_concept_detail."""
    records = client.call("concept_detail")
    for r in records:
        con.execute("""INSERT OR REPLACE INTO ods_concept_detail (concept_name, ts_code, fetched_at)
            VALUES (?,?,now())""", (r["concept_name"], r["ts_code"]))
    return len(records)
