"""ETL orchestrator — wires together fetch, DIM, DWD, and DWS steps.

Usage:
    from backend.etl.orchestrator import run_etl
    run_etl()                             # full pipeline
    run_etl(step="fetch-ods")             # fetch only
    run_etl(step="calc-dws", ts_codes=["000001.SZ"])  # specific stocks
"""

import logging
import multiprocessing
import os
import threading
import time
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

from backend.db.connection import get_connection, check_connectivity, run_checkpoint
from backend.etl.error_handler import (
    log_etl_start, log_etl_end, log_etl_error, check_data_completeness as _check_ods_completeness,
)
from backend.etl.base import SkipReason, CalcResult
from backend.fetch.client import TushareClient
from backend.fetch.ods_daily import fetch_by_date_range_parallel, get_all_active_codes
from backend.etl.build_dim import build_dim_stock, build_dim_date, build_dim_concept
from backend.etl.build_dwd import rebuild_all_dwd, rebuild_dwd_for_stale
from backend.etl.calc_macd import MACDCalculator
from backend.etl.calc_ma import MACalculator
from backend.etl.calc_kpattern import KPatternCalculator
from backend.etl.calc_dde import DDECalculator
from backend.etl.calc_volume import VolumeCalculator
from backend.etl.calc_price_position import PricePositionCalculator

CALCULATORS = [MACDCalculator, MACalculator, KPatternCalculator,
               DDECalculator, VolumeCalculator, PricePositionCalculator]

QUOTE_COLUMNS = [
    "trade_date", "open_qfq", "high_qfq", "low_qfq",
    "close_qfq", "vol", "pct_chg",
]
QUOTE_CALCULATORS = [
    MACDCalculator, MACalculator, KPatternCalculator,
    VolumeCalculator, PricePositionCalculator,
]


