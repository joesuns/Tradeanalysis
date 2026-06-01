"""Fetch concept-stock mappings from tushare.

tushare concept_detail requires either id (concept code) or ts_code.
Strategy:
  - With ts_codes: call concept_detail(ts_code=x) per stock (1 call/stock)
  - Without ts_codes: call concept() to list all, then concept_detail(id=x) per concept
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def fetch_concept_detail(client, con, ts_codes: Optional[list[str]] = None) -> int:
    """Fetch concept-stock mappings. UPSERT into ods_concept_detail. Returns row count."""
    rows = 0

    if ts_codes:
        # Per-stock: one API call per stock, amortized across existing fetch loop
        for ts_code in ts_codes:
            try:
                records = client.call("concept_detail", ts_code=ts_code)
                for r in records:
                    con.execute(
                        """INSERT OR REPLACE INTO ods_concept_detail
                           (concept_name, ts_code, fetched_at) VALUES (?,?,now())""",
                        (r["concept_name"], r["ts_code"]),
                    )
                    rows += 1
            except Exception as e:
                logger.warning(f"concept_detail for {ts_code} failed: {e}")
    else:
        # Full load: list all concepts, then fetch detail per concept
        concepts = client.call("concept")
        logger.info(f"Found {len(concepts)} concepts, fetching detail...")
        for c in concepts:
            try:
                records = client.call("concept_detail", id=c["code"])
                for r in records:
                    con.execute(
                        """INSERT OR REPLACE INTO ods_concept_detail
                           (concept_name, ts_code, fetched_at) VALUES (?,?,now())""",
                        (r["concept_name"], r["ts_code"]),
                    )
                    rows += 1
            except Exception as e:
                logger.warning(f"concept_detail for {c['code']} ({c['name']}) failed: {e}")

    return rows
