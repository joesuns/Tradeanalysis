"""One-time backfill of dws_calc_state for stocks missing routing state."""
import logging
from typing import Dict, List, Optional, Set, Tuple

from backend.etl.calc_indicators import CALC_ROUTE_SPECS

logger = logging.getLogger(__name__)

Key = Tuple[str, str]  # (indicator_name, freq)


def find_missing_state_keys(
    con,
    ts_codes: List[str],
) -> Dict[str, Set[Key]]:
    """Return {ts_code: {(indicator, freq), ...}} for keys absent in dws_calc_state."""
    if not ts_codes:
        return {}

    ph = ",".join(["?"] * len(ts_codes))
    existing = con.execute(f"""
        SELECT ts_code, indicator, freq FROM dws_calc_state
        WHERE ts_code IN ({ph})
    """, list(ts_codes)).fetchall()
    have = {(r[0], r[2], r[1]) for r in existing}  # ts_code, freq, indicator

    missing: Dict[str, Set[Key]] = {}
    all_keys = [(ind, freq) for ind, freq, _, _, _ in CALC_ROUTE_SPECS]
    for ts_code in ts_codes:
        gaps = {k for k in all_keys if (ts_code, k[1], k[0]) not in have}
        if gaps:
            missing[ts_code] = gaps
    return missing


def backfill_calc_state(
    con,
    ts_codes: List[str],
    calc_date: str,
) -> dict:
    """FULL-calc only missing (indicator, freq) per stock; idempotent if state exists."""
    from backend.etl.orchestrator import calc_stock_pipeline_selective, resolve_recalc_start

    gaps = find_missing_state_keys(con, ts_codes)
    if not gaps:
        logger.info("backfill_calc_state: no missing state rows")
        return {"stocks": 0, "indicators": 0, "calculated": 0}

    daily_recalc = resolve_recalc_start(con, calc_date, "daily")
    weekly_recalc = resolve_recalc_start(con, calc_date, "weekly")
    total_calc = 0
    ind_count = 0

    for ts_code, run_keys in sorted(gaps.items()):
        run_modes = {k: ("FULL", []) for k in run_keys}
        for indicator_name, freq, result in calc_stock_pipeline_selective(
            con, ts_code, calc_date, daily_recalc, weekly_recalc,
            run_keys=run_keys, run_modes=run_modes,
        ):
            total_calc += result.calculated
            ind_count += 1
            logger.debug(
                "backfill %s %s %s: calculated=%d",
                ts_code, indicator_name, freq, result.calculated,
            )

    summary = {
        "stocks": len(gaps),
        "indicators": ind_count,
        "calculated": total_calc,
    }
    logger.info(
        "backfill_calc_state done: %d stocks, %d indicator runs, %d rows",
        summary["stocks"], summary["indicators"], summary["calculated"],
    )
    return summary