def resolve_calc_workers() -> int:
    """Resolve calc parallelism: CALC_WORKERS env or min(cpu-1, 8).

    CALC_WORKERS controls the number of calc *threads* (not processes).
    DuckDB's single-file lock forbids concurrent read-write processes, so calc
    parallelism is thread-based, sharing one in-process DuckDB instance.
    Invalid (non-integer) values fall back to the default with a warning.
    """
    env = os.getenv("CALC_WORKERS", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            logger.warning("Invalid CALC_WORKERS=%r, falling back to default", env)
    return max(1, min(multiprocessing.cpu_count() - 1, 8))



def run_etl(step: str = "build-all", ts_codes: Optional[list[str]] = None,
            start: Optional[str] = None, end: Optional[str] = None,
            batch_size: int = 100, force_full: bool = False):
    """Run the ETL pipeline.

    NOTE: Legacy full-pipeline entry point. For calc-only with skip-classification
    and auto-fetch, use run_calc(). This function ignores CalcResult from calculate().

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
        lid, t0 = log_etl_start(con, "health_check")
        if "fatal" in health.get("duckdb", ""):
            log_etl_end(con, lid, "health_check", t0, "failed",
                        error_msg=health["duckdb"])
            raise RuntimeError(health["duckdb"])
        log_etl_end(con, lid, "health_check", t0, "success",
                    error_msg=f"DuckDB v{health['version']}, "
                              f"{health['disk_free_mb']}MB free")

        # 1. Determine what to run
        if step in ("fetch-ods", "build-all"):
            client = TushareClient()
            # Global dimension data — always needed regardless of --ts-code
            from backend.fetch.ods_stock_basic import fetch_stock_basic
            from backend.fetch.ods_trade_cal import fetch_trade_cal
            from backend.fetch.ods_concept import fetch_concept_detail

            lid, t0 = log_etl_start(con, "fetch_stock_basic")
            n = fetch_stock_basic(client, con)
            log_etl_end(con, lid, "fetch_stock_basic", t0, "success", row_count=n)

            lid, t0 = log_etl_start(con, "fetch_trade_cal")
            n = fetch_trade_cal(client, con)
            log_etl_end(con, lid, "fetch_trade_cal", t0, "success", row_count=n)

            codes = ts_codes or get_all_active_codes(con)
            # Date-based batch fetch FIRST — 4 API calls/day for ALL stocks (~30s)
            lid, t0 = log_etl_start(con, "fetch_market_data",
                                     min_trade_date=start, max_trade_date=end)
            rows = fetch_by_date_range_parallel(
                start or "20150101", end or "20991231", workers=3,
                ts_codes=codes, con=con)
            log_etl_end(con, lid, "fetch_market_data", t0, "success",
                        row_count=int(rows),
                        min_trade_date=start, max_trade_date=end)

            # Concept detail LAST — per-stock calls, low priority, skip on failure
            lid, t0 = log_etl_start(con, "fetch_concept_detail")
            try:
                n = fetch_concept_detail(client, con, ts_codes=codes)
                log_etl_end(con, lid, "fetch_concept_detail", t0, "success", row_count=n)
            except Exception as e:
                log_etl_end(con, lid, "fetch_concept_detail", t0, "degraded",
                            error_msg=f"skipped (rate limited): {e}")

            # Run data completeness check after fetch
            lid, t0 = log_etl_start(con, "data_completeness_check")
            comp = _check_ods_completeness(con)
            log_etl_end(con, lid, "data_completeness_check", t0, "success",
                        data_completeness=comp)

        if step in ("build-dim", "build-all"):
            for dim_step, fn in [
                ("build_dim_stock", build_dim_stock),
                ("build_dim_date", build_dim_date),
                ("build_dim_concept", build_dim_concept),
            ]:
                lid, t0 = log_etl_start(con, dim_step)
                try:
                    if dim_step == "build_dim_concept":
                        nc, nm = fn(con)
                        n = nc + nm
                    else:
                        n = fn(con)
                    log_etl_end(con, lid, dim_step, t0, "success", row_count=n)
                except Exception as e:
                    log_etl_error(con, lid, dim_step, t0, 0, e)
                    raise

        if step in ("build-dwd", "build-all"):
            codes = ts_codes or get_all_active_codes(con)
            lid, t0 = log_etl_start(con, "build_dwd")
            try:
                result = rebuild_all_dwd(con, codes)
                for name, n in result.items():
                    log_etl_end(con, lid, f"build_dwd_{name}", t0, "success", row_count=n)
            except Exception as e:
                log_etl_error(con, lid, "build_dwd", t0, 0, e)
                raise

        if step in ("calc-dws", "build-all"):
            codes = ts_codes or get_all_active_codes(con)
            lid, t0 = log_etl_start(con, "calc_dws")
            try:
                calc_date = datetime.now().strftime("%Y%m%d")
                n_batches = (len(codes) + batch_size - 1) // batch_size
                grand_total = 0
                calc_start = time.monotonic()

                for CalcCls in CALCULATORS:
                    for freq in ("daily", "weekly"):
                        calc = CalcCls(con, freq)
                        label = f"{CalcCls.__name__} {freq}"
                        t0 = time.monotonic()
                        last_pct = -1

                        for i in range(0, len(codes), batch_size):
                            batch = codes[i:i + batch_size]
                            calc.calculate(batch, calc_date)

                            done = min(i + batch_size, len(codes))
                            pct = done * 100 // len(codes)
                            # Log every 5% milestone (and 100%)
                            if pct - last_pct >= 5 or pct == 100:
                                last_pct = pct
                                elapsed = time.monotonic() - t0
                                rate = done / elapsed if elapsed > 0 else 0
                                logger.info(
                                    "calc_dws %-30s %d/%d (%d%%) — %.0fs, %.0f stk/s",
                                    label, done, len(codes), pct, elapsed, rate,
                                )

                        elapsed = time.monotonic() - t0
                        n = con.execute(
                            f"SELECT COUNT(*) FROM {calc.dws_table} "
                            f"WHERE calc_date = ?", (calc_date,),
                        ).fetchone()[0]
                        grand_total += n
                        logger.info(
                            "calc_dws %-30s DONE — %d rows, %.0fs",
                            label, n, elapsed,
                        )

                total_elapsed = time.monotonic() - calc_start
                logger.info(
                    "calc_dws ALL DONE — %d stocks × 5 indicators × 2 freqs, "
                    "%d rows, %.0fs",
                    len(codes), grand_total, total_elapsed,
                )
                log_etl_end(con, lid, "calc_dws", t0, "success", row_count=grand_total)
            except Exception as e:
                log_etl_error(con, lid, "calc_dws", t0, 0, e)
                raise

        # Final checkpoint
        run_checkpoint(con)
    finally:
        con.close()


def _derive_warmup_constants():
    from backend.etl.recalc_spec import resolve_warmup_tdays, resolve_weekly_warmup_weeks
    return resolve_warmup_tdays(), resolve_weekly_warmup_weeks()


WARMUP_TDAYS, WEEKLY_WARMUP_WEEKS = _derive_warmup_constants()


def resolve_recalc_start(con, calc_date: str, freq: str) -> Optional[str]:
    """Backtrack resolve_recalc_bars trade/week-end days from calc_date via dim_date."""
    from backend.etl.recalc_spec import collect_specs, resolve_recalc_bars

    n_bars = resolve_recalc_bars(collect_specs(freq))
    if freq == "weekly":
        row = con.execute("""
            SELECT trade_date FROM (
                SELECT trade_date,
                       ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
                FROM dim_date
                WHERE is_trade_day = 1 AND is_week_end = 1 AND trade_date <= ?
            ) WHERE rn = ?
        """, [calc_date, n_bars]).fetchone()
    else:
        row = con.execute("""
            SELECT trade_date FROM (
                SELECT trade_date,
                       ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
                FROM dim_date
                WHERE is_trade_day = 1 AND trade_date <= ?
            ) WHERE rn = ?
        """, [calc_date, n_bars]).fetchone()
    return row[0] if row else None


def resolve_weekly_warmup_start(con, end_date: str,
                                  n_weeks: int = WEEKLY_WARMUP_WEEKS):
    """Return trade_date of the n_weeks-th week-end bar looking back from end_date."""
    row = con.execute("""
        SELECT trade_date FROM (
            SELECT trade_date,
                   ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
            FROM dim_date
            WHERE is_trade_day = 1 AND is_week_end = 1 AND trade_date <= ?
        ) WHERE rn = ?
    """, [end_date, n_weeks]).fetchone()
    return row[0] if row else None


def _needed_history_start(con, end_date: str, list_date: str = None,
                          daily_lookback: int = WARMUP_TDAYS,
                          include_weekly: bool = True) -> Optional[str]:
    """Earliest trade_date required for daily+weekly warmup (more history = smaller date)."""
    daily_row = con.execute("""
        SELECT trade_date FROM (
            SELECT trade_date,
                   ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
            FROM dim_date WHERE is_trade_day = 1 AND trade_date <= ?
        ) WHERE rn = ?
    """, [end_date, daily_lookback]).fetchone()
    daily_start = daily_row[0] if daily_row else None

    weekly_start = None
    if include_weekly and daily_lookback == WARMUP_TDAYS:
        weekly_start = resolve_weekly_warmup_start(con, end_date)

    if daily_start and weekly_start:
        needed = min(daily_start, weekly_start)
    else:
        needed = daily_start or weekly_start
    if not needed:
        return None
    if list_date and list_date > needed:
        return list_date
    return needed


def _available_week_ends_batch(con, ts_codes: list[str], end_date: str) -> dict:
    """Count week-end bars on dim_date between list_date and end_date (inclusive)."""
    if not ts_codes:
        return {}
    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(f"""
        SELECT s.ts_code, COUNT(d.trade_date)
        FROM dim_stock s
        LEFT JOIN dim_date d
          ON d.is_trade_day = 1 AND d.is_week_end = 1
         AND d.trade_date <= ?
         AND (s.list_date IS NULL OR d.trade_date >= s.list_date)
        WHERE s.ts_code IN ({placeholders})
        GROUP BY s.ts_code
    """, [end_date] + ts_codes).fetchall()
    return {r[0]: r[1] for r in rows}


def _count_week_end_bars_batch(con, ts_codes: list[str],
                               calc_date: str) -> dict:
    """Count week-end bars in dwd_weekly within [weekly_warmup_start, calc_date].

    Aligns with resolve_weekly_warmup_start / fetch history window; per-stock
    floor is max(weekly_start, list_date).
    """
    if not ts_codes:
        return {}
    weekly_start = resolve_weekly_warmup_start(con, calc_date)
    if not weekly_start:
        return {code: 0 for code in ts_codes}
    try:
        placeholders = ",".join(["?" for _ in ts_codes])
        rows = con.execute(f"""
            SELECT s.ts_code, COUNT(d.trade_date)
            FROM dim_stock s
            LEFT JOIN dwd_weekly_quote w ON w.ts_code = s.ts_code
            LEFT JOIN dim_date d
              ON d.trade_date = w.trade_date
             AND d.is_week_end = 1
             AND w.trade_date <= ?
             AND w.trade_date >= GREATEST(?, COALESCE(s.list_date, ?))
            WHERE s.ts_code IN ({placeholders})
            GROUP BY s.ts_code
        """, [calc_date, weekly_start, weekly_start] + ts_codes).fetchall()
        result = {r[0]: r[1] for r in rows}
        for code in ts_codes:
            result.setdefault(code, 0)
        return result
    except Exception:
        return {}


def check_data_completeness(con, ts_codes: list[str],
                             calc_date: str = None,
                             min_daily_rows: int = WARMUP_TDAYS,
                             min_week_end_bars: int = WEEKLY_WARMUP_WEEKS) -> dict:
    """检查 DWD 完整度，拆分 calc 准入与 weekly fetch 需求。

    返回:
        {
            "ok": [...],              # daily_ok → 可进入 calc
            "missing": {...},         # NOT daily_ok → 不可 calc，可 auto-fetch
            "weekly_fetch": {...},    # daily_ok 但成熟股 week-end 仍不足 → 仅 fetch
        }
    """
    ok = []
    missing = {}
    weekly_fetch = {}

    if not ts_codes:
        return {"ok": ok, "missing": missing, "weekly_fetch": weekly_fetch}

    if calc_date is None:
        row = con.execute(
            "SELECT MAX(trade_date) FROM dim_date WHERE is_trade_day = 1"
        ).fetchone()
        calc_date = row[0] if row and row[0] else datetime.now().strftime("%Y%m%d")

    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(f"""
        SELECT ts_code, COUNT(*), MIN(trade_date), MAX(trade_date)
        FROM dwd_daily_quote WHERE ts_code IN ({placeholders})
        GROUP BY ts_code
    """, ts_codes).fetchall()

    dwd_data = {r[0]: {"dwd_rows": r[1], "min_date": r[2], "max_date": r[3]}
                for r in rows}

    week_end_counts = _count_week_end_bars_batch(con, ts_codes, calc_date)
    available_counts = _available_week_ends_batch(con, ts_codes, calc_date)

    for ts_code in ts_codes:
        info = dwd_data.get(ts_code)
        dwd_rows = info["dwd_rows"] if info else 0
        week_end_bars = week_end_counts.get(ts_code, 0)
        available_we = available_counts.get(ts_code, 0)
        weekly_required = min(min_week_end_bars, available_we)
        daily_ok = info is not None and dwd_rows >= min_daily_rows
        weekly_ok = week_end_bars >= weekly_required

        base = {
            "dwd_rows": dwd_rows,
            "week_end_bars": week_end_bars,
            "weekly_required": weekly_required,
            "available_week_ends": available_we,
            "min_date": info["min_date"] if info else None,
            "max_date": info["max_date"] if info else None,
        }

        if daily_ok:
            ok.append(ts_code)
            if not weekly_ok and available_we >= min_week_end_bars:
                weekly_fetch[ts_code] = {**base, "reason": "weekly_warmup"}
        else:
            if not weekly_ok:
                reason = "both"
            else:
                reason = "daily_warmup"
            missing[ts_code] = {**base, "reason": reason}

    return {"ok": ok, "missing": missing, "weekly_fetch": weekly_fetch}


def find_stale_ods_codes(con, ts_codes: list[str], analysis_date: str) -> list[str]:
    """Stocks that should have ODS through analysis_date but do not."""
    if not ts_codes:
        return []

    placeholders = ",".join(["?" for _ in ts_codes])
    params = list(ts_codes) + list(ts_codes)
    rows = con.execute(f"""
        SELECT s.ts_code, s.list_date, s.delist_date, o.ods_max
        FROM dim_stock s
        LEFT JOIN (
            SELECT ts_code, MAX(trade_date) AS ods_max
            FROM ods_daily
            WHERE ts_code IN ({placeholders})
            GROUP BY ts_code
        ) o ON s.ts_code = o.ts_code
        WHERE s.ts_code IN ({placeholders})
    """, params).fetchall()

    stale = []
    for ts_code, list_date, delist_date, ods_max in rows:
        if list_date and list_date > analysis_date:
            continue
        if delist_date and delist_date < analysis_date:
            continue
        if not ods_max or ods_max < analysis_date:
            stale.append(ts_code)
    return stale


def find_stale_dwd_codes(con, ts_codes: list[str], analysis_date: str) -> list[str]:
    """Stocks with ODS on analysis_date but any DWD layer max trade_date behind.

    Checks daily_quote, weekly_quote, and moneyflow (only when ODS moneyflow exists
    on analysis_date — BSE etc. without moneyflow are not flagged).
    """
    if not ts_codes:
        return []

    placeholders = ",".join(["?" for _ in ts_codes])
    params = (
        list(ts_codes) + list(ts_codes) + list(ts_codes)
        + [analysis_date] + list(ts_codes)
        + [analysis_date, analysis_date, analysis_date, analysis_date]
    )
    rows = con.execute(f"""
        SELECT o.ts_code
        FROM ods_daily o
        LEFT JOIN (
            SELECT ts_code, MAX(trade_date) AS dwd_max
            FROM dwd_daily_quote
            WHERE ts_code IN ({placeholders})
            GROUP BY ts_code
        ) dq ON o.ts_code = dq.ts_code
        LEFT JOIN (
            SELECT ts_code, MAX(trade_date) AS dwd_max
            FROM dwd_weekly_quote
            WHERE ts_code IN ({placeholders})
            GROUP BY ts_code
        ) wq ON o.ts_code = wq.ts_code
        LEFT JOIN (
            SELECT ts_code, MAX(trade_date) AS dwd_max
            FROM dwd_daily_moneyflow
            WHERE ts_code IN ({placeholders})
            GROUP BY ts_code
        ) mf ON o.ts_code = mf.ts_code
        WHERE o.trade_date = ?
          AND o.ts_code IN ({placeholders})
          AND (
              dq.dwd_max IS NULL OR dq.dwd_max < ?
              OR wq.dwd_max IS NULL OR wq.dwd_max < ?
              OR (
                  EXISTS (
                      SELECT 1 FROM ods_moneyflow om
                      WHERE om.ts_code = o.ts_code AND om.trade_date = ?
                  )
                  AND (mf.dwd_max IS NULL OR mf.dwd_max < ?)
              )
          )
    """, params).fetchall()
    return [r[0] for r in rows]


def _auto_fetch_stale_ods(con, stale_codes: list[str], analysis_date: str) -> int:
    """Fetch missing tail ODS for stale stocks; rebuild their DWD."""
    from backend.fetch.ods_daily import fetch_stocks_incremental

    if not stale_codes:
        return 0

    placeholders = ",".join(["?" for _ in stale_codes])
    max_rows = con.execute(f"""
        SELECT ts_code, MAX(trade_date) FROM ods_daily
        WHERE ts_code IN ({placeholders})
        GROUP BY ts_code
    """, stale_codes).fetchall()
    ods_max = {r[0]: r[1] for r in max_rows}

    gap_starts = []
    for ts_code in stale_codes:
        max_ods = ods_max.get(ts_code)
        if not max_ods:
            row = con.execute(
                "SELECT list_date FROM dim_stock WHERE ts_code = ?", (ts_code,)
            ).fetchone()
            gap_starts.append(row[0] if row and row[0] else analysis_date)
            continue
        next_day = con.execute("""
            SELECT MIN(trade_date) FROM dim_date
            WHERE is_trade_day = 1 AND trade_date > ? AND trade_date <= ?
        """, (max_ods, analysis_date)).fetchone()[0]
        gap_starts.append(next_day or analysis_date)

    seg_start = min(gap_starts)
    tdays = _count_trading_days(con, seg_start, analysis_date)
    client = TushareClient()

    from backend.etl.progress import log_timed_step

    mode = "date-batched" if _choose_fetch_strategy(len(stale_codes), tdays) else "stock-batched"
    logger.info(
        "progress calc.stale_fetch: started | %s~%s | stocks=%d | mode=%s",
        seg_start, analysis_date, len(stale_codes), mode,
    )

    def _fetch_stale():
        if mode == "date-batched":
            return fetch_by_date_range_parallel(
                seg_start, analysis_date, workers=3,
                ts_codes=stale_codes, con=con,
            )
        return fetch_stocks_incremental(
            client, con, stale_codes, start=seg_start, end=analysis_date,
        )

    n_fetched = log_timed_step(
        "calc.stale_fetch", "ods",
        _fetch_stale,
        stocks=len(stale_codes),
        extra=f"{seg_start}~{analysis_date}",
    )
    log_timed_step(
        "calc.stale_fetch", "rebuild_dwd",
        lambda: rebuild_dwd_for_stale(con, stale_codes, analysis_date),
        stocks=len(stale_codes),
    )
    return int(n_fetched)


def _compute_fetch_range(con, ts_code: str, calc_date: str,
                          lookback_tdays: int = WARMUP_TDAYS) -> tuple:
    """Compute the date range that needs to be fetched for a stock.

    Start = max(list_date, calc_date往前推lookback_tdays个交易日)
    End   = min(calc_date, delist_date)

    Returns (needed_start, needed_end) or (None, None) if already covered.
    """
    # 1. stock lifecycle dates
    row = con.execute("""
        SELECT list_date, delist_date FROM dim_stock WHERE ts_code = ?
    """, (ts_code,)).fetchone()
    if not row:
        logger.warning("_compute_fetch_range: %s not found in dim_stock, skipping", ts_code)
        return (None, None)
    list_date, delist_date = row

    # 2. end_date: delisted stock stops at delist_date, otherwise calc_date
    end_date = calc_date
    if delist_date and delist_date < calc_date:
        end_date = delist_date

    # 3. needed start: daily + weekly warmup (weekly only when using default lookback)
    include_weekly = (lookback_tdays == WARMUP_TDAYS)
    needed_start = _needed_history_start(
        con, end_date, list_date,
        daily_lookback=lookback_tdays,
        include_weekly=include_weekly,
    )
    if not needed_start:
        return (None, None)

    # 4. check if already covered by existing ODS data
    actual = con.execute("""
        SELECT COUNT(DISTINCT trade_date) FROM ods_daily
        WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ?
    """, (ts_code, needed_start, end_date)).fetchone()[0]

    expected = con.execute("""
        SELECT COUNT(*) FROM dim_date
        WHERE is_trade_day = 1 AND trade_date >= ? AND trade_date <= ?
    """, (needed_start, end_date)).fetchone()[0]

    if actual > 0 and actual >= expected:
        return (None, None)  # 100% coverage required (aligned with 123 project strict check)

    return (needed_start, end_date)


def _count_trading_days(con, start: str, end: str) -> int:
    """Return number of trading days between start and end (inclusive)."""
    row = con.execute("""
        SELECT COUNT(*) FROM dim_date
        WHERE is_trade_day = 1 AND trade_date >= ? AND trade_date <= ?
    """, (start, end)).fetchone()
    return row[0] if row else 0


def _choose_fetch_strategy(n_stocks: int, n_tdays: int) -> bool:
    """Choose between date-batched (True) and stock-batched (False) mode.

    Stock-batched cost: n_stocks × 4 API calls (per-stock adj_factor/daily/daily_basic/moneyflow).
    Date-batched cost: n_tdays × 4 API calls (per-date, returns all stocks).

    Pick the mode with fewer API calls. When equal, stock-batched is more targeted.
    """
    return n_stocks > n_tdays


def _filter_delisted(con, ts_codes: list[str], calc_date: str) -> tuple:
    """Filter out delisted stocks that already have DWS data.

    Returns (active_codes, delisted_dict).
    """
    active = []
    delisted = {}
    for ts_code in ts_codes:
        row = con.execute(
            "SELECT delist_date FROM dim_stock WHERE ts_code = ?", (ts_code,)
        ).fetchone()
        if not row or not row[0]:
            active.append(ts_code)
            continue
        delist_date = row[0]
        if delist_date >= calc_date:
            active.append(ts_code)
            continue
        # delisted: check if DWS already exists
        has_dws = con.execute(
            "SELECT 1 FROM dws_macd_daily WHERE ts_code = ? LIMIT 1", (ts_code,)
        ).fetchone()
        if has_dws:
            delisted[ts_code] = f"delisted={delist_date}, DWS exists, skip"
        else:
            active.append(ts_code)
            logger.info("Delisted stock %s (%s) — first calc, including",
                        ts_code, delist_date)
    return active, delisted


def _classify_still_missing(con, missing: dict) -> dict:
    """Classify still-missing stocks after fetch+rebuild into root cause categories."""
    classified = {}
    for ts_code, info in missing.items():
        dwd_rows = info["dwd_rows"]
        if dwd_rows == 0:
            if ts_code.endswith(".BJ"):
                reason = SkipReason.SOURCE_UNAVAILABLE
                detail = "BSE stock: DWD data unavailable (tushare may not support)"
            else:
                reason = SkipReason.NO_DWD_DATA
                detail = "DWD rows=0 after fetch+rebuild"
        else:
            reason = SkipReason.INSUFFICIENT_ROWS
            detail = (
                f"DWD rows={dwd_rows}, week_end_bars={info.get('week_end_bars', '?')}"
                f"/{info.get('weekly_required', '?')}"
                f" (min_date={info['min_date']}, max_date={info['max_date']},"
                f" reason={info.get('reason', '?')})"
            )
        if reason not in classified:
            classified[reason] = []
        classified[reason].append((ts_code, detail))
    return classified


def _write_skip_log_batch(con, calc_date: str, indicator: str, freq: str,
                           classified: dict, *, verbose: bool = True):
    """Write classified skip reasons to ods_calc_skip_log."""
    if not verbose:
        items = classified.get(SkipReason.FINGERPRINT_MATCH, [])
        if items and len(items) > 100 and len(classified) == 1:
            con.execute(
                """INSERT OR REPLACE INTO ods_calc_skip_log
                   (calc_date, ts_code, indicator, freq, reason, detail)
                   VALUES (?, '__batch__', ?, ?, ?, ?)""",
                [calc_date, indicator, freq, SkipReason.FINGERPRINT_MATCH.value,
                 f"batch_skip={len(items)}"],
            )
            return
    rows = []
    for reason, items in classified.items():
        for ts_code, detail in items:
            rows.append((calc_date, ts_code, indicator, freq, reason.value, detail))
    if rows:
        con.executemany(
            """INSERT OR REPLACE INTO ods_calc_skip_log
               (calc_date, ts_code, indicator, freq, reason, detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )


def _route_calc(con, calc, name: str, freq: str, ts_code: str, df,
                calc_date: str, recalc_start: Optional[str],
                quote_groups, append_on: bool) -> CalcResult:
    """Route one (calculator, freq) to SKIP / APPEND / FULL and refresh state.

    APPEND computes only new bars (vectorized, seeded); FULL is the existing
    narrow-window recompute (and the equivalence oracle); SKIP is taken when no
    new bar appeared and the fixed-window history signature is unchanged.
    State (dws_calc_state) is refreshed on SKIP (signature match), APPEND, and FULL
    writes so routing state never drifts from the frames used to classify.
    """
    from backend.etl.calc_state import load_calc_state, write_calc_state_from_df
    from backend.etl.calc_router import classify_calc_mode

    def _full() -> CalcResult:
        if name == "dde":
            return calc.calculate([ts_code], calc_date, recalc_start=recalc_start)
        return calc.calculate([ts_code], calc_date, recalc_start=recalc_start,
                              quote_groups=quote_groups)

    if not append_on or df is None or len(df) == 0:
        return _full()

    state = load_calc_state(con, freq, name, [ts_code]).get(ts_code)
    spec_version = getattr(calc, "SPEC_VERSION", "v1")
    mode, new_bars = classify_calc_mode(
        df, state, calc.SIGNATURE_COLS, expected_spec_version=spec_version)

    if mode == "SKIP":
        r = CalcResult()
        r.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                   "append: no new bars, signature match")
        if state is not None:
            write_calc_state_from_df(
                con, ts_code, freq, name, df, calc.SIGNATURE_COLS, calc_date,
                last_trade_date=state["last_trade_date"],
                spec_version=spec_version,
            )
        return r

    if mode == "APPEND":
        result = calc.append_calculate(ts_code, df, new_bars, calc_date, state)
    else:  # FULL
        result = _full()

    if result.calculated > 0 and df is not None and len(df) > 0:
        write_calc_state_from_df(
            con, ts_code, freq, name, df, calc.SIGNATURE_COLS, calc_date,
            spec_version=spec_version,
        )
    return result


