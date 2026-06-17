"""Realign dws_calc_state history_fp with current DWD tails without recalculating DWS.

Use after a one-off full-market DWD rebuild that poisons append routing (all FULL /
chunk explosion) while existing DWS snapshots remain valid.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
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


def _apply_records_to_state_map(state_map: dict, records: list, calc_date: str) -> None:
    """Patch in-memory state after upsert (avoids reload load_calc_state_batch)."""
    for ts_code, freq, indicator, last_td, fp, _, quote_adj, spec_ver in records:
        key = (ts_code, freq, indicator)
        entry = state_map.get(key)
        if entry is None:
            state_map[key] = {
                "last_trade_date": last_td,
                "history_fp": fp,
                "quote_latest_adj": quote_adj,
                "spec_version": spec_ver,
                "updated_calc_date": calc_date,
            }
        else:
            entry["history_fp"] = fp
            entry["spec_version"] = spec_ver
            entry["updated_calc_date"] = calc_date


def _run_readonly_loader(loader):
    """Run a read-only query closure without holding a write connection."""
    import duckdb

    from backend.config import DUCKDB_PATH

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        return loader(con)
    finally:
        con.close()


def _load_refresh_tails_sequential_on_con(con, ts_codes: List[str], n: int):
    """Sequential tail load on an existing connection (run path / inline)."""
    from backend.etl.progress import log_timed_step

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
    return state_map, daily_tails, weekly_tails, dde_daily, dde_weekly


def _load_refresh_tails_isolated(ts_codes: List[str], n: int):
    """Load tails via ephemeral read-only connections (cli refresh-state path).

    No write lock held during SQL — enables parallel loaders when
    ``REFRESH_STATE_PARALLEL=1``.
    """
    from backend.config import REFRESH_STATE_PARALLEL
    from backend.etl.progress import log_timed_step

    t0 = time.monotonic()

    if not REFRESH_STATE_PARALLEL:
        def _load_all(connection):
            return (
                load_calc_state_batch(connection, ts_codes),
                batch_load_quote_tails(
                    connection, ts_codes, "daily", quote_tail_columns("daily"),
                ),
                batch_load_quote_tails(
                    connection, ts_codes, "weekly", quote_tail_columns("weekly"),
                ),
                batch_load_dde_tails(connection, ts_codes, "daily"),
                batch_load_dde_tails(connection, ts_codes, "weekly"),
            )

        bundle = log_timed_step(
            "refresh_state", "isolated_sequential",
            lambda: _run_readonly_loader(_load_all),
            stocks=n,
        )
        state_map, daily_tails, weekly_tails, dde_daily, dde_weekly = bundle
    else:
        loaders = (
            ("load_state", lambda c: load_calc_state_batch(c, ts_codes)),
            ("quote_daily", lambda c: batch_load_quote_tails(
                c, ts_codes, "daily", quote_tail_columns("daily"),
            )),
            ("quote_weekly", lambda c: batch_load_quote_tails(
                c, ts_codes, "weekly", quote_tail_columns("weekly"),
            )),
            ("dde_daily", lambda c: batch_load_dde_tails(c, ts_codes, "daily")),
            ("dde_weekly", lambda c: batch_load_dde_tails(c, ts_codes, "weekly")),
        )
        results = {}
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="refresh_ro") as pool:
            futures = {
                name: pool.submit(_run_readonly_loader, fn)
                for name, fn in loaders
            }
            for name, fut in futures.items():
                results[name] = fut.result()
                logger.info(
                    "progress refresh_state: isolated %s done | %d stocks",
                    name, len(results[name]) if name != "load_state" else n,
                )
        state_map = results["load_state"]
        daily_tails = results["quote_daily"]
        weekly_tails = results["quote_weekly"]
        dde_daily = results["dde_daily"]
        dde_weekly = results["dde_weekly"]

    elapsed = time.monotonic() - t0
    logger.info(
        "progress refresh_state: isolated load done | %d stocks | %.1fs | parallel=%s",
        n, elapsed, REFRESH_STATE_PARALLEL,
    )
    return state_map, daily_tails, weekly_tails, dde_daily, dde_weekly


def _load_refresh_tails(con, ts_codes: List[str], n: int, isolated: bool = False):
    if isolated:
        return _load_refresh_tails_isolated(ts_codes, n)
    return _load_refresh_tails_sequential_on_con(con, ts_codes, n)


def _build_preflight_artifacts(
    ts_codes: List[str],
    state_map: dict,
    daily_tails: dict,
    weekly_tails: dict,
    dde_daily: dict,
    dde_weekly: dict,
) -> Tuple[int, int, int, Set[str], Dict[str, dict], Dict[str, dict]]:
    preflight_skip = 0
    preflight_full = 0
    preflight_append = 0
    chunk_stocks: Set[str] = set()
    stock_modes: Dict[str, dict] = {}
    fp_cache_by_stock: Dict[str, dict] = {}

    for ts_code in ts_codes:
        modes, fps = preflight_stock_modes_with_fps(
            ts_code, state_map,
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

    return (
        preflight_skip,
        preflight_full,
        preflight_append,
        chunk_stocks,
        stock_modes,
        fp_cache_by_stock,
    )


def _upsert_refresh_records(con, records: list) -> None:
    from backend.db.schema import ensure_calc_state_table

    ensure_calc_state_table(con)
    upsert_calc_state_batch(con, records)


def refresh_calc_state_fingerprints(
    con,
    ts_codes: List[str],
    calc_date: str,
    dry_run: bool = False,
    return_artifacts: bool = False,
    isolated_tail_load: bool = False,
):
    """Batch-recompute history_fp from 245-bar tails; optional UPSERT only.

    Preserves each row's last_trade_date (DWS anchor). Does not write DWS.

    When ``isolated_tail_load=True`` (cli refresh-state), tail SQL uses ephemeral
    read-only connections so parallel load is safe. Upsert opens a short write
    connection only when ``con`` is None.
    """
    from backend.etl.progress import stock_progress

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
        "tail_load_mode": "isolated" if isolated_tail_load else "inline",
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

    if isolated_tail_load and con is not None:
        logger.warning(
            "refresh_state: isolated_tail_load=True with open con — "
            "tail load ignores con (write con used for upsert only)",
        )

    t0 = time.monotonic()
    n = len(ts_codes)

    state_map, daily_tails, weekly_tails, dde_daily, dde_weekly = _load_refresh_tails(
        con, ts_codes, n, isolated=isolated_tail_load,
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
        from backend.etl.progress import log_timed_step

        def _do_upsert(write_con):
            _upsert_refresh_records(write_con, records)

        if con is not None:
            log_timed_step(
                "refresh_state", "upsert",
                lambda: _do_upsert(con),
                extra=f"records={len(records)}",
            )
        else:
            from backend.db.connection import get_connection

            write_con = get_connection()
            try:
                log_timed_step(
                    "refresh_state", "upsert",
                    lambda: _do_upsert(write_con),
                    extra=f"records={len(records)}",
                )
            finally:
                write_con.close()
        _apply_records_to_state_map(state_map, records, calc_date)

    preflight_skip = 0
    preflight_full = 0
    preflight_append = 0
    chunk_stocks: Set[str] = set()
    stock_modes: Dict[str, dict] = {}
    fp_cache_by_stock: Dict[str, dict] = {}

    # Preflight modes only needed for run→calc hot path (return_artifacts).
    if return_artifacts and not dry_run:
        (
            preflight_skip,
            preflight_full,
            preflight_append,
            chunk_stocks,
            stock_modes,
            fp_cache_by_stock,
        ) = _build_preflight_artifacts(
            ts_codes, state_map,
            daily_tails, weekly_tails, dde_daily, dde_weekly,
        )

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
        "tail_load_mode": "isolated" if isolated_tail_load else "inline",
    }
    logger.info(
        "refresh_calc_state done: stocks=%d updated=%d unchanged=%d skipped=%d "
        "written=%d dry_run=%s elapsed=%.1fs tail_load=%s | "
        "preflight skip=%d full=%d append=%d chunk=%d",
        summary["stocks"], summary["keys_updated"], summary["keys_unchanged"],
        summary["keys_skipped"], summary["records_written"], dry_run, elapsed,
        summary["tail_load_mode"],
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
            "state_map": state_map,
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
        isolated_tail_load=False,
    )
