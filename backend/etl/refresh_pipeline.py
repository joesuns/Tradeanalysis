"""Explicit force-recalc pipeline for ``cli refresh`` (R1)."""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Single-day full-market 12-route refresh ≈ 5500×12; require explicit opt-in above this.
REFRESH_CONFIRM_ROUTE_THRESHOLD = 60_000

VALID_INDICATORS = frozenset({
    "macd", "ma", "kpattern", "volume", "priceposition", "dde",
})


def parse_indicator_filter(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    indicators = [s.strip().lower() for s in raw.split(",") if s.strip()]
    invalid = [i for i in indicators if i not in VALID_INDICATORS]
    if invalid:
        raise ValueError(
            f"Unknown indicator(s): {invalid}. "
            f"Valid: {sorted(VALID_INDICATORS)}",
        )
    return indicators


def resolve_refresh_routes(
    indicator_filter: Optional[List[str]] = None,
) -> List[Tuple[str, str]]:
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS

    if indicator_filter is None:
        return [(ind, freq) for ind, freq, *_ in CALC_ROUTE_SPECS]
    filt = set(indicator_filter)
    return [(ind, freq) for ind, freq, *_ in CALC_ROUTE_SPECS if ind in filt]


def codes_for_route(ts_codes: List[str], indicator: str) -> List[str]:
    if indicator == "dde":
        return [c for c in ts_codes if not c.endswith(".BJ")]
    return list(ts_codes)


def build_refresh_full_groups(
    ts_codes: List[str],
    routes: List[Tuple[str, str]],
) -> Dict[Tuple[str, str], List[str]]:
    groups: Dict[Tuple[str, str], List[str]] = {}
    for ind, freq in routes:
        groups[(ind, freq)] = codes_for_route(ts_codes, ind)
    return groups


def estimate_refresh_scope(
    dates: List[str],
    ts_codes: List[str],
    routes: List[Tuple[str, str]],
) -> dict:
    per_date = sum(len(codes_for_route(ts_codes, ind)) for ind, _freq in routes)
    return {
        "dates": dates,
        "n_stocks": len(ts_codes),
        "indicators": sorted({ind for ind, _ in routes}),
        "n_routes": len(routes),
        "est_route_count": len(dates) * per_date,
    }


def _rebuild_dwd_after_fetch(con, codes: List[str], date: str, fetch_result) -> tuple:
    """DWD narrow rebuild after refresh fetch (changed ∪ stale)."""
    from backend.etl.build_dwd import rebuild_dwd_for_stale
    from backend.etl.orchestrator import find_stale_dwd_codes
    from backend.etl.pipeline_context import coerce_fetch_result

    fr = coerce_fetch_result(fetch_result)
    changed = fr.changed_codes_for_date(date)
    stale = find_stale_dwd_codes(con, codes, date)
    to_rebuild = sorted(set(changed) | set(stale))
    if not to_rebuild:
        return {}, []
    result = rebuild_dwd_for_stale(con, to_rebuild, date)
    return result, to_rebuild


def run_refresh_calc(
    con,
    calc_date: str,
    ts_codes: List[str],
    indicator_filter: Optional[List[str]] = None,
) -> dict:
    """Force FULL for scoped routes; bypasses idempotent skip and batch shortcut."""
    import time

    from backend.db.connection import run_checkpoint
    from backend.etl.calc_batch_append import run_batch_full_phase
    from backend.etl.calc_fast_skip import batch_load_dde_tails, batch_load_quote_tails
    from backend.etl.calc_indicators import quote_tail_columns
    from backend.etl.calc_state import load_calc_state_batch
    from backend.etl.error_handler import log_etl_end, log_etl_start
    from backend.etl.orchestrator import _filter_delisted

    routes = resolve_refresh_routes(indicator_filter)
    codes, delisted = _filter_delisted(con, ts_codes, calc_date)
    if delisted:
        logger.info("refresh calc: %d delisted stocks excluded", len(delisted))
    if not codes:
        return {
            "calculated": 0,
            "full_by_indicator": {},
            "batch_full_items": 0,
            "routes": [f"{i}_{f}" for i, f in routes],
            "force_scope": True,
        }

    full_groups = build_refresh_full_groups(codes, routes)
    daily_tails = batch_load_quote_tails(
        con, codes, "daily", quote_tail_columns("daily"),
    )
    weekly_tails = batch_load_quote_tails(
        con, codes, "weekly", quote_tail_columns("weekly"),
    )
    dde_daily = batch_load_dde_tails(con, codes, "daily")
    dde_weekly = batch_load_dde_tails(con, codes, "weekly")
    state_map = load_calc_state_batch(con, codes)
    batch_ctx = {
        "stock_modes": {},
        "daily_tails": daily_tails,
        "weekly_tails": weekly_tails,
        "dde_daily": dde_daily,
        "dde_weekly": dde_weekly,
        "state_map": state_map,
        "force_recompute": True,
    }

    lid, t0 = log_etl_start(con, "calc_dws")
    t_start = time.monotonic()
    try:
        result = run_batch_full_phase(con, calc_date, full_groups, batch_ctx)
        calculated = sum(
            v.calculated for v in result.get("agg_by_key", {}).values()
        )
        summary = {
            "calculated": calculated,
            "full_by_indicator": result.get("full_by_indicator", {}),
            "batch_full_items": result.get("batch_full_items", 0),
            "routes": [f"{i}_{f}" for i, f in routes],
            "force_scope": True,
            "indicator_filter": indicator_filter,
        }
        log_etl_end(
            con, lid, "calc_dws", t0, "success",
            row_count=calculated,
            data_completeness={
                "calc_date": calc_date,
                "mode": "refresh",
                **summary,
            },
        )
        run_checkpoint(con)
        logger.info(
            "refresh calc done: %.0fs calculated=%d full_by_indicator=%s",
            time.monotonic() - t_start,
            calculated,
            summary["full_by_indicator"],
        )
        return summary
    except Exception:
        log_etl_end(con, lid, "calc_dws", t0, "failed")
        raise


def run_refresh_pipeline(
    con,
    analysis_date: str,
    ts_codes: Optional[List[str]] = None,
    indicator_filter: Optional[List[str]] = None,
    do_export: bool = False,
    dry_run: bool = False,
    confirmed: bool = False,
    export_path: Optional[str] = None,
    db_path: str = "data/tradeanalysis.duckdb",
) -> dict:
    """Single-day refresh: fetch → DWD → force FULL calc → optional export."""
    from backend.etl.error_handler import log_etl_end, log_etl_start
    from backend.etl.pipeline_context import PipelineContext, coerce_fetch_result
    from backend.fetch.client import TushareClient
    from backend.fetch.ods_daily import (
        fetch_by_date_range_parallel,
        fetch_stocks_incremental,
        get_all_active_codes,
    )

    codes = ts_codes or get_all_active_codes(con)
    routes = resolve_refresh_routes(indicator_filter)
    scope = estimate_refresh_scope([analysis_date], codes, routes)

    if dry_run:
        logger.info("refresh dry-run: %s", scope)
        return {"dry_run": True, **scope}

    if (
        scope["est_route_count"] > REFRESH_CONFIRM_ROUTE_THRESHOLD
        and not confirmed
    ):
        raise ValueError(
            f"Large refresh scope ({scope['est_route_count']} route-runs). "
            f"Re-run with --confirm to proceed.",
        )

    logger.info(
        "=== refresh fetch for %s (%d stocks, %d routes) ===",
        analysis_date, len(codes), len(routes),
    )
    lid, t0 = log_etl_start(con, "refresh_fetch")
    try:
        if ts_codes:
            client = TushareClient()
            fetch_result = fetch_stocks_incremental(
                client, con, codes,
                start=analysis_date, end=analysis_date,
                force_compare=True,
            )
        else:
            fetch_result = fetch_by_date_range_parallel(
                analysis_date, analysis_date,
                workers=3, ts_codes=codes, con=con,
                skip_covered=False,
            )
        fetch_result = coerce_fetch_result(fetch_result)
        log_etl_end(
            con, lid, "refresh_fetch", t0, "success",
            row_count=fetch_result.rows_written,
            data_completeness={
                "analysis_date": analysis_date,
                "mode": "refresh",
                **fetch_result.to_completeness(),
            },
        )
    except Exception:
        log_etl_end(con, lid, "refresh_fetch", t0, "failed")
        raise

    pipeline_ctx = PipelineContext.from_fetch(
        con, analysis_date, codes, fetch_result,
        mode="refresh", force_scope=True,
        indicator_filter=indicator_filter,
    )

    logger.info("=== refresh DWD for %s ===", analysis_date)
    lid, t0 = log_etl_start(con, "refresh_rebuild_dwd")
    try:
        dwd_result, stale_rebuilt = _rebuild_dwd_after_fetch(
            con, codes, analysis_date, fetch_result,
        )
        from backend.etl.build_dwd import _dwd_rebuild_row_count

        rebuild_rows = _dwd_rebuild_row_count(dwd_result) if dwd_result else 0
        log_etl_end(
            con, lid, "refresh_rebuild_dwd", t0, "success",
            row_count=rebuild_rows,
            data_completeness={
                "analysis_date": analysis_date,
                "skipped": not dwd_result,
                "stale_count": len(stale_rebuilt),
                **pipeline_ctx.to_completeness(),
            },
        )
    except Exception:
        log_etl_end(con, lid, "refresh_rebuild_dwd", t0, "failed")
        raise

    if dwd_result and stale_rebuilt:
        from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

        lid2, t02 = log_etl_start(con, "refresh_refresh_state")
        try:
            refresh_summary = maybe_refresh_state_after_dwd_rebuild(
                con, stale_rebuilt, analysis_date, dwd_result,
            )
            log_etl_end(
                con, lid2, "refresh_refresh_state", t02, "success",
                row_count=(refresh_summary or {}).get("records_written", 0),
                data_completeness={
                    "analysis_date": analysis_date,
                    "stale_count": len(stale_rebuilt),
                    **(refresh_summary or {}),
                },
            )
        except Exception:
            log_etl_end(con, lid2, "refresh_refresh_state", t02, "failed")
            raise

    if dwd_result and stale_rebuilt and fetch_result is not None:
        from backend.etl.backfill_dde_recalc import (
            maybe_invalidate_dde_after_column_patch,
        )

        maybe_invalidate_dde_after_column_patch(
            con, analysis_date, fetch_result, stale_rebuilt,
        )

    logger.info("=== refresh calc for %s ===", analysis_date)
    calc_summary = run_refresh_calc(
        con, analysis_date, codes, indicator_filter=indicator_filter,
    )

    export_summary = None
    if do_export:
        from backend.export_wide import (
            build_export_data_completeness,
            default_export_path,
            export_wide_to_excel,
        )

        out = export_path or default_export_path(analysis_date)
        logger.info("=== refresh export for %s ===", analysis_date)
        lid, t0 = log_etl_start(con, "refresh_export")
        try:
            result = export_wide_to_excel(
                db_path, analysis_date, out,
                filter_st=True,
                include_index=True,
                ts_codes=ts_codes,
            )
            export_summary = {"row_count": result.row_count, "path": out}
            log_etl_end(
                con, lid, "refresh_export", t0, "success",
                row_count=result.row_count,
                data_completeness=build_export_data_completeness(
                    analysis_date, result.tradable_enrich,
                ),
            )
        except Exception:
            log_etl_end(con, lid, "refresh_export", t0, "failed")
            raise

    return {
        "analysis_date": analysis_date,
        "scope": scope,
        "fetch": fetch_result.to_completeness(),
        "calc": calc_summary,
        "export": export_summary,
    }