def calc_stock_pipeline(con, ts_code: str, calc_date: str,
                        daily_recalc: Optional[str] = None,
                        weekly_recalc: Optional[str] = None) -> list:
    """Run all 12 indicator×freq calcs for one stock with shared quote loads.

    Each (indicator, freq) is routed SKIP / APPEND / FULL by _route_calc when
    CALC_APPEND is on and an incremental window is active. Returns a list of
    (indicator_name, freq, CalcResult) tuples.
    """
    from backend.etl.base import load_quote_groups
    from backend.etl.recalc_spec import resolve_load_start
    from backend.config import CALC_APPEND
    from backend.etl.calc_indicators import quote_pipeline_columns

    outputs = []
    for freq, recalc_start in (("daily", daily_recalc), ("weekly", weekly_recalc)):
        load_start = None
        if recalc_start:
            load_start = resolve_load_start(con, recalc_start, freq)
        src = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        quote_groups = load_quote_groups(
            con, src, freq, quote_pipeline_columns(freq), [ts_code],
            start_date=load_start,
        )
        append_on = CALC_APPEND and recalc_start is not None
        qdf = quote_groups.get(ts_code)

        for CalcCls in QUOTE_CALCULATORS:
            calc = CalcCls(con, freq)
            name = CalcCls.__name__.replace("Calculator", "").lower()
            result = _route_calc(con, calc, name, freq, ts_code, qdf, calc_date,
                                 recalc_start, quote_groups, append_on)
            outputs.append((name, freq, result))

        dde = DDECalculator(con, freq)
        if freq == "daily":
            dde_groups = dde._load_daily_batch([ts_code], start_date=load_start)
        else:
            dde_groups = dde._load_weekly_batch([ts_code], start_date=load_start)
        ddf = dde_groups.get(ts_code)
        result = _route_calc(con, dde, "dde", freq, ts_code, ddf, calc_date,
                             recalc_start, None, append_on)
        outputs.append(("dde", freq, result))

    return outputs


