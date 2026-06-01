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
    """Fetch concept-stock mappings. UPSERT into ods_concept_detail. Returns row count.

    Strategy:
      - <=100 stocks: per-stock concept_detail(ts_code=x) — 1 call/stock
      - >100 stocks or None: per-concept — concept() then concept_detail(id=x) — ~879 calls
    """
    if ts_codes and len(ts_codes) <= 100:
        return _fetch_by_stock(client, con, ts_codes)
    else:
        if ts_codes:
            logger.info(f"{len(ts_codes)} stocks > 100, "
                        f"switching to per-concept ({879} calls vs {len(ts_codes)})")
        return _fetch_by_concept(client, con)


def _fetch_by_stock(client, con, ts_codes: list[str]) -> int:
    """Per-stock: concept_detail(ts_code=xxx) for each stock. Best for small batches."""
    rows = 0
    for ts_code in ts_codes:
        try:
            records = client.call("concept_detail", ts_code=ts_code)
            for r in records:
                con.execute("""INSERT OR REPLACE INTO ods_concept_detail
                    (concept_name, ts_code, fetched_at) VALUES (?,?,now())""",
                    (r["concept_name"], r["ts_code"]))
                rows += 1
        except Exception as e:
            logger.warning(f"concept_detail({ts_code}): {e}")
    return rows


def _fetch_by_concept(client, con) -> int:
    """Per-concept: concept() then concept_detail(id=xxx) per concept.
    ~879 concepts, ~2 min at 500 calls/min."""
    concepts = client.call("concept")
    logger.info(f"Found {len(concepts)} concepts, fetching detail "
                f"(~{len(concepts)/500*60:.0f}s est.)")
    rows = 0
    for i, c in enumerate(concepts):
        try:
            records = client.call("concept_detail", id=c["code"])
            for r in records:
                con.execute("""INSERT OR REPLACE INTO ods_concept_detail
                    (concept_name, ts_code, fetched_at) VALUES (?,?,now())""",
                    (r["concept_name"], r["ts_code"]))
                rows += 1
        except Exception as e:
            logger.warning(f"concept_detail({c['code']} {c['name']}): {e}")
        if (i + 1) % 200 == 0:
            logger.info(f"  concept: {i+1}/{len(concepts)}")
    return rows
