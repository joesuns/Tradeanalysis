"""One-time / ops backfill for B4 weekly DDE trend inputs (net_amount_dc + circ_mv)."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from backend.etl.calc_dde import DDE_WEEKLY_TREND_HISTORY_DAYS
from backend.fetch.ods_daily import (
    _backfill_circ_mv_by_date,
    _backfill_circ_mv_stock,
    _backfill_net_amount_dc_by_date,
    _backfill_net_amount_dc_stock,
    _get_circ_mv_null_ranges,
    _get_net_amount_dc_null_ranges,
    _local_trading_days,
    get_all_active_codes,
)

logger = logging.getLogger(__name__)

MONEYFLOW_DC_MIN = "20230911"

_STAT_KEYS = (
    "dc_api_calls", "circ_api_calls",
    "dc_rows_updated", "circ_rows_updated",
)


def resolve_backfill_range(
    end_date: str,
    days: int = DDE_WEEKLY_TREND_HISTORY_DAYS,
    since: str = MONEYFLOW_DC_MIN,
) -> tuple[str, str]:
    end_dt = datetime.strptime(end_date, "%Y%m%d")
    start = (end_dt - timedelta(days=days)).strftime("%Y%m%d")
    return max(since, start), end_date


def _resolve_universe(con, ts_codes: Optional[List[str]]) -> List[str]:
    if ts_codes:
        return [c for c in ts_codes if not c.endswith(".BJ")]
    return [c for c in get_all_active_codes(con) if not c.endswith(".BJ")]


def _count_days_in_ranges(ranges: list, all_days: list[str]) -> int:
    day_to_idx = {d: i for i, d in enumerate(all_days)}
    total = 0
    for start, end in ranges:
        if start in day_to_idx and end in day_to_idx:
            total += day_to_idx[end] - day_to_idx[start] + 1
    return total


def list_days_needing_dc_backfill(
    con, start: str, end: str, ts_codes: Optional[List[str]] = None,
) -> List[str]:
    code_filter = ""
    params: list = [start, end]
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        code_filter = f" AND ts_code IN ({ph})"
        params.extend(ts_codes)
    rows = con.execute(
        f"""
        SELECT DISTINCT trade_date
        FROM ods_moneyflow
        WHERE trade_date >= ? AND trade_date <= ?
          AND net_amount_dc IS NULL
          {code_filter}
        ORDER BY trade_date
        """,
        params,
    ).fetchall()
    return [r[0] for r in rows]


def list_days_needing_circ_backfill(
    con, start: str, end: str, ts_codes: Optional[List[str]] = None,
) -> List[str]:
    code_filter = ""
    params: list = [start, end]
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        code_filter = f" AND d.ts_code IN ({ph})"
        params.extend(ts_codes)
    rows = con.execute(
        f"""
        SELECT DISTINCT d.trade_date
        FROM ods_daily d
        LEFT JOIN ods_daily_basic b
          ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date
        WHERE d.trade_date >= ? AND d.trade_date <= ?
          AND b.circ_mv IS NULL
          {code_filter}
        ORDER BY d.trade_date
        """,
        params,
    ).fetchall()
    return [r[0] for r in rows]


def list_stocks_still_needing_backfill(
    con, start: str, end: str, ts_codes: Optional[List[str]] = None,
) -> List[str]:
    """Stocks with any remaining dc/circ NULL in range (for tail sweep)."""
    code_filter = ""
    params: list = [start, end]
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        code_filter = f" AND ts_code IN ({ph})"
        params.extend(ts_codes)

    dc_rows = con.execute(
        f"""
        SELECT DISTINCT ts_code FROM ods_moneyflow
        WHERE trade_date >= ? AND trade_date <= ?
          AND net_amount_dc IS NULL
          {code_filter}
        """,
        params,
    ).fetchall()

    circ_params: list = [start, end]
    circ_filter = ""
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        circ_filter = f" AND d.ts_code IN ({ph})"
        circ_params.extend(ts_codes)
    circ_rows = con.execute(
        f"""
        SELECT DISTINCT d.ts_code
        FROM ods_daily d
        LEFT JOIN ods_daily_basic b
          ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date
        WHERE d.trade_date >= ? AND d.trade_date <= ?
          AND b.circ_mv IS NULL
          {circ_filter}
        """,
        circ_params,
    ).fetchall()

    return sorted({r[0] for r in dc_rows} | {r[0] for r in circ_rows})


def _empty_stats(**extra) -> dict:
    base = {k: 0 for k in _STAT_KEYS}
    base.update(extra)
    return base


def _merge_stats(primary: dict, secondary: dict) -> dict:
    out = dict(primary)
    for k in _STAT_KEYS:
        out[k] = out.get(k, 0) + secondary.get(k, 0)
    for k, v in secondary.items():
        if k not in _STAT_KEYS:
            out[k] = v
    return out


def _backfill_days_chunk(
    trade_dates: list[str],
    dc_days: set,
    circ_days: set,
    ts_codes: Optional[List[str]],
    dry_run: bool,
    client=None,
) -> dict:
    """Worker: own DuckDB connection; optional injected client (workers=1 tests)."""
    from backend.config import DUCKDB_PATH
    from backend.fetch.client import TushareClient
    import duckdb

    local = _empty_stats()
    thread_con = duckdb.connect(DUCKDB_PATH)
    try:
        c = client or TushareClient()
        suffix = f"_{threading.current_thread().ident}"
        for td in trade_dates:
            need_dc = td in dc_days and td >= MONEYFLOW_DC_MIN
            need_circ = td in circ_days
            if dry_run:
                if need_dc:
                    local["dc_api_calls"] += 1
                if need_circ:
                    local["circ_api_calls"] += 1
                continue
            if need_dc:
                local["dc_api_calls"] += 1
                local["dc_rows_updated"] += _backfill_net_amount_dc_by_date(
                    thread_con, c, td,
                    ts_codes=ts_codes, register_suffix=suffix,
                )
            if need_circ:
                local["circ_api_calls"] += 1
                local["circ_rows_updated"] += _backfill_circ_mv_by_date(
                    thread_con, c, td,
                    ts_codes=ts_codes, register_suffix=suffix,
                )
        return local
    finally:
        thread_con.close()


def backfill_dde_meta_ods_by_date(
    con,
    client,
    ts_codes: Optional[List[str]],
    start: str,
    end: str,
    dry_run: bool = False,
    workers: int = 3,
    sync_dwd_batch: int = 0,
    on_batch_sync: Optional[Callable] = None,
) -> dict:
    """Plan C: parallel date-batched ODS backfill."""
    all_days = _local_trading_days(con, start, end)
    if all_days is None:
        raise RuntimeError(
            f"dim_date does not cover [{start}, {end}]; run fetch/build_dim first"
        )

    dc_days = set(list_days_needing_dc_backfill(con, start, end, ts_codes=ts_codes))
    circ_days = set(list_days_needing_circ_backfill(con, start, end, ts_codes=ts_codes))
    work_days = sorted(dc_days | circ_days)

    stats = _empty_stats(
        mode="date",
        days_total=len(all_days),
        days_work=len(work_days),
        days_skipped=len(all_days) - len(work_days),
        dwd_sync_batches=0,
    )

    from backend.etl.progress import day_progress

    prog = day_progress("fetch.dde_meta", len(work_days), detail="DDE按日补洞")
    prog.log_start(
        range=f"{start}~{end}",
        workers=workers,
        skipped_days=stats["days_skipped"],
    )

    if not work_days:
        prog.log_done(days=0)
        logger.info("backfill_dde_meta_ods_by_date complete: %s", stats)
        return stats

    completed = 0

    def _run_chunk(days_chunk: list[str]) -> dict:
        return _backfill_days_chunk(
            days_chunk, dc_days, circ_days, ts_codes, dry_run, client=client,
        )

    if workers <= 1:
        part = _run_chunk(work_days)
        for k in _STAT_KEYS:
            stats[k] += part[k]
        completed = len(work_days)
        prog.tick(len(work_days))
    else:
        chunk_size = max(1, len(work_days) // workers)
        chunks = [
            work_days[i:i + chunk_size]
            for i in range(0, len(work_days), chunk_size)
        ]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_run_chunk, ch): ch for ch in chunks}
            for fut in as_completed(futures):
                part = fut.result()
                for k in _STAT_KEYS:
                    stats[k] += part[k]
                completed += len(futures[fut])
                prog.tick(len(futures[fut]))
                if (
                    sync_dwd_batch > 0
                    and on_batch_sync is not None
                    and not dry_run
                    and completed % sync_dwd_batch == 0
                ):
                    on_batch_sync(con)
                    stats["dwd_sync_batches"] += 1

    if (
        sync_dwd_batch > 0
        and on_batch_sync is not None
        and not dry_run
        and completed % sync_dwd_batch != 0
    ):
        on_batch_sync(con)
        stats["dwd_sync_batches"] += 1

    prog.log_done(days=len(work_days))
    logger.info("backfill_dde_meta_ods_by_date complete: %s", stats)
    return stats


def _backfill_dde_meta_ods_by_stock(
    con,
    client,
    ts_codes: Optional[List[str]],
    start: str,
    end: str,
    dry_run: bool = False,
) -> dict:
    """Stock-batched fallback (--ts-code or tail sweep)."""
    codes = _resolve_universe(con, ts_codes)
    all_days = _local_trading_days(con, start, end)
    if all_days is None:
        raise RuntimeError(
            f"dim_date does not cover [{start}, {end}]; run fetch/build_dim first"
        )

    stats = _empty_stats(
        mode="stock",
        stocks=len(codes),
        dc_null_days=0,
        circ_null_days=0,
    )

    from backend.etl.progress import stock_progress

    prog = stock_progress("fetch.dde_meta", len(codes), detail="DDE按股补洞")
    prog.log_start(range=f"{start}~{end}")

    for ts_code in codes:
        dc_ranges = _get_net_amount_dc_null_ranges(con, ts_code, all_days)
        circ_ranges = _get_circ_mv_null_ranges(con, ts_code, all_days)
        stats["dc_null_days"] += _count_days_in_ranges(dc_ranges, all_days)
        stats["circ_null_days"] += _count_days_in_ranges(circ_ranges, all_days)

        if dry_run:
            if dc_ranges:
                stats["dc_api_calls"] += 1
            if circ_ranges:
                stats["circ_api_calls"] += 1
            prog.tick()
            continue

        for seg_start, seg_end in dc_ranges:
            stats["dc_api_calls"] += 1
            stats["dc_rows_updated"] += _backfill_net_amount_dc_stock(
                con, client, ts_code, seg_start, seg_end,
            )
        for seg_start, seg_end in circ_ranges:
            stats["circ_api_calls"] += 1
            stats["circ_rows_updated"] += _backfill_circ_mv_stock(
                con, client, ts_code, seg_start, seg_end,
            )
        prog.tick()

    prog.log_done(stocks=len(codes))
    logger.info("_backfill_dde_meta_ods_by_stock complete: %s", stats)
    return stats


def backfill_dde_meta_ods(
    con,
    client,
    ts_codes: Optional[List[str]],
    start: str,
    end: str,
    dry_run: bool = False,
    workers: int = 3,
    sync_dwd_batch: int = 0,
    on_batch_sync: Optional[Callable] = None,
) -> dict:
    """ODS backfill entry: date-batched full market, stock path for --ts-code."""
    if ts_codes:
        return _backfill_dde_meta_ods_by_stock(
            con, client, ts_codes, start, end, dry_run=dry_run,
        )

    stats = backfill_dde_meta_ods_by_date(
        con, client, None, start, end,
        dry_run=dry_run,
        workers=workers,
        sync_dwd_batch=sync_dwd_batch,
        on_batch_sync=on_batch_sync,
    )

    if dry_run:
        return stats

    tail_codes = list_stocks_still_needing_backfill(con, start, end)
    if tail_codes:
        logger.info(
            "backfill tail sweep: %d stocks still have dc/circ gaps",
            len(tail_codes),
        )
        tail = _backfill_dde_meta_ods_by_stock(
            con, client, tail_codes, start, end, dry_run=False,
        )
        stats = _merge_stats(stats, tail)
        stats["tail_stocks"] = len(tail_codes)

    return stats