def calc_stock_pipeline_selective(
    con, ts_code: str, calc_date: str,
    daily_recalc: Optional[str] = None,
    weekly_recalc: Optional[str] = None,
    run_keys: Optional[set] = None,
    run_modes: Optional[dict] = None,
    tail_frames: Optional[dict] = None,
) -> list:
    """Run only (indicator, freq) in run_keys.

    Loads are narrowed by (freq, source): quote and DDE are fetched separately.
    APPEND indicators reuse preflight tail_frames when provided; FULL uses the
    narrow recalc window only for groups that need it.
    """
    from backend.etl.base import load_quote_groups
    from backend.etl.recalc_spec import resolve_load_start
    from backend.config import CALC_APPEND
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS

    if not run_keys:
        return []

    outputs = []
    for indicator_name, freq, CalcCls, _, source in CALC_ROUTE_SPECS:
        if (indicator_name, freq) not in run_keys:
            continue
        recalc_start = daily_recalc if freq == "daily" else weekly_recalc
        if recalc_start is None:
            continue
        load_start = resolve_load_start(con, recalc_start, freq)
        append_on = CALC_APPEND and recalc_start is not None
        mode = "FULL"
        if run_modes is not None:
            mode = run_modes.get((indicator_name, freq), ("FULL", []))[0]

        df = None
        quote_groups = None
        if source == "quote":
            if tail_frames is not None:
                df = tail_frames.get((freq, "quote"))
                if df is not None and len(df) > 0:
                    quote_groups = {ts_code: df}
            if df is None or len(df) == 0:
                src = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
                start = load_start if mode == "FULL" else load_start
                from backend.etl.calc_indicators import quote_pipeline_columns
                quote_groups = load_quote_groups(
                    con, src, freq, quote_pipeline_columns(freq), [ts_code],
                    start_date=start,
                )
                df = quote_groups.get(ts_code)
        else:
            if tail_frames is not None:
                df = tail_frames.get((freq, "dde"))
            if df is None or len(df) == 0:
                dde = DDECalculator(con, freq)
                if freq == "daily":
                    dde_groups = dde._load_daily_batch(
                        [ts_code], start_date=load_start if mode == "FULL" else load_start,
                    )
                else:
                    dde_groups = dde._load_weekly_batch(
                        [ts_code], start_date=load_start if mode == "FULL" else load_start,
                    )
                df = dde_groups.get(ts_code)

        calc = CalcCls(con, freq)
        if source == "quote":
            result = _route_calc(
                con, calc, indicator_name, freq, ts_code, df, calc_date,
                recalc_start, quote_groups, append_on,
            )
        else:
            result = _route_calc(
                con, calc, "dde", freq, ts_code, df, calc_date,
                recalc_start, None, append_on,
            )
        outputs.append((indicator_name, freq, result))
    return outputs


from backend.etl.progress import StageProgress

_calc_progress: Optional[StageProgress] = None


def _init_calc_progress(total: int, **start_extra: object) -> None:
    """Reset shared calc progress before a multi-threaded run."""
    global _calc_progress
    _calc_progress = StageProgress("calc.stocks", total, unit="stocks")
    _calc_progress.log_start(**start_extra)


def _report_calc_progress() -> None:
    """Thread-safe per-stock tick (true global total across workers)."""
    if _calc_progress is not None:
        _calc_progress.tick()


def _count_calc_rows(con, table: str, calc_date: str, ts_codes: list) -> int:
    """Count DWS rows written for a specific calc_date AND ts_code set.

    Scoped by ts_code so per-chunk totals are disjoint — summing chunk results
    yields the true grand total (the old whole-table COUNT double-counted across
    threads/chunks).
    """
    if not ts_codes:
        return 0
    ph = ",".join(["?"] * len(ts_codes))
    return con.execute(
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE calc_date = ? AND ts_code IN ({ph})",
        [calc_date] + list(ts_codes),
    ).fetchone()[0]


