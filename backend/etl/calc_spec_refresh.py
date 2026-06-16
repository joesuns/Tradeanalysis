"""Narrow-window FULL for indicators whose dws_calc_state spec lags code."""
import logging
from typing import Dict, List, Optional, Set, Tuple

from backend.etl.calc_batch_append import run_batch_full_phase
from backend.etl.calc_spec_gate import find_spec_stale_codes
from backend.etl.error_handler import log_etl_end, log_etl_start

logger = logging.getLogger(__name__)


def run_refresh_spec(
    con,
    calc_date: str,
    refresh_indicators: List[str],
    ts_codes: Optional[List[str]] = None,
    dry_run: bool = False,
) -> dict:
    """Recompute only indicators with stale spec_version in dws_calc_state."""
    from backend.etl.calc_fast_skip import batch_load_dde_tails, batch_load_quote_tails
    from backend.etl.calc_indicators import quote_tail_columns
    from backend.etl.calc_state import load_calc_state_batch

    stale_groups = find_spec_stale_codes(con, refresh_indicators, ts_codes)
    if not stale_groups:
        logger.info("refresh-spec: no stale rows for %s", refresh_indicators)
        return {
            "refreshed": 0,
            "full_by_indicator": {},
            "dry_run": dry_run,
            "stale_groups": {},
        }

    stale_summary = {
        f"{ind}_{freq}": len(codes)
        for (ind, freq), codes in stale_groups.items()
    }
    all_codes: Set[str] = set()
    for codes in stale_groups.values():
        all_codes.update(codes)
    codes_list = sorted(all_codes)

    if dry_run:
        logger.info(
            "refresh-spec dry-run: indicators=%s stocks=%d groups=%d stale=%s",
            refresh_indicators, len(codes_list), len(stale_groups), stale_summary,
        )
        return {
            "refreshed": 0,
            "full_by_indicator": {},
            "calculated": 0,
            "dry_run": True,
            "stale_groups": stale_summary,
            "stocks": len(codes_list),
        }

    logger.info(
        "refresh-spec: indicators=%s stocks=%d groups=%d",
        refresh_indicators, len(codes_list), len(stale_groups),
    )

    daily_tails = batch_load_quote_tails(
        con, codes_list, "daily", quote_tail_columns("daily"),
    )
    weekly_tails = batch_load_quote_tails(
        con, codes_list, "weekly", quote_tail_columns("weekly"),
    )
    dde_daily = batch_load_dde_tails(con, codes_list, "daily")
    dde_weekly = batch_load_dde_tails(con, codes_list, "weekly")
    state_map = load_calc_state_batch(con, codes_list)

    batch_ctx = {
        "stock_modes": {},
        "daily_tails": daily_tails,
        "weekly_tails": weekly_tails,
        "dde_daily": dde_daily,
        "dde_weekly": dde_weekly,
        "state_map": state_map,
    }
    result = run_batch_full_phase(con, calc_date, stale_groups, batch_ctx)
    return {
        "refreshed": result.get("batch_full_items", 0),
        "full_by_indicator": result.get("full_by_indicator", {}),
        "calculated": sum(
            v.calculated for v in result.get("agg_by_key", {}).values()
        ),
    }


def cmd_refresh_spec(
    con,
    calc_date: str,
    refresh_spec: str,
    ts_codes: Optional[List[str]] = None,
    dry_run: bool = False,
):
    """CLI entry: parse comma-separated indicator names and run refresh."""
    indicators = [s.strip().lower() for s in refresh_spec.split(",") if s.strip()]
    if not indicators:
        raise ValueError("refresh-spec requires at least one indicator name")
    lid, t0 = log_etl_start(con, "calc_refresh_spec")
    try:
        summary = run_refresh_spec(
            con, calc_date, indicators, ts_codes, dry_run=dry_run,
        )
        log_etl_end(
            con, lid, "calc_refresh_spec", t0, "success",
            row_count=summary.get("calculated", 0),
            data_completeness={"calc_date": calc_date, **summary},
        )
        if dry_run:
            logger.info(
                "refresh-spec dry-run done: stale_groups=%s stocks=%s",
                summary.get("stale_groups"),
                summary.get("stocks", 0),
            )
        else:
            logger.info(
                "refresh-spec done: calculated=%s full_by_indicator=%s",
                summary.get("calculated", 0),
                summary.get("full_by_indicator", {}),
            )
    except Exception:
        log_etl_end(con, lid, "calc_refresh_spec", t0, "failed")
        raise
