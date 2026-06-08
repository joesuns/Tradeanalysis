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
from backend.etl.build_dwd import rebuild_all_dwd
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
                        row_count=rows,
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
    """Stocks with ODS on analysis_date but DWD max trade_date behind."""
    if not ts_codes:
        return []

    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(f"""
        SELECT o.ts_code
        FROM ods_daily o
        LEFT JOIN (
            SELECT ts_code, MAX(trade_date) AS dwd_max
            FROM dwd_daily_quote
            WHERE ts_code IN ({placeholders})
            GROUP BY ts_code
        ) d ON o.ts_code = d.ts_code
        WHERE o.trade_date = ?
          AND o.ts_code IN ({placeholders})
          AND (d.dwd_max IS NULL OR d.dwd_max < ?)
    """, list(ts_codes) + [analysis_date] + list(ts_codes) + [analysis_date]).fetchall()
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

    if _choose_fetch_strategy(len(stale_codes), tdays):
        logger.info(
            "Stale auto-fetch [%s~%s]: %d stocks, %d tdays → date-batched",
            seg_start, analysis_date, len(stale_codes), tdays,
        )
        n_fetched = fetch_by_date_range_parallel(
            seg_start, analysis_date, workers=3,
            ts_codes=stale_codes, con=con,
        )
    else:
        logger.info(
            "Stale auto-fetch [%s~%s]: %d stocks, %d tdays → stock-batched",
            seg_start, analysis_date, len(stale_codes), tdays,
        )
        n_fetched = fetch_stocks_incremental(
            client, con, stale_codes, start=seg_start, end=analysis_date)

    logger.info("Rebuilding DWD for %d stale stocks", len(stale_codes))
    rebuild_all_dwd(con, stale_codes)
    return n_fetched


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
                           classified: dict):
    """Write classified skip reasons to ods_calc_skip_log."""
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
    State (dws_calc_state) is refreshed only when a write actually happened.
    """
    from backend.etl.calc_state import load_calc_state, upsert_calc_state
    from backend.etl.calc_router import classify_calc_mode, state_signature

    def _full() -> CalcResult:
        if name == "dde":
            return calc.calculate([ts_code], calc_date, recalc_start=recalc_start)
        return calc.calculate([ts_code], calc_date, recalc_start=recalc_start,
                              quote_groups=quote_groups)

    if not append_on or df is None or len(df) == 0:
        return _full()

    state = load_calc_state(con, freq, name, [ts_code]).get(ts_code)
    mode, new_bars = classify_calc_mode(df, state, calc.SIGNATURE_COLS)

    if mode == "SKIP":
        r = CalcResult()
        r.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                   "append: no new bars, signature match")
        return r

    if mode == "APPEND":
        result = calc.append_calculate(ts_code, df, new_bars, calc_date, state)
    else:  # FULL
        result = _full()

    # Only establish/refresh state when a write actually happened. A FULL that
    # the calculator skipped (e.g. insufficient rows) leaves no baseline, so
    # next run re-routes to FULL rather than appending onto nothing.
    if result.calculated > 0 and df is not None and len(df) > 0:
        max_td = str(df["trade_date"].max())
        fp = state_signature(df, max_td, calc.SIGNATURE_COLS)
        upsert_calc_state(con, ts_code, freq, name, last_trade_date=max_td,
                          history_fp=fp, calc_date=calc_date)
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

    outputs = []
    for freq, recalc_start in (("daily", daily_recalc), ("weekly", weekly_recalc)):
        load_start = None
        if recalc_start:
            load_start = resolve_load_start(con, recalc_start, freq)
        src = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        quote_groups = load_quote_groups(
            con, src, freq, QUOTE_COLUMNS, [ts_code], start_date=load_start,
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


_calc_progress_lock = threading.Lock()
_calc_progress = {"done": 0, "total": 0, "t0": 0.0, "step": 1}


def _init_calc_progress(total: int) -> None:
    """Reset the shared calc progress counter before a multi-threaded run.

    Count-throttled at ~5% (step = total // 20) so the per-stock heartbeat
    emits ~20 progress lines instead of going silent for the whole calc phase.
    """
    with _calc_progress_lock:
        _calc_progress["done"] = 0
        _calc_progress["total"] = total
        _calc_progress["t0"] = time.monotonic()
        _calc_progress["step"] = max(1, total // 20)


def _report_calc_progress() -> None:
    """Thread-safe per-stock tick. Logs every ~5% of stocks with rate + ETA.

    Called once per stock by every worker thread; threads share one counter so
    the reported progress is the true global total, not per-chunk.
    """
    with _calc_progress_lock:
        _calc_progress["done"] += 1
        done = _calc_progress["done"]
        total = _calc_progress["total"]
        step = _calc_progress["step"]
        if total <= 0 or (done % step != 0 and done != total):
            return
        elapsed = time.monotonic() - _calc_progress["t0"]
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (total - done) / rate if rate > 0 else 0.0
        pct = done * 100 // total
    logger.info(
        "calc progress: %d/%d (%d%%) | %.0fs | %.1f stk/s | ETA ~%.0fs",
        done, total, pct, elapsed, rate, eta,
    )


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


def _calc_stock_chunk(chunk: list[str], calc_date: str,
                      incremental: bool = True) -> int:
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
        from backend.etl.calc_state import load_calc_state_batch
        from backend.etl.calc_fast_skip import (
            batch_load_quote_tails,
            batch_load_dde_tails,
            preflight_stock_modes,
            stock_can_fast_skip,
        )

        agg_by_key = defaultdict(CalcResult)
        fast_on = CALC_FAST_SKIP and CALC_APPEND and incremental
        fast_skip_count = 0
        state_map = {}
        daily_tails = weekly_tails = dde_daily = dde_weekly = {}
        if fast_on:
            state_map = load_calc_state_batch(con, chunk)
            tail_cols = quote_tail_columns()
            daily_tails = batch_load_quote_tails(con, chunk, "daily", tail_cols)
            weekly_tails = batch_load_quote_tails(con, chunk, "weekly", tail_cols)
            dde_daily = batch_load_dde_tails(con, chunk, "daily")
            dde_weekly = batch_load_dde_tails(con, chunk, "weekly")

        for ts_code in chunk:
            modes = None
            if fast_on:
                modes = preflight_stock_modes(
                    ts_code, state_map,
                    daily_tails.get(ts_code), weekly_tails.get(ts_code),
                    dde_daily.get(ts_code), dde_weekly.get(ts_code),
                )
            if modes is not None and stock_can_fast_skip(modes):
                for indicator_name, freq, _, _, _ in CALC_ROUTE_SPECS:
                    agg_by_key[(indicator_name, freq)].add_skip(
                        SkipReason.FINGERPRINT_MATCH, ts_code,
                        "fast_skip: preflight",
                    )
                fast_skip_count += 1
                _report_calc_progress()
                continue

            for indicator_name, freq, result in calc_stock_pipeline(
                    con, ts_code, calc_date, daily_recalc, weekly_recalc):
                key = (indicator_name, freq)
                agg = agg_by_key[key]
                agg.calculated += result.calculated
                for reason, items in result.skipped.items():
                    for code, detail in items:
                        agg.add_skip(reason, code, detail)
            _report_calc_progress()

        if fast_on:
            logger.info("calc fast_skip: %d/%d stocks in chunk",
                        fast_skip_count, len(chunk))

        chunk_total = 0
        for CalcCls in CALCULATORS:
            indicator_name = CalcCls.__name__.replace("Calculator", "").lower()
            for freq in ("daily", "weekly"):
                calc = CalcCls(con, freq)
                agg_result = agg_by_key.get((indicator_name, freq), CalcResult())

                _write_skip_log_batch(con, calc_date, indicator_name, freq,
                                      agg_result.skipped)

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
        return chunk_total
    finally:
        con.close()


def run_calc(con, ts_codes: list[str] = None, auto_fetch: bool = True,
             batch_size: int = 100, calc_date: str = None,
             skip_stale_fetch: bool = False):
    """执行 DWS 计算流程。

    1. 如果未指定 ts_codes，获取全市场活跃股票
    2. 退市股过滤
    3. 数据完整度检查（warmup >= WARMUP_TDAYS）
    4. 缺 warmup → auto_fetch 补拉（熔断器：连续5次失败中止）
    5. stale ODS（max < calc_date）→ date/stock-batched 补 latest day（可 skip）
    6. 逐 Calculator 计算 DWS
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

    if ts_codes is None:
        ts_codes = get_all_active_codes(con)
    if not ts_codes:
        logger.warning("No stocks to calculate")
        return

    if calc_date is None:
        calc_date = datetime.now().strftime("%Y%m%d")
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

            for (seg_start, seg_end), bucket_codes in range_buckets.items():
                try:
                    tdays = _count_trading_days(con, seg_start, seg_end)
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
                    attempted_codes.update(bucket_codes)
                    if rows > 0:
                        n_fetched += rows
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
                fetched_codes = list({c for c, _, _ in to_fetch})
                rebuild_all_dwd(con, fetched_codes)
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
                logger.info(
                    "Stale DWD: %d stocks have ODS on %s but DWD behind — rebuilding",
                    len(stale_dwd), calc_date,
                )
                rebuild_all_dwd(con, stale_dwd)

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

    # 4. 计算 DWS — ThreadPoolExecutor by stock chunk.
    #    DuckDB 单文件仅允许一个 read-write 进程；multiprocessing 会争文件锁
    #    （IOException: Could not set lock）。线程共享同一进程 DuckDB 实例，
    #    支持多写线程（MVCC），与 ods_daily fetch 的 3 线程模式一致。
    workers = resolve_calc_workers()
    chunk_size = max(1, (len(codes_to_calc) + workers - 1) // workers)
    chunks = [codes_to_calc[i:i + chunk_size]
              for i in range(0, len(codes_to_calc), chunk_size)]

    logger.info("calc %d stocks with %d threads (%d stocks/chunk)",
                len(codes_to_calc), workers, chunk_size)

    _init_calc_progress(len(codes_to_calc))
    lid, t0 = log_etl_start(con, "calc_dws")
    calc_start = time.monotonic()

    from backend.config import CALC_INCREMENTAL
    from backend.log_config import _run_id, set_run_id
    from concurrent.futures import ThreadPoolExecutor

    rid = _run_id.get()
    with ThreadPoolExecutor(
        max_workers=workers,
        initializer=set_run_id,
        initargs=(rid,),
    ) as executor:
        results = list(executor.map(
            _calc_stock_chunk,
            chunks,
            [calc_date] * len(chunks),
            [CALC_INCREMENTAL] * len(chunks),
        ))
    grand_total = sum(results)

    total_elapsed = time.monotonic() - calc_start
    logger.info("calc ALL DONE — %d total DWS rows across %d indicator×freq pairs, %.0fs",
                grand_total, len(CALCULATORS) * 2, total_elapsed)
    logger.info("Skip details: SELECT reason, COUNT(*) FROM ods_calc_skip_log "
                "WHERE calc_date='%s' GROUP BY reason", calc_date)
    log_etl_end(con, lid, "calc_dws", t0, "success", row_count=grand_total)
    run_checkpoint(con)