def _calc_full_work_chunk(
    work_items: list,
    calc_date: str,
    incremental: bool = True,
    batch_ctx: Optional[dict] = None,
) -> int:
    """Worker: run FULL calc for indicator-level work items in a dedicated connection."""
    import duckdb
    from backend.config import DUCKDB_PATH

    if not work_items:
        return 0

    con = duckdb.connect(DUCKDB_PATH)
    try:
        if incremental:
            daily_recalc = resolve_recalc_start(con, calc_date, "daily")
            weekly_recalc = resolve_recalc_start(con, calc_date, "weekly")
        else:
            daily_recalc = weekly_recalc = None

        from collections import defaultdict

        ts_codes = list({ts for ts, _ in work_items})
        agg_by_key = defaultdict(CalcResult)

        daily_tails = weekly_tails = dde_daily = dde_weekly = {}
        stock_modes = {}
        if batch_ctx is not None:
            stock_modes = batch_ctx.get("stock_modes", {})
            daily_tails = {
                c: batch_ctx["daily_tails"][c] for c in ts_codes
                if c in batch_ctx.get("daily_tails", {})
            }
            weekly_tails = {
                c: batch_ctx["weekly_tails"][c] for c in ts_codes
                if c in batch_ctx.get("weekly_tails", {})
            }
            dde_daily = {
                c: batch_ctx["dde_daily"][c] for c in ts_codes
                if c in batch_ctx.get("dde_daily", {})
            }
            dde_weekly = {
                c: batch_ctx["dde_weekly"][c] for c in ts_codes
                if c in batch_ctx.get("dde_weekly", {})
            }

        logger.info(
            "progress calc.chunk: started | work_items=%d | stocks=%d",
            len(work_items), len(ts_codes),
        )

        for ts_code, (indicator_name, freq) in work_items:
            run_keys = {(indicator_name, freq)}
            run_modes = {(indicator_name, freq): ("FULL", [])}
            if ts_code in stock_modes:
                mode_entry = stock_modes[ts_code].get((indicator_name, freq))
                if mode_entry:
                    run_modes = {(indicator_name, freq): mode_entry}

            tail_frames = {
                ("daily", "quote"): daily_tails.get(ts_code),
                ("weekly", "quote"): weekly_tails.get(ts_code),
                ("daily", "dde"): dde_daily.get(ts_code),
                ("weekly", "dde"): dde_weekly.get(ts_code),
            }
            for ind, f, result in calc_stock_pipeline_selective(
                    con, ts_code, calc_date, daily_recalc, weekly_recalc,
                    run_keys=run_keys, run_modes=run_modes,
                    tail_frames=tail_frames):
                key = (ind, f)
                agg = agg_by_key[key]
                agg.calculated += result.calculated
                for reason, items in result.skipped.items():
                    for code, detail in items:
                        agg.add_skip(reason, code, detail)
            _report_calc_progress()

        from backend.config import CALC_SKIP_LOG_VERBOSE

        chunk_total = 0
        for CalcCls in CALCULATORS:
            indicator_name = CalcCls.__name__.replace("Calculator", "").lower()
            for freq in ("daily", "weekly"):
                calc = CalcCls(con, freq)
                agg_result = agg_by_key.get((indicator_name, freq), CalcResult())

                _write_skip_log_batch(con, calc_date, indicator_name, freq,
                                      agg_result.skipped,
                                      verbose=CALC_SKIP_LOG_VERBOSE)

                n = _count_calc_rows(con, calc.dws_table, calc_date, ts_codes)
                chunk_total += n

                skip_parts = []
                for reason in SkipReason:
                    items = agg_result.skipped.get(reason, [])
                    if items:
                        skip_parts.append(f"{reason.value}={len(items)}")
                skip_str = ", ".join(skip_parts) if skip_parts else "none skipped"
                if agg_result.calculated or skip_parts:
                    logger.info(
                        "calc %-30s DONE — %d rows (%d calculated), %s",
                        f"{CalcCls.__name__} {freq}", n,
                        agg_result.calculated, skip_str,
                    )
        logger.info(
            "progress calc.chunk: done | work_items=%d | rows=%d",
            len(work_items), chunk_total,
        )
        return chunk_total
    finally:
        con.close()


def _calc_stock_chunk(chunk: list[str], calc_date: str,
                      incremental: bool = True,
                      batch_ctx: Optional[dict] = None) -> int:
    """Worker: run all calculators for one stock chunk in a dedicated connection."""
    import duckdb
    from backend.config import DUCKDB_PATH

    con = duckdb.connect(DUCKDB_PATH)
    try:
        if incremental:
            daily_recalc = resolve_recalc_start(con, calc_date, "daily")
            weekly_recalc = resolve_recalc_start(con, calc_date, "weekly")
        else:
            daily_recalc = weekly_recalc = None
        from collections import defaultdict

        from backend.config import CALC_FAST_SKIP, CALC_APPEND
        from backend.etl.calc_indicators import CALC_ROUTE_SPECS, quote_tail_columns
        from backend.etl.column_indicator_deps import (
            active_route_specs,
            needs_dde_tails,
            needs_quote_tails,
        )
        from backend.etl.calc_state import load_calc_state_batch
        from backend.etl.calc_fast_skip import (
            batch_load_quote_tails,
            batch_load_dde_tails,
            build_skip_state_records,
            preflight_stock_modes_with_fps,
            partition_preflight_modes,
        )

        completed_keys = set()
        indicator_filter = None
        if batch_ctx:
            completed_keys = batch_ctx.get("completed_keys", set())
            indicator_filter = batch_ctx.get("indicator_filter")

        route_specs = active_route_specs(indicator_filter)
        fallthrough_run_keys = {(ind, freq) for ind, freq, *_ in route_specs}

        agg_by_key = defaultdict(CalcResult)
        fast_on = CALC_FAST_SKIP and CALC_APPEND and incremental
        full_skip_count = 0
        partial_run_count = 0
        fallthrough_count = 0
        state_map = {}
        daily_tails = weekly_tails = dde_daily = dde_weekly = {}
        if batch_ctx is not None:
            state_map = {
                k: v for k, v in batch_ctx.get("state_map", {}).items()
                if k[0] in chunk
            }
            daily_tails = {c: batch_ctx["daily_tails"][c] for c in chunk
                           if c in batch_ctx.get("daily_tails", {})}
            weekly_tails = {c: batch_ctx["weekly_tails"][c] for c in chunk
                            if c in batch_ctx.get("weekly_tails", {})}
            dde_daily = {c: batch_ctx["dde_daily"][c] for c in chunk
                         if c in batch_ctx.get("dde_daily", {})}
            dde_weekly = {c: batch_ctx["dde_weekly"][c] for c in chunk
                          if c in batch_ctx.get("dde_weekly", {})}
            fast_on = CALC_FAST_SKIP and CALC_APPEND and incremental
        elif fast_on:
            from backend.etl.progress import log_timed_step

            cn = len(chunk)
            state_map = log_timed_step(
                "calc.chunk_tails", "state",
                lambda: load_calc_state_batch(con, chunk), stocks=cn,
            )
            if needs_quote_tails(indicator_filter):
                daily_tails = log_timed_step(
                    "calc.chunk_tails", "quote_daily",
                    lambda: batch_load_quote_tails(
                        con, chunk, "daily", quote_tail_columns("daily"),
                    ),
                    stocks=cn,
                )
                weekly_tails = log_timed_step(
                    "calc.chunk_tails", "quote_weekly",
                    lambda: batch_load_quote_tails(
                        con, chunk, "weekly", quote_tail_columns("weekly"),
                    ),
                    stocks=cn,
                )
            if needs_dde_tails(indicator_filter):
                dde_daily = log_timed_step(
                    "calc.chunk_tails", "dde_daily",
                    lambda: batch_load_dde_tails(con, chunk, "daily"),
                    stocks=cn,
                )
                dde_weekly = log_timed_step(
                    "calc.chunk_tails", "dde_weekly",
                    lambda: batch_load_dde_tails(con, chunk, "weekly"),
                    stocks=cn,
                )

        from backend.etl.calc_state import upsert_calc_state_batch

        logger.info("progress calc.chunk: started | stocks=%d", len(chunk))

        dwd_fp_cache = None
        if fast_on:
            from backend.etl.calc_dwd_fp_gate import build_dwd_fp_cache

            dwd_fp_cache = build_dwd_fp_cache(con, chunk, calc_date)

        stock_modes = {}
        fp_cache_by_stock = {}
        for ts_code in chunk:
            modes = None
            if fast_on:
                modes, fps = preflight_stock_modes_with_fps(
                    ts_code, state_map,
                    daily_tails.get(ts_code), weekly_tails.get(ts_code),
                    dde_daily.get(ts_code), dde_weekly.get(ts_code),
                    specs=route_specs,
                    con=con,
                    dwd_fp_cache=dwd_fp_cache,
                )
                if modes is not None:
                    stock_modes[ts_code] = modes
                    fp_cache_by_stock[ts_code] = fps
            if modes is not None:
                skip_keys, run_keys = partition_preflight_modes(modes)
                for indicator_name, freq in skip_keys:
                    agg_by_key[(indicator_name, freq)].add_skip(
                        SkipReason.FINGERPRINT_MATCH, ts_code,
                        "fast_skip: preflight",
                    )
                if completed_keys:
                    run_keys = {
                        k for k in run_keys
                        if (ts_code, k[0], k[1]) not in completed_keys
                    }
                if not run_keys:
                    full_skip_count += 1
                    _report_calc_progress()
                    continue
                run_modes = {k: modes[k] for k in run_keys}
                tail_frames = {
                    ("daily", "quote"): daily_tails.get(ts_code),
                    ("weekly", "quote"): weekly_tails.get(ts_code),
                    ("daily", "dde"): dde_daily.get(ts_code),
                    ("weekly", "dde"): dde_weekly.get(ts_code),
                }
                for indicator_name, freq, result in calc_stock_pipeline_selective(
                        con, ts_code, calc_date, daily_recalc, weekly_recalc,
                        run_keys=run_keys, run_modes=run_modes,
                        tail_frames=tail_frames):
                    key = (indicator_name, freq)
                    agg = agg_by_key[key]
                    agg.calculated += result.calculated
                    for reason, items in result.skipped.items():
                        for code, detail in items:
                            agg.add_skip(reason, code, detail)
                partial_run_count += 1
                _report_calc_progress()
                continue

            fallthrough_count += 1
            if indicator_filter is not None:
                for indicator_name, freq, result in calc_stock_pipeline_selective(
                        con, ts_code, calc_date, daily_recalc, weekly_recalc,
                        run_keys=fallthrough_run_keys):
                    key = (indicator_name, freq)
                    agg = agg_by_key[key]
                    agg.calculated += result.calculated
                    for reason, items in result.skipped.items():
                        for code, detail in items:
                            agg.add_skip(reason, code, detail)
            else:
                for indicator_name, freq, result in calc_stock_pipeline(
                        con, ts_code, calc_date, daily_recalc, weekly_recalc):
                    key = (indicator_name, freq)
                    agg = agg_by_key[key]
                    agg.calculated += result.calculated
                    for reason, items in result.skipped.items():
                        for code, detail in items:
                            agg.add_skip(reason, code, detail)
            _report_calc_progress()

        skip_state_records = build_skip_state_records(
            stock_modes, fp_cache_by_stock, state_map, calc_date,
            daily_tails, weekly_tails, dde_daily, dde_weekly,
        )
        if skip_state_records:
            from backend.etl.progress import log_timed_step as _log_timed_step
            _log_timed_step(
                "calc.chunk_state", "skip_refresh",
                lambda: upsert_calc_state_batch(con, skip_state_records),
                extra=f"records={len(skip_state_records)}",
            )

        if fast_on:
            logger.info(
                "calc partial_skip: full_skip=%d partial_run=%d fallthrough=%d / %d stocks",
                full_skip_count, partial_run_count, fallthrough_count, len(chunk),
            )

        from backend.config import CALC_SKIP_LOG_VERBOSE

        chunk_total = 0
        for CalcCls in CALCULATORS:
            indicator_name = CalcCls.__name__.replace("Calculator", "").lower()
            for freq in ("daily", "weekly"):
                calc = CalcCls(con, freq)
                agg_result = agg_by_key.get((indicator_name, freq), CalcResult())

                _write_skip_log_batch(con, calc_date, indicator_name, freq,
                                      agg_result.skipped,
                                      verbose=CALC_SKIP_LOG_VERBOSE)

                n = _count_calc_rows(con, calc.dws_table, calc_date, chunk)
                chunk_total += n

                skip_parts = []
                for reason in SkipReason:
                    items = agg_result.skipped.get(reason, [])
                    if items:
                        skip_parts.append(f"{reason.value}={len(items)}")
                skip_str = ", ".join(skip_parts) if skip_parts else "none skipped"
                logger.info(
                    "calc %-30s DONE — %d rows (%d calculated), %s",
                    f"{CalcCls.__name__} {freq}", n,
                    agg_result.calculated, skip_str,
                )
        logger.info(
            "progress calc.chunk: done | stocks=%d | rows=%d",
            len(chunk), chunk_total,
        )
        return chunk_total
    finally:
        con.close()


