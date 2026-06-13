"""Sync B4 DDE trend meta fields from ODS into DWD (content drift repair)."""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


def sync_dwd_dde_meta(
    con,
    ts_codes: Optional[List[str]] = None,
    since: str = "20230911",
) -> dict:
    """UPDATE dwd_daily_moneyflow.net_amount_dc and dwd_daily_quote.circ_mv from ODS."""
    code_filter = ""
    params: list = [since]
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        code_filter = f" AND d.ts_code IN ({ph})"
        params.extend(ts_codes)

    dc_before = con.execute(
        f"""
        SELECT COUNT(*)
        FROM dwd_daily_moneyflow d
        JOIN ods_moneyflow o
          ON d.ts_code = o.ts_code AND d.trade_date = o.trade_date
        WHERE d.trade_date >= ?
          AND o.net_amount_dc IS NOT NULL
          AND d.net_amount_dc IS NULL
          {code_filter}
        """,
        params,
    ).fetchone()[0]

    con.execute(
        f"""
        UPDATE dwd_daily_moneyflow AS d
        SET net_amount_dc = o.net_amount_dc
        FROM ods_moneyflow AS o
        WHERE d.ts_code = o.ts_code
          AND d.trade_date = o.trade_date
          AND d.trade_date >= ?
          AND o.net_amount_dc IS NOT NULL
          AND (d.net_amount_dc IS NULL OR d.net_amount_dc IS DISTINCT FROM o.net_amount_dc)
          {code_filter}
        """,
        params,
    )

    circ_before = con.execute(
        f"""
        SELECT COUNT(*)
        FROM dwd_daily_quote d
        JOIN ods_daily_basic b
          ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date
        WHERE d.trade_date >= ?
          AND b.circ_mv IS NOT NULL AND b.circ_mv > 0
          AND (d.circ_mv IS NULL OR d.circ_mv IS DISTINCT FROM b.circ_mv)
          {code_filter}
        """,
        params,
    ).fetchone()[0]

    con.execute(
        f"""
        UPDATE dwd_daily_quote AS d
        SET circ_mv = b.circ_mv
        FROM ods_daily_basic AS b
        WHERE d.ts_code = b.ts_code
          AND d.trade_date = b.trade_date
          AND d.trade_date >= ?
          AND b.circ_mv IS NOT NULL AND b.circ_mv > 0
          AND (d.circ_mv IS NULL OR d.circ_mv IS DISTINCT FROM b.circ_mv)
          {code_filter}
        """,
        params,
    )

    out = {
        "moneyflow_dc_updated": dc_before,
        "quote_circ_updated": circ_before,
        "since": since,
    }
    logger.info("sync_dwd_dde_meta: %s", out)
    return out
