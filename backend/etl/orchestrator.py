"""ETL orchestrator — wires together fetch, DIM, DWD, and DWS steps.

Usage:
    from backend.etl.orchestrator import run_etl
    run_etl()                             # full pipeline
    run_etl(step="fetch-ods")             # fetch only
    run_etl(step="calc-dws", ts_codes=["000001.SZ"])  # specific stocks
"""

import logging
from typing import Optional
from datetime import datetime

from backend.db.connection import get_connection, check_connectivity, run_checkpoint
from backend.etl.error_handler import log_etl, check_data_completeness
from backend.fetch.client import TushareClient
from backend.fetch.ods_daily import fetch_by_date_range_parallel, get_all_active_codes
from backend.etl.build_dim import build_dim_stock, build_dim_date, build_dim_concept
from backend.etl.build_dwd import build_dwd_daily_quote, build_dwd_daily_moneyflow, build_dwd_weekly_quote
from backend.etl.calc_macd import MACDCalculator
from backend.etl.calc_ma import MACalculator
from backend.etl.calc_kpattern import KPatternCalculator
from backend.etl.calc_dde import DDECalculator
from backend.etl.calc_volume import VolumeCalculator

logger = logging.getLogger(__name__)

CALCULATORS = [MACDCalculator, MACalculator, KPatternCalculator,
               DDECalculator, VolumeCalculator]


def run_etl(step: str = "build-all", ts_codes: Optional[list[str]] = None,
            start: Optional[str] = None, end: Optional[str] = None,
            batch_size: int = 100, force_full: bool = False):
    """Run the ETL pipeline.

    Parameters
    ----------
    step : str
        One of "fetch-ods", "build-dim", "build-dwd", "calc-dws", "build-all".
    ts_codes : Optional[list[str]]
        Stock codes to process. If None, all active codes are used.
    start, end : Optional[str]
        Date range in YYYYMMDD format for fetch step.
    batch_size : int
        Number of stocks per batch (default 100).
    force_full : bool
        Currently unused — reserved for future forced-full-recalc logic.
    """
    con = get_connection()
    try:
        # 0. Self-check
        health = check_connectivity()
        if "fatal" in health.get("duckdb", ""):
            log_etl(con, "health_check", "failed",
                    error_msg=health["duckdb"])
            raise RuntimeError(health["duckdb"])
        log_etl(con, "health_check", "success",
                error_msg=f"DuckDB v{health['version']}, "
                          f"{health['disk_free_mb']}MB free")

        # 1. Determine what to run
        if step in ("fetch-ods", "build-all"):
            client = TushareClient()
            # Global dimension data — always needed regardless of --ts-code
            from backend.fetch.ods_stock_basic import fetch_stock_basic
            from backend.fetch.ods_trade_cal import fetch_trade_cal
            from backend.fetch.ods_concept import fetch_concept_detail
            n = fetch_stock_basic(client, con)
            log_etl(con, "fetch_stock_basic", "success", row_count=n)
            n = fetch_trade_cal(client, con)
            log_etl(con, "fetch_trade_cal", "success", row_count=n)
            codes = ts_codes or get_all_active_codes(con)
            n = fetch_concept_detail(client, con, ts_codes=codes)
            log_etl(con, "fetch_concept_detail", "success", row_count=n)
            # Date-based batch fetch: 4 API calls per trading day for ALL stocks
            rows = fetch_by_date_range_parallel(
                start or "20150101", end or "20991231", workers=3)
            log_etl(con, "fetch_market_data",
                    "success", row_count=rows)

        if step in ("build-dim", "build-all"):
            n = build_dim_stock(con)
            log_etl(con, "build_dim_stock", "success", row_count=n)
            n = build_dim_date(con)
            log_etl(con, "build_dim_date", "success", row_count=n)
            nc, nm = build_dim_concept(con)
            log_etl(con, "build_dim_concept", "success", row_count=nc + nm)

        if step in ("build-dwd", "build-all"):
            codes = ts_codes or get_all_active_codes(con)
            n = build_dwd_daily_quote(con, codes)
            log_etl(con, "build_dwd_daily", "success", row_count=n)
            n = build_dwd_weekly_quote(con, codes)
            log_etl(con, "build_dwd_weekly", "success", row_count=n)
            n = build_dwd_daily_moneyflow(con, codes)
            log_etl(con, "build_dwd_moneyflow", "success", row_count=n)

        if step in ("calc-dws", "build-all"):
            codes = ts_codes or get_all_active_codes(con)
            calc_date = datetime.now().strftime("%Y%m%d")
            for i in range(0, len(codes), batch_size):
                batch = codes[i:i + batch_size]
                for CalcCls in CALCULATORS:
                    for freq in ("daily", "weekly"):
                        calc = CalcCls(con, freq)
                        calc.calculate(batch, calc_date)
            log_etl(con, "calc_dws", "success", row_count=len(codes))

        # Final checkpoint
        run_checkpoint(con)
    finally:
        con.close()