def _should_skip_calc_idempotent(
    con,
    calc_date: str,
    user_subset: bool,
    force: bool,
    skip_stale_fetch: bool,
) -> bool:
    """True when a full-market calc for calc_date already completed and data is fresh."""
    from backend.config import CALC_FORCE_HARD
    from backend.etl.calc_gate import (
        data_mutated_since_last_calc,
        get_last_calc_log,
        has_prior_calc_snapshot,
    )

    if user_subset:
        return False
    if force and CALC_FORCE_HARD:
        return False

    if force:
        if not get_last_calc_log(con, calc_date):
            return False
    elif not has_prior_calc_snapshot(con, calc_date):
        return False

    if data_mutated_since_last_calc(con, calc_date):
        return False

    # --force same-day reuse trusts prior calc; stale tail stocks are a subset
    # handled on the next fetch/calc cycle and must not block the whole market.
    if force:
        from backend.etl.calc_spec_gate import has_spec_stale_indicators
        if has_spec_stale_indicators(con, calc_date):
            return False
        return True

    if not skip_stale_fetch:
        latest_ods = con.execute("SELECT MAX(trade_date) FROM ods_daily").fetchone()[0]
        if latest_ods and calc_date >= latest_ods:
            from backend.fetch.ods_daily import get_all_active_codes
            stale = find_stale_ods_codes(con, get_all_active_codes(con), calc_date)
            if stale:
                return False

    from backend.etl.calc_spec_gate import has_spec_stale_indicators
    if has_spec_stale_indicators(con, calc_date):
        return False
    return True


def _refresh_state_after_dwd_rebuild(con, ts_codes: list, calc_date: str, dwd_result: dict) -> None:
    from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

    summary = maybe_refresh_state_after_dwd_rebuild(con, ts_codes, calc_date, dwd_result)
    if summary:
        logger.info(
            "refresh_state after DWD rebuild: stocks=%d written=%d chunk_stocks=%d",
            summary.get("stocks", len(ts_codes)),
            summary.get("records_written", 0),
            summary.get("chunk_stocks", -1),
        )


def _merge_preflight_after_dwd_rebuild(
    con,
    ts_codes: list,
    calc_date: str,
    dwd_result: dict,
    preflight_ctx,
):
    """Refresh patch stocks after DWD rebuild and merge into run→calc preflight ctx."""
    from backend.config import CALC_REUSE_REFRESH_CTX
    from backend.etl.calc_preflight_context import merge_context_patch
    from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

    if not CALC_REUSE_REFRESH_CTX:
        return preflight_ctx
    result = maybe_refresh_state_after_dwd_rebuild(
        con, ts_codes, calc_date, dwd_result, return_artifacts=True,
    )
    if not result:
        return preflight_ctx
    if isinstance(result, tuple):
        summary, tails_bundle = result
    else:
        summary = result
        tails_bundle = None
    if summary:
        logger.info(
            "refresh_state after DWD rebuild: stocks=%d written=%d chunk_stocks=%d",
            summary.get("stocks", len(ts_codes)),
            summary.get("records_written", 0),
            summary.get("chunk_stocks", -1),
        )
    if tails_bundle is None:
        return preflight_ctx
    logger.info(
        "refresh_state merge: patch_stocks=%d ctx_before=%s",
        len(ts_codes), "set" if preflight_ctx else "none",
    )
    return merge_context_patch(preflight_ctx, ts_codes, tails_bundle, calc_date)


