"""Calc date validation against ODS tail — prevents phantom calc runs."""
from typing import Optional
import logging

import duckdb

logger = logging.getLogger(__name__)


def get_ods_max_trade_date(con) -> Optional[str]:
    try:
        row = con.execute("SELECT MAX(trade_date) FROM ods_daily").fetchone()
    except duckdb.CatalogException:
        return None
    return row[0] if row and row[0] else None


def resolve_effective_calc_date(con, requested: str, cap_to_ods: bool = True) -> str:
    """Return min(requested, ods_max) when cap_to_ods and ods_max exists."""
    if not cap_to_ods:
        return requested
    ods_max = get_ods_max_trade_date(con)
    if ods_max and requested > ods_max:
        logger.warning(
            "calc_date %s ahead of ods_max %s — capping to ods_max",
            requested, ods_max,
        )
        return ods_max
    return requested


def assert_calc_date_ready(con, calc_date: str, strict: bool = True) -> None:
    ods_max = get_ods_max_trade_date(con)
    if ods_max and calc_date > ods_max:
        msg = (
            f"calc_date {calc_date} > ods_max {ods_max}: "
            f"no market data for requested date. "
            f"Run fetch first or use --date {ods_max}."
        )
        if strict:
            raise ValueError(msg)
        logger.warning(msg)
