"""Realign dws_calc_state history_fp with current DWD tails without recalculating DWS.

Use after a one-off full-market DWD rebuild that poisons append routing (all FULL /
chunk explosion) while existing DWS snapshots remain valid.
"""
import logging
import time
from typing import Dict, List, Optional, Set, Tuple

from backend.etl.calc_fast_skip import (
    batch_load_dde_tails,
    batch_load_quote_tails,
    preflight_stock_modes_with_fps,
)
from backend.etl.calc_indicators import CALC_ROUTE_SPECS, quote_tail_columns
from backend.etl.calc_router import state_signature
from backend.etl.calc_state import load_calc_state_batch, upsert_calc_state_batch

logger = logging.getLogger(__name__)

Key = Tuple[str, str]  # (indicator_name, freq)


def _tail_for_spec(
    ts_code: str,
    freq: str,
    source: str,
    daily_q: Optional[dict],
    weekly_q: Optional[dict],
    daily_dde: Optional[dict],
    weekly_dde: Optional[dict],
):
    if source == "quote":
        tails = daily_q if freq == "daily" else weekly_q
    else:
        tails = daily_dde if freq == "daily" else weekly_dde
    if not tails:
        return None
    return tails.get(ts_code)


def refresh_calc_state_fingerprints(
    con,
    ts_codes: List[str],
    calc_date: str,
    dry_run: bool = False,
    return_artifacts: bool = False,
):
    """Batch-recompute history_fp from 245-bar tails; optional UPSERT only.

    Preserves each row's last_trade_date (DWS anchor). Does not write DWS.
    """
    from backend.etl.progress import log_timed_step, stock_progress

    empty_summary = {
        "stocks": 0,
        "keys_updated": 0,
        "keys_unchanged": 0,
        "keys_skipped": 0,
        "dry_run": dry_run,
        "elapsed_sec": 0.0,
        "preflight_skip": 0,
        "preflight_full": 0,
        "preflight_append": 0,
        "chunk_stocks": 0,
    }
    empty_artifacts = {
        "daily_tails": {},
        "weekly_tails": {},
        "dde_daily": {},
        "dde_weekly": {},
        "stock_modes": {},
        "fp_cache_by_stock": {},
        "state_map": {},
    }
    if not ts_codes:
        if return_artifacts:
            return empty_summary, empty_artifacts
        return empty_summary

    t0 = time.monotonic()
    n = len(ts_codes)

    state_map = log_timed_step(
        "refresh_state", "load_state",
        lambda: load_calc_state_batch(con, ts_codes), stocks=n,
    )
    daily_tails = log_timed_step(
        "refresh_state", "quote_daily",
        lambda: batch_load_quote_tails(
            con, ts_codes, "daily", quote_tail_columns("daily"),
        ),
        stocks=n,
    )
    weekly_tails = log_timed_step(
        "refresh_state", "quote_weekly",
        lambda: batch_load_quote_tails(
            con, ts_codes, "weekly", quote_tail_columns("weekly"),
        ),
        stocks=n,
    )
    dde_daily = log_timed_step(
        "refresh_state", "dde_daily",
        lambda: batch_load_dde_tails(con, ts_codes, "daily"),
        stocks=n,
    )
    dde_weekly = log_timed_step(
        "refresh_state", "dde_weekly",
        lambda: batch_load_dde_tails(con, ts_codes, "weekly"),
        stocks=n,
    )

    records = []
    updated = 0
    unchanged = 0
    skipped = 0

    prog = stock_progress("refresh_state", n)
    prog.log_start()
    for ts_code in ts_codes:
        for indicator_name, freq, CalcCls, sig_cols, source in CALC_ROUTE_SPECS:
            tdf = _tail_for_spec(
                ts_code, freq, source,
                daily_tails, weekly_tails, dde_daily, dde_weekly,
            )
            if tdf is None or tdf.empty:
                if ts_code.endswith(".BJ") and source == "dde":
                    skipped += 1
                    continue
                skipped += 1
                continue

            st = state_map.get((ts_code, freq, indicator_name))
            if st is not None:
                last_td = st["last_trade_date"]
            else:
                last_td = str(tdf["trade_date"].max())

            fp = state_signature(tdf, last_td, sig_cols)
            spec_ver = getattr(CalcCls, "SPEC_VERSION", "v1")
            old_fp = st["history_fp"] if st else None
            old_spec = (st.get("spec_version") or "v1") if st else None
            if old_fp == fp and old_spec == spec_ver:
                unchanged += 1
                continue

            updated += 1
            quote_adj = st.get("quote_latest_adj") if st else None
            records.append((
                ts_code, freq, indicator_name, last_td, fp,
                calc_date, quote_adj, spec_ver,
            ))
        prog.tick()
    prog.log_done()

    if not dry_run and records:
        log_timed_step(
            "refresh_state", "upsert",
            lambda: upsert_calc_state_batch(con, records),
            extra=f"records={len(records)}",
        )

    # Post-check: batch preflight routing after refresh (reuse in calc hot path).
    preflight_skip = 0
    preflight_full = 0
    preflight_append = 0
    chunk_stocks: Set[str] = set()
    stock_modes: Dict[str, dict] = {}
    fp_cache_by_stock: Dict[str, dict] = {}
    fresh_state = state_map
    if not dry_run:
        fresh_state = load_calc_state_batch(con, ts_codes)
        for ts_code in ts_codes:
            modes, fps = preflight_stock_modes_with_fps(
                ts_code, fresh_state,
                daily_tails.get(ts_code), weekly_tails.get(ts_code),
                dde_daily.get(ts_code), dde_weekly.get(ts_code),
                specs=CALC_ROUTE_SPECS,
            )
            if modes is None:
                chunk_stocks.add(ts_code)
                continue
            stock_modes[ts_code] = modes
            fp_cache_by_stock[ts_code] = fps
            for _key, (mode, _) in modes.items():
                if mode == "SKIP":
                    preflight_skip += 1
                elif mode == "APPEND":
                    preflight_append += 1
                else:
                    preflight_full += 1

    elapsed = time.monotonic() - t0
    summary = {
        "stocks": n,
        "keys_updated": updated,
        "keys_unchanged": unchanged,
        "keys_skipped": skipped,
        "records_written": 0 if dry_run else len(records),
        "dry_run": dry_run,
        "elapsed_sec": round(elapsed, 1),
        "preflight_skip": preflight_skip,
        "preflight_full": preflight_full,
        "preflight_append": preflight_append,
        "chunk_stocks": len(chunk_stocks),
    }
    logger.info(
        "refresh_calc_state done: stocks=%d updated=%d unchanged=%d skipped=%d "
        "written=%d dry_run=%s elapsed=%.1fs | preflight skip=%d full=%d append=%d chunk=%d",
        summary["stocks"], summary["keys_updated"], summary["keys_unchanged"],
        summary["keys_skipped"], summary["records_written"], dry_run, elapsed,
        preflight_skip, preflight_full, preflight_append, len(chunk_stocks),
    )
    if return_artifacts:
        tails_bundle = {
            "daily_tails": daily_tails,
            "weekly_tails": weekly_tails,
            "dde_daily": dde_daily,
            "dde_weekly": dde_weekly,
            "stock_modes": stock_modes,
            "fp_cache_by_stock": fp_cache_by_stock,
            "state_map": fresh_state,
        }
        return summary, tails_bundle
    return summary


def maybe_refresh_state_after_dwd_rebuild(
    con,
    ts_codes: List[str],
    calc_date: str,
    dwd_result: dict,
    return_artifacts: bool = False,
):
    """Realign calc state fingerprints after a DWD rebuild, before calc routing.

    No-op when DWD_REBUILD_REFRESH_STATE=0, dwd_result empty, or ts_codes empty.
    Does not advance last_trade_date or recalculate DWS.
    """
    from backend.config import DWD_REBUILD_REFRESH_STATE

    if not DWD_REBUILD_REFRESH_STATE:
        return None
    if not dwd_result or not ts_codes:
        return None
    if not any(dwd_result.values()):
        return None
    return refresh_calc_state_fingerprints(
        con, ts_codes, calc_date, dry_run=False, return_artifacts=return_artifacts,
    )