def run_calc(con, ts_codes: list[str] = None, auto_fetch: bool = True,
             batch_size: int = 100, calc_date: str = None,
             skip_stale_fetch: bool = False, force: bool = False,
             preflight_ctx=None, indicator_filter: list[str] = None,
             calc_handoff=None):
    """执行 DWS 计算流程。

    1. 如果未指定 ts_codes，获取全市场活跃股票
    2. 退市股过滤
    3. 数据完整度检查（warmup >= WARMUP_TDAYS）
    4. 缺 warmup → auto_fetch 补拉（熔断器：连续5次失败中止）
    5. stale ODS（max < calc_date）→ date/stock-batched 补 latest day（可 skip）
    6. 逐 Calculator 计算 DWS

    DWD refresh in run_calc uses rebuild_dwd_for_stale only (G3 / auto-fetch /
    stale_fetch). rebuild_all_dwd is reserved for legacy run_etl build-dwd step.
    """
    import time
    from datetime import datetime
    from backend.fetch.ods_daily import get_all_active_codes
    from backend.fetch.client import TushareClient
    from backend.fetch.ods_daily import fetch_stocks_incremental
    from backend.etl.error_handler import log_etl_start, log_etl_end
    from backend.db.connection import run_checkpoint
    from backend.db.schema import ensure_calc_state_table

    # Append-only routing reads/writes dws_calc_state from per-stock worker
    # connections that don't run schema init. Ensure it exists on the main
    # connection first so DBs created before this table get it (idempotent).
    ensure_calc_state_table(con)

    user_subset = ts_codes is not None

    if calc_date is None:
        calc_date = datetime.now().strftime("%Y%m%d")

    from backend.config import CALC_STRICT_DATE
    from backend.etl.calc_gate import assert_calc_date_ready, resolve_effective_calc_date

    if CALC_STRICT_DATE:
        assert_calc_date_ready(con, calc_date, strict=True)
    else:
        calc_date = resolve_effective_calc_date(con, calc_date, cap_to_ods=True)

    if _should_skip_calc_idempotent(con, calc_date, user_subset, force, skip_stale_fetch):
        lid, t0 = log_etl_start(con, "calc_dws")
        skip_key = "force_same_day_skip" if force else "idempotent_skip"
        log_etl_end(
            con, lid, "calc_dws", t0, "success", row_count=0,
            data_completeness={"calc_date": calc_date, skip_key: True},
        )
        if force:
            logger.info(
                "calc force same-day skip: %s unchanged since last calc "
                "(set CALC_FORCE_HARD=1 to force recalc)",
                calc_date,
            )
        else:
            logger.info(
                "calc idempotent skip: %s already completed (use --force to recalculate)",
                calc_date,
            )
        return

    if ts_codes is None:
        ts_codes = get_all_active_codes(con)
    if not ts_codes:
        logger.warning("No stocks to calculate")
        return

    ts_codes, delisted = _filter_delisted(con, ts_codes, calc_date)
    if delisted:
        classified = {SkipReason.DELISTED: [(c, d) for c, d in delisted.items()]}
        _write_skip_log_batch(con, calc_date, "dwd", "both", classified)
        logger.info("Pre-calc: %d delisted stocks skipped (DWS already exists)",
                    len(delisted))
    if not ts_codes:
        logger.warning("No stocks to calculate (all delisted or empty input)")
        return

    # 1. 数据完整度检查
    completeness = check_data_completeness(con, ts_codes, calc_date=calc_date)
    weekly_fetch = completeness.get("weekly_fetch", {})
    logger.info(
        "DWD completeness: calc_ok=%d/%d, missing=%d, weekly_fetch=%d",
        len(completeness["ok"]), len(ts_codes),
        len(completeness["missing"]), len(weekly_fetch),
    )
    if completeness["missing"]:
        reason_counts = {}
        for c in completeness["missing"]:
            r = completeness["missing"][c].get("reason", "unknown")
            reason_counts[r] = reason_counts.get(r, 0) + 1
        logger.info("Missing breakdown by reason: %s", reason_counts)

    if auto_fetch and (completeness["missing"] or weekly_fetch):
        fetch_candidates = list(completeness["missing"].keys()) + list(weekly_fetch.keys())
        to_fetch = []
        for ts_code in fetch_candidates:
            needed_start, needed_end = _compute_fetch_range(con, ts_code, calc_date)
            if needed_start is None:
                continue
            to_fetch.append((ts_code, needed_start, needed_end))

        if to_fetch:
            logger.info(
                "Auto-fetching %d stocks (daily>=%d, weekly>=%d week-ends)...",
                len(to_fetch), WARMUP_TDAYS, WEEKLY_WARMUP_WEEKS,
            )
            client = TushareClient()

            from collections import defaultdict
            range_buckets = defaultdict(list)
            for ts_code, seg_start, seg_end in to_fetch:
                range_buckets[(seg_start, seg_end)].append(ts_code)

            consecutive_errors = 0
            n_fetched = 0
            all_fetch_codes = {c for c, _, _ in to_fetch}
            attempted_codes = set()

            n_buckets = len(range_buckets)
            for bucket_idx, ((seg_start, seg_end), bucket_codes) in enumerate(
                    range_buckets.items(), 1):
                try:
                    tdays = _count_trading_days(con, seg_start, seg_end)
                    logger.info(
                        "progress calc.auto_fetch: bucket %d/%d | %s~%s | stocks=%d",
                        bucket_idx, n_buckets, seg_start, seg_end, len(bucket_codes),
                    )
                    if _choose_fetch_strategy(len(bucket_codes), tdays):
                        logger.info(
                            "Auto-fetch bucket [%s~%s]: %d stocks, %d tdays "
                            "→ date-batched parallel mode",
                            seg_start, seg_end, len(bucket_codes), tdays,
                        )
                        rows = fetch_by_date_range_parallel(
                            seg_start, seg_end, workers=3,
                            ts_codes=bucket_codes, con=con,
                        )
                    else:
                        logger.info(
                            "Auto-fetch bucket [%s~%s]: %d stocks, %d tdays "
                            "→ stock-batched sequential mode",
                            seg_start, seg_end, len(bucket_codes), tdays,
                        )
                        rows = fetch_stocks_incremental(
                            client, con, bucket_codes,
                            start=seg_start, end=seg_end)
                    logger.info(
                        "progress calc.auto_fetch: bucket done | rows=%d", int(rows),
                    )
                    attempted_codes.update(bucket_codes)
                    if int(rows) > 0:
                        n_fetched += int(rows)
                        consecutive_errors = 0
                except Exception as e:
                    attempted_codes.update(bucket_codes)
                    consecutive_errors += 1
                    logger.warning("fetch failed for batch [%s~%s] (%d stocks): %s",
                                   seg_start, seg_end, len(bucket_codes), e)

                if consecutive_errors >= 5:
                    logger.error("Circuit breaker: %d consecutive fetch errors. "
                                 "tushare may be down. Aborting auto-fetch.",
                                 consecutive_errors)
                    break

            fetch_failed_codes = all_fetch_codes - attempted_codes
            if fetch_failed_codes:
                cf = {SkipReason.FETCH_FAILED: [
                    (c, "auto-fetch aborted by circuit breaker") for c in fetch_failed_codes
                ]}
                _write_skip_log_batch(con, calc_date, "dwd", "both", cf)
                logger.warning("Pre-calc skip: %d stocks — fetch_failed (circuit breaker)",
                               len(fetch_failed_codes))
                for c in fetch_failed_codes:
                    if c in completeness["missing"]:
                        del completeness["missing"][c]

            logger.info("Auto-fetch complete: %d ODS rows fetched (%d batches)",
                        n_fetched, len(range_buckets))
            if n_fetched > 0:
                from backend.etl.progress import log_timed_step
                fetched_codes = list({c for c, _, _ in to_fetch})
                dwd_result = log_timed_step(
                    "calc.auto_fetch", "rebuild_dwd",
                    lambda: rebuild_dwd_for_stale(con, fetched_codes, calc_date),
                    stocks=len(fetched_codes),
                )
                preflight_ctx = _merge_preflight_after_dwd_rebuild(
                    con, fetched_codes, calc_date, dwd_result or {}, preflight_ctx,
                )
                completeness = check_data_completeness(con, ts_codes, calc_date=calc_date)
        else:
            logger.info("No ODS gaps to fetch; skipping DWD rebuild")
    elif not auto_fetch and completeness["missing"]:
        logger.info("Auto-fetch disabled. Skipping %d missing stocks.",
                    len(completeness["missing"]))

    # 1b. Stale ODS tail — stocks with warmup OK but missing latest trade_date
    if auto_fetch and not skip_stale_fetch:
        stale_ods = find_stale_ods_codes(con, ts_codes, calc_date)
        if stale_ods:
            logger.info(
                "Stale ODS: %d/%d stocks missing data through %s",
                len(stale_ods), len(ts_codes), calc_date,
            )
            n_stale = _auto_fetch_stale_ods(con, stale_ods, calc_date)
            logger.info("Stale auto-fetch complete: %d ODS rows", n_stale)
        else:
            stale_dwd = find_stale_dwd_codes(con, ts_codes, calc_date)
            if stale_dwd:
                from backend.etl.progress import log_timed_step
                logger.info(
                    "Stale DWD: %d stocks have ODS on %s but DWD behind — rebuilding",
                    len(stale_dwd), calc_date,
                )
                dwd_result = log_timed_step(
                    "calc.stale_dwd", "rebuild",
                    lambda: rebuild_dwd_for_stale(con, stale_dwd, calc_date),
                    stocks=len(stale_dwd),
                )
                preflight_ctx = _merge_preflight_after_dwd_rebuild(
                    con, stale_dwd, calc_date, dwd_result or {}, preflight_ctx,
                )

    # 2. 分类仍缺失的股票 + 写 skip_log
    if completeness["missing"]:
        classified = _classify_still_missing(con, completeness["missing"])
        _write_skip_log_batch(con, calc_date, "dwd", "both", classified)

        for reason, items in classified.items():
            count = len(items)
            level = "info" if reason in (SkipReason.SOURCE_UNAVAILABLE,
                                          SkipReason.INSUFFICIENT_ROWS) else "warning"
            getattr(logger, level)(
                "Pre-calc skip: %d stocks — %s", count, reason.value)
            sample = [c for c, _ in items[:10]]
            logger.info("  Sample: %s", ", ".join(sample))

    # 3. 只计算数据充足的股票
    codes_to_calc = completeness["ok"]
    if not codes_to_calc:
        logger.warning("No stocks with sufficient data to calculate")
        return

    # 3a. Auto spec refresh — narrow FULL for state/DWS spec_version lag (before batch APPEND).
    auto_spec_summary = {}
    from backend.config import CALC_AUTO_SPEC_REFRESH
    if CALC_AUTO_SPEC_REFRESH:
        from backend.etl.calc_spec_refresh import run_auto_spec_refresh_if_needed

        auto_spec_summary = run_auto_spec_refresh_if_needed(
            con, calc_date, codes_to_calc,
            indicator_filter=indicator_filter,
        )
        if auto_spec_summary.get("refreshed", 0) > 0:
            logger.info(
                "auto spec refresh done: refreshed=%s full_by_indicator=%s",
                auto_spec_summary.get("refreshed", 0),
                auto_spec_summary.get("full_by_indicator", {}),
            )
            # DWS changed — discard hot preflight to avoid duplicate FULL routing.
            if preflight_ctx is not None:
                preflight_ctx = None

    # 3b. Cross-stock batch APPEND (full market only; FULL stocks defer to chunk workers).
    from backend.config import CALC_APPEND, CALC_BATCH_APPEND, CALC_INCREMENTAL
    from backend.etl.calc_batch_append import run_batch_append_phase

    batch_ctx = None
    chunk_codes = codes_to_calc
    full_items = []
    fallthrough_codes = []
    chunk_work_items = 0
    if CALC_APPEND and CALC_BATCH_APPEND and CALC_INCREMENTAL and not user_subset:
        batch_ctx = run_batch_append_phase(
            con, codes_to_calc, calc_date, force=force,
            preflight_ctx=preflight_ctx,
            indicator_filter=indicator_filter,
        )
        if batch_ctx:
            chunk_codes = batch_ctx["chunk_codes"]
            from backend.etl.calc_executor import build_work_queue

            wq = build_work_queue(
                batch_ctx.get("stock_modes", {}),
                batch_ctx.get("completed_keys", set()),
            )
            full_items = batch_ctx.get("full_items") or wq.full_items
            chunk_work_items = batch_ctx.get("chunk_work_items", len(full_items))
            fallthrough_codes = [
                c for c in chunk_codes if c not in wq.full_stocks
            ]
            n_batch_only = len(codes_to_calc) - len(chunk_codes)
            if n_batch_only:
                logger.info(
                    "batch_append: %d stocks fully handled (APPEND/SKIP), "
                    "%d stocks need chunk (FULL/fallthrough)",
                    n_batch_only, len(chunk_codes),
                )
            if chunk_work_items:
                logger.info(
                    "chunk phase: %d work items across %d stocks "
                    "(%d fallthrough stocks)",
                    chunk_work_items,
                    len({ts for ts, _ in full_items}),
                    len(fallthrough_codes),
                )
            from backend.config import CALC_SKIP_LOG_VERBOSE
            for (indicator_name, freq), agg in batch_ctx.get("agg_by_key", {}).items():
                if agg.skipped:
                    _write_skip_log_batch(
                        con, calc_date, indicator_name, freq, agg.skipped,
                        verbose=CALC_SKIP_LOG_VERBOSE,
                    )
    else:
        fallthrough_codes = chunk_codes

    # 4. 计算 DWS — ThreadPoolExecutor by indicator work item + stock fallthrough.
    #    DuckDB 单文件仅允许一个 read-write 进程；multiprocessing 会争文件锁
    #    （IOException: Could not set lock）。线程共享同一进程 DuckDB 实例，
    #    支持多写线程（MVCC），与 ods_daily fetch 的 3 线程模式一致。
    workers = resolve_calc_workers()
    work_chunks = []
    if full_items:
        wi_chunk_size = max(1, (len(full_items) + workers - 1) // workers)
        work_chunks = [
            full_items[i:i + wi_chunk_size]
            for i in range(0, len(full_items), wi_chunk_size)
        ]
    stock_chunks = []
    if fallthrough_codes:
        st_chunk_size = max(1, (len(fallthrough_codes) + workers - 1) // workers)
        stock_chunks = [
            fallthrough_codes[i:i + st_chunk_size]
            for i in range(0, len(fallthrough_codes), st_chunk_size)
        ]

    chunk_tasks = [("work", wc) for wc in work_chunks] + [
        ("stock", sc) for sc in stock_chunks
    ]

    logger.info(
        "calc %d stocks with %d threads (%d work_items, %d fallthrough/chunk)",
        len(codes_to_calc), workers,
        len(full_items), len(fallthrough_codes),
    )

    progress_total = len(full_items) + len(fallthrough_codes)
    _init_calc_progress(
        progress_total or len(chunk_codes),
        threads=workers,
        work_items=len(full_items),
        fallthrough_stocks=len(fallthrough_codes),
        batch_only=len(codes_to_calc) - len(chunk_codes),
    )
    lid, t0 = log_etl_start(con, "calc_dws")
    calc_start = time.monotonic()

    from backend.log_config import _run_id, set_run_id
    from concurrent.futures import ThreadPoolExecutor

    rid = _run_id.get()

    def _dispatch_chunk_task(task):
        kind, chunk = task
        if kind == "work":
            return _calc_full_work_chunk(
                chunk, calc_date, CALC_INCREMENTAL, batch_ctx,
            )
        return _calc_stock_chunk(
            chunk, calc_date, CALC_INCREMENTAL, batch_ctx,
        )

    if chunk_tasks:
        with ThreadPoolExecutor(
            max_workers=workers,
            initializer=set_run_id,
            initargs=(rid,),
        ) as executor:
            results = list(executor.map(_dispatch_chunk_task, chunk_tasks))
        grand_total = sum(results)
    else:
        results = []
        if batch_ctx:
            grand_total = sum(
                agg.calculated
                for agg in batch_ctx.get("agg_by_key", {}).values()
            )
        else:
            grand_total = 0

    batch_only = [c for c in codes_to_calc if c not in set(chunk_codes)]
    if batch_only and chunk_codes:
        for CalcCls in CALCULATORS:
            for freq in ("daily", "weekly"):
                calc = CalcCls(con, freq)
                grand_total += _count_calc_rows(
                    con, calc.dws_table, calc_date, batch_only,
                )

    if batch_ctx:
        from backend.etl.calc_indicators import CALC_ROUTE_SPECS
        for indicator_name, freq, CalcCls, _, _ in CALC_ROUTE_SPECS:
            agg = batch_ctx.get("agg_by_key", {}).get((indicator_name, freq), CalcResult())
            skip_parts = []
            for reason in SkipReason:
                items = agg.skipped.get(reason, [])
                if items:
                    skip_parts.append(f"{reason.value}={len(items)}")
            skip_str = ", ".join(skip_parts) if skip_parts else "none skipped"
            if agg.calculated or skip_parts:
                if chunk_codes:
                    calc = CalcCls(con, freq)
                    n = _count_calc_rows(con, calc.dws_table, calc_date, codes_to_calc)
                else:
                    n = agg.calculated
                logger.info(
                    "calc %-30s BATCH — %d rows (%d calculated), %s",
                    f"{CalcCls.__name__} {freq}", n,
                    agg.calculated, skip_str,
                )

    total_elapsed = time.monotonic() - calc_start
    if _calc_progress is not None:
        _calc_progress.log_done(rows=grand_total, elapsed=f"{total_elapsed:.0f}s")
    logger.info("calc ALL DONE — %d total DWS rows across %d indicator×freq pairs, %.0fs",
                grand_total, len(CALCULATORS) * 2, total_elapsed)
    logger.info("Skip details: SELECT reason, COUNT(*) FROM ods_calc_skip_log "
                "WHERE calc_date='%s' GROUP BY reason", calc_date)
    from backend.etl.calc_gate import get_ods_max_trade_date

    from backend.etl.calc_spec_gate import (
        count_dws_spec_stale_on_trade_date,
        count_spec_stale_by_indicator,
    )

    chunk_stock_count = len({ts for ts, _ in full_items}) + len(fallthrough_codes)
    batch_obs = {}
    narrow_obs = {}
    if calc_handoff is not None:
        narrow_obs = {
            "run_indicator_filter": calc_handoff.indicator_filter,
            "calc_routes_narrowed": calc_handoff.calc_routes_narrowed,
            "active_routes": calc_handoff.active_routes,
        }
    elif indicator_filter is not None:
        from backend.etl.column_indicator_deps import (
            active_route_keys,
            calc_routes_narrowed,
        )
        narrow_obs = {
            "run_indicator_filter": indicator_filter,
            "calc_routes_narrowed": calc_routes_narrowed(indicator_filter),
            "active_routes": active_route_keys(indicator_filter),
        }
    if batch_ctx:
        batch_obs = {
            "preflight_source": batch_ctx.get("preflight_source", "cold"),
            "tails_load_skipped": batch_ctx.get("tails_load_skipped", False),
            "preflight_elapsed_sec": batch_ctx.get("preflight_elapsed_sec", 0.0),
            "cold_merge_stocks": batch_ctx.get("cold_merge_stocks", 0),
            "cold_merge_elapsed_sec": batch_ctx.get("cold_merge_elapsed_sec", 0.0),
            "state_upsert_mode": batch_ctx.get("state_upsert_mode", "per_stock"),
        }
    log_etl_end(
        con, lid, "calc_dws", t0, "success", row_count=grand_total,
        data_completeness={
            "calc_date": calc_date,
            "stocks": len(codes_to_calc),
            "ods_max": get_ods_max_trade_date(con),
            "batch_only": len(codes_to_calc) - len(chunk_codes),
            "chunk_stocks": chunk_stock_count,
            "chunk_work_items": chunk_work_items,
            "batch_full_items": (
                batch_ctx.get("batch_full_items", 0) if batch_ctx else 0
            ),
            "full_by_indicator": (
                batch_ctx.get("full_by_indicator", {}) if batch_ctx else {}
            ),
            "spec_stale_counts": count_spec_stale_by_indicator(con),
            "dws_spec_stale_counts": count_dws_spec_stale_on_trade_date(
                con, calc_date, codes_to_calc,
            ),
            "auto_spec_refresh": auto_spec_summary or None,
            **narrow_obs,
            **batch_obs,
        },
    )
    run_checkpoint(con)
