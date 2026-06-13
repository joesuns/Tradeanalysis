"""Fetch daily OHLCV + daily_basic + moneyflow by TRADE_DATE.

Key insight: tushare daily/daily_basic/moneyflow APIs all support trade_date param,
returning ALL stocks for that date in ONE call. This is ~100x faster than per-stock.

Multi-threaded: each thread handles a chunk of trading days with its own
TushareClient + DuckDB connection. WAL mode allows concurrent writers.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from backend.config import DUCKDB_PATH
import duckdb

from backend.etl.progress import day_progress, stock_progress

logger = logging.getLogger(__name__)

_ODS_DAILY_BASIC_COLS = (
    "ts_code", "trade_date", "total_mv", "circ_mv", "pe_ttm",
    "turnover_rate", "volume_ratio", "fetched_at",
)
_ODS_MONEYFLOW_COLS = (
    "ts_code", "trade_date", "buy_sm_vol", "buy_sm_amount",
    "sell_sm_vol", "sell_sm_amount", "buy_md_vol", "buy_md_amount",
    "sell_md_vol", "sell_md_amount", "buy_lg_vol", "buy_lg_amount",
    "sell_lg_vol", "sell_lg_amount", "buy_elg_vol", "buy_elg_amount",
    "sell_elg_vol", "sell_elg_amount", "net_mf_vol", "net_mf_amount",
    "net_amount_dc", "fetched_at",
)


def _daily_basic_record(r) -> dict:
    return {
        "ts_code": r["ts_code"],
        "trade_date": r["trade_date"],
        "total_mv": r.get("total_mv"),
        "circ_mv": r.get("circ_mv"),
        "pe_ttm": r.get("pe_ttm"),
        "turnover_rate": r.get("turnover_rate"),
        "volume_ratio": r.get("volume_ratio"),
    }


def _moneyflow_record(r) -> dict:
    return {
        "ts_code": r["ts_code"],
        "trade_date": r["trade_date"],
        "buy_sm_vol": r.get("buy_sm_vol"),
        "buy_sm_amount": r.get("buy_sm_amount"),
        "sell_sm_vol": r.get("sell_sm_vol"),
        "sell_sm_amount": r.get("sell_sm_amount"),
        "buy_md_vol": r.get("buy_md_vol"),
        "buy_md_amount": r.get("buy_md_amount"),
        "sell_md_vol": r.get("sell_md_vol"),
        "sell_md_amount": r.get("sell_md_amount"),
        "buy_lg_vol": r.get("buy_lg_vol"),
        "buy_lg_amount": r.get("buy_lg_amount"),
        "sell_lg_vol": r.get("sell_lg_vol"),
        "sell_lg_amount": r.get("sell_lg_amount"),
        "buy_elg_vol": r.get("buy_elg_vol"),
        "buy_elg_amount": r.get("buy_elg_amount"),
        "sell_elg_vol": r.get("sell_elg_vol"),
        "sell_elg_amount": r.get("sell_elg_amount"),
        "net_mf_vol": r.get("net_mf_vol"),
        "net_mf_amount": r.get("net_mf_amount"),
        "net_amount_dc": None,
    }


def _attach_moneyflow_dc(client, trade_date: str, mf_data: list) -> list:
    """Merge moneyflow_dc net_amount (123 B4 alignment) into moneyflow rows."""
    if not mf_data:
        return mf_data
    try:
        recs = client.call("moneyflow_dc", trade_date=trade_date)
    except Exception as exc:
        logger.warning("moneyflow_dc trade_date=%s skipped: %s", trade_date, exc)
        for row in mf_data:
            row.setdefault("net_amount_dc", None)
        return mf_data
    dc_map = {r["ts_code"]: r.get("net_amount") for r in recs}
    for row in mf_data:
        row["net_amount_dc"] = dc_map.get(row["ts_code"])
    return mf_data


def _attach_moneyflow_dc_stock(
    client, ts_code: str, start: str, end: str, mf_data: list,
) -> list:
    if not mf_data:
        return mf_data
    try:
        recs = client.call(
            "moneyflow_dc", ts_code=ts_code, start_date=start, end_date=end,
        )
    except Exception as exc:
        logger.warning(
            "moneyflow_dc %s [%s~%s] skipped: %s", ts_code, start, end, exc,
        )
        for row in mf_data:
            row.setdefault("net_amount_dc", None)
        return mf_data
    dc_map = {r["trade_date"]: r.get("net_amount") for r in recs}
    for row in mf_data:
        row["net_amount_dc"] = dc_map.get(row["trade_date"])
    return mf_data


def _insert_ods_daily_basic(con, df, register_name: str = "_basic_batch"):
    cols = ", ".join(_ODS_DAILY_BASIC_COLS[:-1])
    con.register(register_name, df)
    con.execute(f"""
        INSERT OR REPLACE INTO ods_daily_basic ({cols}, fetched_at)
        SELECT {cols}, now() FROM {register_name}
    """)
    con.unregister(register_name)


def _insert_ods_moneyflow(con, df, register_name: str = "_mf_batch"):
    cols = ", ".join(_ODS_MONEYFLOW_COLS[:-1])
    con.register(register_name, df)
    con.execute(f"""
        INSERT OR REPLACE INTO ods_moneyflow ({cols}, fetched_at)
        SELECT {cols}, now() FROM {register_name}
    """)
    con.unregister(register_name)


def _apply_net_amount_dc_patch(con, df, register_name: str = "_dc_patch") -> int:
    """Bulk UPDATE ods_moneyflow.net_amount_dc where currently NULL."""
    if df is None or df.empty:
        return 0
    con.register(register_name, df)
    n = con.execute(
        f"""
        SELECT COUNT(*)
        FROM ods_moneyflow o
        JOIN {register_name} p
          ON o.ts_code = p.ts_code AND o.trade_date = p.trade_date
        WHERE o.net_amount_dc IS NULL AND p.net_amount_dc IS NOT NULL
        """
    ).fetchone()[0]
    con.execute(
        f"""
        UPDATE ods_moneyflow AS o
        SET net_amount_dc = p.net_amount_dc, fetched_at = now()
        FROM {register_name} AS p
        WHERE o.ts_code = p.ts_code
          AND o.trade_date = p.trade_date
          AND o.net_amount_dc IS NULL
          AND p.net_amount_dc IS NOT NULL
        """
    )
    con.unregister(register_name)
    return int(n)


def _apply_circ_mv_patch(con, df, register_name: str = "_circ_patch") -> int:
    """Bulk upsert circ_mv (NULL-only update + missing row INSERT)."""
    if df is None or df.empty:
        return 0
    con.register(register_name, df)
    insert_n = con.execute(
        f"""
        SELECT COUNT(*)
        FROM {register_name} p
        LEFT JOIN ods_daily_basic b
          ON b.ts_code = p.ts_code AND b.trade_date = p.trade_date
        WHERE b.ts_code IS NULL AND p.circ_mv IS NOT NULL
        """
    ).fetchone()[0]
    con.execute(
        f"""
        INSERT INTO ods_daily_basic (ts_code, trade_date, circ_mv, fetched_at)
        SELECT p.ts_code, p.trade_date, p.circ_mv, now()
        FROM {register_name} p
        LEFT JOIN ods_daily_basic b
          ON b.ts_code = p.ts_code AND b.trade_date = p.trade_date
        WHERE b.ts_code IS NULL AND p.circ_mv IS NOT NULL
        """
    )
    update_n = con.execute(
        f"""
        SELECT COUNT(*)
        FROM ods_daily_basic b
        JOIN {register_name} p
          ON b.ts_code = p.ts_code AND b.trade_date = p.trade_date
        WHERE b.circ_mv IS NULL AND p.circ_mv IS NOT NULL
        """
    ).fetchone()[0]
    con.execute(
        f"""
        UPDATE ods_daily_basic AS b
        SET circ_mv = p.circ_mv, fetched_at = now()
        FROM {register_name} AS p
        WHERE b.ts_code = p.ts_code
          AND b.trade_date = p.trade_date
          AND b.circ_mv IS NULL
          AND p.circ_mv IS NOT NULL
        """
    )
    con.unregister(register_name)
    return int(insert_n) + int(update_n)


def _backfill_net_amount_dc_by_date(
    con, client, trade_date: str,
    ts_codes=None,
    register_suffix: str = "",
) -> int:
    """One moneyflow_dc(trade_date) call → bulk patch NULL net_amount_dc rows."""
    import pandas as pd

    try:
        recs = client.call("moneyflow_dc", trade_date=trade_date)
    except Exception as exc:
        logger.warning("moneyflow_dc date=%s skipped: %s", trade_date, exc)
        return 0
    rows = []
    code_set = set(ts_codes) if ts_codes else None
    for r in recs:
        code = r.get("ts_code")
        if code_set is not None and code not in code_set:
            continue
        amt = r.get("net_amount")
        if amt is None:
            continue
        rows.append({
            "ts_code": code,
            "trade_date": trade_date,
            "net_amount_dc": amt,
        })
    if not rows:
        return 0
    reg = f"_dc_patch_{trade_date}{register_suffix}"
    return _apply_net_amount_dc_patch(con, pd.DataFrame(rows), register_name=reg)


def _backfill_circ_mv_by_date(
    con, client, trade_date: str,
    ts_codes=None,
    register_suffix: str = "",
) -> int:
    """One daily_basic(trade_date) call → bulk patch NULL circ_mv rows."""
    import pandas as pd

    try:
        recs = client.call("daily_basic", trade_date=trade_date)
    except Exception as exc:
        logger.warning("daily_basic circ date=%s skipped: %s", trade_date, exc)
        return 0
    rows = []
    code_set = set(ts_codes) if ts_codes else None
    for r in recs:
        code = r.get("ts_code")
        if code_set is not None and code not in code_set:
            continue
        circ = r.get("circ_mv")
        if circ is None:
            continue
        rows.append({
            "ts_code": code,
            "trade_date": trade_date,
            "circ_mv": circ,
        })
    if not rows:
        return 0
    reg = f"_circ_patch_{trade_date}{register_suffix}"
    return _apply_circ_mv_patch(con, pd.DataFrame(rows), register_name=reg)


def fetch_by_date_range(client, con, start: str, end: str,
                        ts_codes: list[str] = None) -> int:
    """Fetch daily + daily_basic + moneyflow for all stocks, batched by trade_date.

    Returns total rows written across all three ODS tables.
    Incremental: per-target-stock when ts_codes set (or auto-resolved from dim).
    """
    import time
    if ts_codes is None and con is not None:
        ts_codes = get_all_active_codes(con)
    days = _get_trading_days(client, start, end, con=con, ts_codes=ts_codes)
    if not days:
        logger.info("All dates already in DB — nothing to fetch (%s~%s)", start, end)
        return 0
    prog = day_progress("fetch.ods", len(days))
    prog.log_start(range=f"{start}~{end}")

    t0 = time.time()
    total = 0
    failed_dates = []
    for trade_date in days:
        try:
            import pandas as pd
            # 0. Fetch adj_factor FIRST — build lookup for daily INSERT
            adj_recs = client.call("adj_factor", trade_date=trade_date)
            adj_map = {a["ts_code"]: a.get("adj_factor") for a in adj_recs}
            total += len(adj_recs)

            # 1. Daily OHLCV — bulk INSERT via register + SELECT
            recs = client.call("daily", trade_date=trade_date)
            recs, _ = _validate_ods_batch(recs, "daily", trade_date)
            daily_data = [{"ts_code": r["ts_code"],
                "trade_date": r["trade_date"], "open": r["open"],
                "high": r["high"], "low": r["low"], "close": r["close"],
                "vol": r["vol"], "amount": r["amount"],
                "pct_chg": r["pct_chg"],
                "adj_factor": adj_map.get(r["ts_code"])} for r in recs]
            if daily_data:
                df = pd.DataFrame(daily_data)
                con.register("_daily_batch", df)
                con.execute("""INSERT OR REPLACE INTO ods_daily
                    (ts_code, trade_date, open, high, low, close,
                     vol, amount, pct_chg, adj_factor, fetched_at)
                    SELECT ts_code, trade_date, open, high, low, close,
                           vol, amount, pct_chg, adj_factor, now()
                    FROM _daily_batch""")
                con.unregister("_daily_batch")
                total += len(daily_data)

            # 2. Daily basic — bulk INSERT via register + SELECT
            recs = client.call("daily_basic", trade_date=trade_date)
            basic_data = [_daily_basic_record(r) for r in recs]
            if basic_data:
                df = pd.DataFrame(basic_data)
                _insert_ods_daily_basic(con, df)
                total += len(basic_data)

            # 3. Moneyflow — bulk INSERT via register + SELECT
            recs = client.call("moneyflow", trade_date=trade_date)
            mf_data = [_moneyflow_record(r) for r in recs]
            mf_data = _attach_moneyflow_dc(client, trade_date, mf_data)
            if mf_data:
                df = pd.DataFrame(mf_data)
                _insert_ods_moneyflow(con, df)
                total += len(mf_data)

        except Exception:
            failed_dates.append(trade_date)
            logger.exception("Failed trade_date=%s", trade_date)
        prog.tick()

    if failed_dates:
        display = ", ".join(failed_dates[:5])
        if len(failed_dates) > 5:
            display += f" ... (+{len(failed_dates) - 5} more)"
        logger.warning("fetch_by_date_range: %d/%d dates failed: %s",
                       len(failed_dates), len(days) if days else 0, display)
    elapsed = time.time() - t0
    prog.log_done(rows=total, days=len(days))
    logger.info("ODS fetch complete: %d rows in %.0fs (%.1f days/min)",
                total, elapsed, len(days) / elapsed * 60 if elapsed > 0 else 0)
    return total


def _local_trading_days(con, start: str, end: str):
    """Return trading days from local dim_date, or None if it can't be used.

    Returns None (→ caller falls back to trade_cal API) when dim_date is
    missing, empty, or does not fully cover [start, end]. Coverage requires
    dim_date to span from <= start to >= end so we never silently return a
    truncated calendar.
    """
    try:
        bounds = con.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM dim_date"
        ).fetchone()
    except duckdb.CatalogException:
        return None
    if not bounds or bounds[0] is None:
        return None
    min_d, max_d = bounds
    if min_d > start or max_d < end:
        return None
    rows = con.execute(
        "SELECT trade_date FROM dim_date "
        "WHERE is_trade_day = 1 AND trade_date >= ? AND trade_date <= ? "
        "ORDER BY trade_date",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def _get_trading_days(client, start: str, end: str,
                      con=None, ts_codes: list[str] = None) -> list[str]:
    """Get list of trading days in date range from tushare trade_cal.

    When con + ts_codes: per-target-stock incremental — only skip dates
    where ALL target stocks already have ODS data.
    When con only: original date-global incremental (any stock → date skipped).

    Calendar source: prefer the local dim_date table when it fully covers the
    requested [start, end] range (avoids a redundant trade_cal round-trip in
    incremental runs); otherwise fall back to the trade_cal API.
    """
    days = _local_trading_days(con, start, end) if con is not None else None
    if days is None:
        recs = client.call("trade_cal", exchange="SSE", start_date=start,
                           end_date=end, is_open=1)
        days = sorted([r["cal_date"] for r in recs])

    if con and ts_codes:
        # Per-target-stock: date is "covered" only when ALL target stocks have it
        placeholders = ",".join(["?" for _ in ts_codes])
        rows = con.execute(f"""
            SELECT trade_date, COUNT(DISTINCT ts_code) AS n
            FROM ods_daily
            WHERE ts_code IN ({placeholders})
            AND trade_date >= ? AND trade_date <= ?
            GROUP BY trade_date
        """, (*ts_codes, start, end)).fetchall()
        n_targets = len(ts_codes)
        existing = {r[0] for r in rows if r[1] >= n_targets}
        if existing:
            new_days = [d for d in days if d not in existing]
            logger.info("Incremental (per-stock): %d/%d dates fully covered, "
                        "%d new to fetch (%s~%s)",
                        len(existing), len(days), len(new_days), start, end)
            return new_days
        return days

    if con:
        existing = set(r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM ods_daily "
            "WHERE trade_date >= ? AND trade_date <= ?",
            (start, end)
        ).fetchall())
        if existing:
            new_days = [d for d in days if d not in existing]
            logger.info("Incremental: %d/%d dates already in DB, %d new to fetch "
                        "(%s~%s)", len(existing), len(days), len(new_days), start, end)
            return new_days

    return days


def _validate_ods_batch(recs: list[dict], api_name: str,
                        trade_date: str = "") -> tuple[list, int]:
    """Filter an ODS daily batch before INSERT. Returns (valid_recs, invalid_count).

    Checks (OHLCV daily records only):
      1. Required fields present and non-None (open/high/low/close/vol/amount)
      2. OHLC logic: high >= low

    Invalid rows are dropped from the returned list and counted; one WARNING
    is logged per batch when any rows are rejected.
    """
    import pandas as pd
    required = ["open", "high", "low", "close", "vol", "amount"]
    valid_recs = []
    invalid = 0

    for r in recs:
        # Check 1: required fields present and non-null
        missing = [f for f in required if r.get(f) is None]
        if missing:
            invalid += 1
            continue

        # Check 2: OHLC sanity
        try:
            o, h, l, c = (float(r["open"]), float(r["high"]),
                          float(r["low"]), float(r["close"]))
        except (ValueError, TypeError):
            invalid += 1
            continue

        if h < l:
            invalid += 1
            continue

        # Check 3: close must be non-None (already checked, but double-check)
        if pd.isna(c):
            invalid += 1
            continue

        valid_recs.append(r)

    if invalid > 0:
        logger.warning("_validate_ods_batch: %s %s %d/%d rows rejected",
                       api_name, trade_date, invalid, len(recs))

    return valid_recs, invalid


def _get_missing_days_for_stock(con, ts_code: str,
                                all_trading_days: list[str]) -> list[str]:
    """返回该股票在交易日列表中缺失的日期。"""
    if not all_trading_days:
        return []
    existing = set(r[0] for r in con.execute(
        "SELECT trade_date FROM ods_daily WHERE ts_code = ? "
        "AND trade_date >= ? AND trade_date <= ?",
        (ts_code, all_trading_days[0], all_trading_days[-1])
    ).fetchall())
    return [d for d in all_trading_days if d not in existing]


def _drop_suspension_gaps(con, ts_code: str,
                          missing_days: list[str]) -> list[str]:
    """丢弃落在该股交易区间 [first_ods, last_ods] 内部的缺失日（停牌）。

    保留 head (<first_ods) 与 tail (>last_ods) 缺口；无 ODS 行时全部保留。
    """
    if not missing_days:
        return missing_days
    row = con.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM ods_daily WHERE ts_code = ?",
        (ts_code,),
    ).fetchone()
    first_ods = row[0] if row else None
    last_ods = row[1] if row else None
    if not first_ods or not last_ods:
        return missing_days
    return [d for d in missing_days if d < first_ods or d > last_ods]


def _days_to_ranges(days: list[str], all_trading_days: list[str]) -> list[tuple[str, str]]:
    """Merge sorted trading days into contiguous (start, end) segments."""
    if not days:
        return []
    day_to_idx = {d: i for i, d in enumerate(all_trading_days)}
    ranges = []
    seg_start = days[0]
    prev = days[0]
    for d in days[1:]:
        if day_to_idx[d] - day_to_idx[prev] > 1:
            ranges.append((seg_start, prev))
            seg_start = d
        prev = d
    ranges.append((seg_start, prev))
    return ranges


def _merge_ranges(
    ranges_list: list[list[tuple[str, str]]],
    all_trading_days: list[str],
) -> list[tuple[str, str]]:
    """Union of date ranges, merged on the trading-day index."""
    if not ranges_list:
        return []
    day_to_idx = {d: i for i, d in enumerate(all_trading_days)}
    idx_ranges: list[tuple[int, int]] = []
    for ranges in ranges_list:
        for start, end in ranges:
            if start in day_to_idx and end in day_to_idx:
                idx_ranges.append((day_to_idx[start], day_to_idx[end]))
    if not idx_ranges:
        return []
    idx_ranges.sort()
    merged: list[tuple[int, int]] = []
    cur_lo, cur_hi = idx_ranges[0]
    for lo, hi in idx_ranges[1:]:
        if lo <= cur_hi + 1:
            cur_hi = max(cur_hi, hi)
        else:
            merged.append((cur_lo, cur_hi))
            cur_lo, cur_hi = lo, hi
    merged.append((cur_lo, cur_hi))
    return [(all_trading_days[lo], all_trading_days[hi]) for lo, hi in merged]


def _get_missing_ranges_per_stock(con, ts_code: str,
                                   all_trading_days: list[str]) -> list[tuple[str, str]]:
    """返回该股票缺失的连续日期段列表，每个元素为 (start, end)。
    连续缺失的日期合并为一个 range，减少 API 调用次数。"""
    missing = _get_missing_days_for_stock(con, ts_code, all_trading_days)
    missing = _drop_suspension_gaps(con, ts_code, missing)
    return _days_to_ranges(missing, all_trading_days)


def _get_circ_mv_null_days(con, ts_code: str,
                           all_trading_days: list[str]) -> list[str]:
    """ODS daily 已有行但 daily_basic.circ_mv 缺失 — 需补拉 daily_basic。"""
    if not all_trading_days:
        return []
    rows = con.execute(
        """
        SELECT d.trade_date
        FROM ods_daily d
        LEFT JOIN ods_daily_basic b
          ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date
        WHERE d.ts_code = ?
          AND d.trade_date >= ? AND d.trade_date <= ?
          AND b.circ_mv IS NULL
        ORDER BY d.trade_date
        """,
        (ts_code, all_trading_days[0], all_trading_days[-1]),
    ).fetchall()
    return [r[0] for r in rows]


def _get_circ_mv_null_ranges(con, ts_code: str,
                             all_trading_days: list[str]) -> list[tuple[str, str]]:
    return _days_to_ranges(
        _get_circ_mv_null_days(con, ts_code, all_trading_days),
        all_trading_days,
    )


def _get_net_amount_dc_null_days(con, ts_code: str,
                                 all_trading_days: list[str]) -> list[str]:
    """ODS moneyflow 已有行但 net_amount_dc 缺失 — 需补拉 moneyflow_dc。"""
    if not all_trading_days:
        return []
    rows = con.execute(
        """
        SELECT trade_date
        FROM ods_moneyflow
        WHERE ts_code = ?
          AND trade_date >= ? AND trade_date <= ?
          AND net_amount_dc IS NULL
        ORDER BY trade_date
        """,
        (ts_code, all_trading_days[0], all_trading_days[-1]),
    ).fetchall()
    return [r[0] for r in rows]


def _get_net_amount_dc_null_ranges(con, ts_code: str,
                                   all_trading_days: list[str]) -> list[tuple[str, str]]:
    return _days_to_ranges(
        _get_net_amount_dc_null_days(con, ts_code, all_trading_days),
        all_trading_days,
    )


def _backfill_net_amount_dc_stock(
    con, client, ts_code: str, start: str, end: str,
) -> int:
    """UPDATE existing ods_moneyflow rows from moneyflow_dc (no full mf re-fetch)."""
    import pandas as pd

    try:
        recs = client.call(
            "moneyflow_dc", ts_code=ts_code, start_date=start, end_date=end,
        )
    except Exception as exc:
        logger.warning(
            "moneyflow_dc backfill %s [%s~%s] skipped: %s",
            ts_code, start, end, exc,
        )
        return 0
    rows = []
    for r in recs:
        amt = r.get("net_amount")
        if amt is None:
            continue
        rows.append({
            "ts_code": ts_code,
            "trade_date": r["trade_date"],
            "net_amount_dc": amt,
        })
    if not rows:
        return 0
    safe = ts_code.replace(".", "_")
    reg = f"_dc_stock_{safe}_{start}_{end}"
    return _apply_net_amount_dc_patch(con, pd.DataFrame(rows), register_name=reg)


def _backfill_circ_mv_stock(
    con, client, ts_code: str, start: str, end: str,
) -> int:
    """UPDATE ods_daily_basic.circ_mv from daily_basic API (NULL rows only)."""
    import pandas as pd

    try:
        recs = client.call(
            "daily_basic", ts_code=ts_code, start_date=start, end_date=end,
        )
    except Exception as exc:
        logger.warning(
            "daily_basic circ backfill %s [%s~%s] skipped: %s",
            ts_code, start, end, exc,
        )
        return 0
    rows = []
    for r in recs:
        circ = r.get("circ_mv")
        if circ is None:
            continue
        rows.append({
            "ts_code": ts_code,
            "trade_date": r["trade_date"],
            "circ_mv": circ,
        })
    if not rows:
        return 0
    safe = ts_code.replace(".", "_")
    reg = f"_circ_stock_{safe}_{start}_{end}"
    return _apply_circ_mv_patch(con, pd.DataFrame(rows), register_name=reg)


def fetch_stocks_incremental(client, con, ts_codes: list[str],
                              start: str = None,
                              end: str = "20991231") -> int:
    """Stock-batched 增量拉取：每只股票独立检测缺失日期。

    对每只股票：
      1. 查询 ods_daily 已有日期
      2. 连续缺失段合并为 (start, end)
      3. 调用 daily(ts_code=, start_date=, end_date=) 补拉

    返回写入的总行数。
    """
    import time
    import pandas as pd

    if start is None:
        # Default: 90 calendar-day lookback (~60 trading days) instead of 2015-01-01.
        # Callers needing precise warmup should pass explicit start.
        from datetime import datetime as dt, timedelta
        end_dt = dt.strptime(end[:8], "%Y%m%d") if len(end) >= 8 else dt.now()
        start = (end_dt - timedelta(days=90)).strftime("%Y%m%d")

    # Prefer the local dim_date calendar; fall back to trade_cal API when it
    # doesn't fully cover [start, end] (e.g. the 20991231 sentinel end).
    all_days = _local_trading_days(con, start, end)
    if all_days is None:
        try:
            cal = client.call("trade_cal", exchange="SSE",
                              start_date=start, end_date=end, is_open=1)
        except Exception:
            logger.exception("fetch_stocks_incremental: trade_cal API failed")
            return 0
        all_days = sorted([r["cal_date"] for r in cal])
    if not all_days:
        return 0

    prog = stock_progress("fetch.stocks", len(ts_codes))
    prog.log_start(range=f"{start}~{end}")

    total = 0
    t0 = time.time()
    for ts_code in ts_codes:
        daily_ranges = _get_missing_ranges_per_stock(con, ts_code, all_days)
        circ_ranges = _get_circ_mv_null_ranges(con, ts_code, all_days)
        dc_ranges = _get_net_amount_dc_null_ranges(con, ts_code, all_days)
        ranges = _merge_ranges([daily_ranges, circ_ranges, dc_ranges], all_days)

        for seg_start, seg_end in ranges:
            need_daily = any(
                s <= seg_end and e >= seg_start for s, e in daily_ranges
            )
            need_circ = any(
                s <= seg_end and e >= seg_start for s, e in circ_ranges
            )
            need_dc = any(
                s <= seg_end and e >= seg_start for s, e in dc_ranges
            )

            if need_daily:
                try:
                    # 0. Fetch adj_factor per-stock first — daily API doesn't include it
                    adj_recs = client.call("adj_factor", ts_code=ts_code,
                                           start_date=seg_start, end_date=seg_end)
                    adj_map = {a["trade_date"]: a.get("adj_factor") for a in adj_recs}

                    # 1. Daily OHLCV — per-stock, bulk insert via register + SELECT
                    recs = client.call("daily", ts_code=ts_code,
                                       start_date=seg_start, end_date=seg_end)
                    recs, _ = _validate_ods_batch(recs, "daily",
                                                  f"{seg_start}~{seg_end}")
                    daily_data = [{"ts_code": r["ts_code"], "trade_date": r["trade_date"],
                                   "open": r["open"], "high": r["high"], "low": r["low"],
                                   "close": r["close"], "vol": r["vol"],
                                   "amount": r["amount"], "pct_chg": r["pct_chg"],
                                   "adj_factor": adj_map.get(r["trade_date"])}
                                  for r in recs]
                    if daily_data:
                        df = pd.DataFrame(daily_data)
                        con.register("_inc_daily", df)
                        con.execute("""INSERT OR REPLACE INTO ods_daily
                            (ts_code, trade_date, open, high, low, close, vol,
                             amount, pct_chg, adj_factor, fetched_at)
                            SELECT ts_code, trade_date, open, high, low, close, vol,
                                   amount, pct_chg, adj_factor, now()
                            FROM _inc_daily""")
                        con.unregister("_inc_daily")
                        total += len(daily_data)
                except Exception:
                    logger.exception("fetch_stocks_incremental %s [%s~%s]",
                                     ts_code, seg_start, seg_end)

            if need_daily or need_circ:
                try:
                    recs = client.call("daily_basic", ts_code=ts_code,
                                       start_date=seg_start, end_date=seg_end)
                    basic_data = [_daily_basic_record(r) for r in recs]
                    if basic_data:
                        df = pd.DataFrame(basic_data)
                        _insert_ods_daily_basic(con, df, "_inc_basic")
                        total += len(basic_data)
                except Exception as e:
                    logger.warning(
                        "fetch_stocks_incremental %s [%s~%s] daily_basic skipped: %s",
                        ts_code, seg_start, seg_end, e,
                    )

            if need_daily:
                try:
                    recs = client.call("moneyflow", ts_code=ts_code,
                                       start_date=seg_start, end_date=seg_end)
                    mf_data = [_moneyflow_record(r) for r in recs]
                    mf_data = _attach_moneyflow_dc_stock(
                        client, ts_code, seg_start, seg_end, mf_data,
                    )
                    if mf_data:
                        df = pd.DataFrame(mf_data)
                        _insert_ods_moneyflow(con, df, "_inc_mf")
                        total += len(mf_data)
                except Exception as e:
                    logger.warning("fetch_stocks_incremental %s [%s~%s] moneyflow skipped: %s",
                                  ts_code, seg_start, seg_end, e)
            elif need_dc:
                total += _backfill_net_amount_dc_stock(
                    con, client, ts_code, seg_start, seg_end,
                )

        prog.tick()

    elapsed = time.time() - t0
    prog.log_done(rows=total, stocks=len(ts_codes))
    logger.info("Stock fetch complete: %d stocks, %d rows, %.0fs",
                len(ts_codes), total, elapsed)
    return total


def get_all_active_codes(con) -> list[str]:
    """Get all ts_codes that need daily data (not delisted)."""
    return [r[0] for r in con.execute(
        "SELECT ts_code FROM ods_stock_basic WHERE delist_date IS NULL OR delist_date=''"
    ).fetchall()]


def fetch_by_date_range_parallel(start: str, end: str, workers: int = 3,
                                 ts_codes: list[str] = None,
                                 con=None) -> int:
    """Multi-threaded version: each thread processes a chunk of trading days.

    Each thread has its own TushareClient + DuckDB connection.
    DuckDB WAL mode allows concurrent writers.

    Parameters
    ----------
    ts_codes : list[str], optional
        If provided, only INSERT rows for these stocks (API still returns all stocks).
    con : duckdb.DuckDBPyConnection, optional
        Existing connection (used for incremental date detection).
    """
    from backend.fetch.client import TushareClient
    import time

    # Get trading days with incremental skip (single-threaded, 1 API call)
    client = TushareClient()
    if ts_codes is None and con is not None:
        ts_codes = get_all_active_codes(con)
    days = _get_trading_days(client, start, end, con=con, ts_codes=ts_codes)
    if not days:
        logger.info("All dates already in DB — nothing to fetch (%s~%s)", start, end)
        return 0
    code_set = set(ts_codes) if ts_codes else None
    filter_msg = f" ({len(ts_codes)} stocks)" if ts_codes else ""
    t0 = time.time()
    prog = day_progress("fetch.ods", len(days))
    prog.log_start(threads=workers, range=f"{start}~{end}", stocks=filter_msg.strip())

    # Split days into chunks
    chunk_size = max(1, len(days) // workers)
    chunks = [days[i:i + chunk_size] for i in range(0, len(days), chunk_size)]

    def _fetch_chunk(trade_dates: list[str]) -> int:
        """Process one chunk of trading days in a thread.

        Uses DuckDB register() + INSERT INTO SELECT for bulk insert
        (~666x faster than executemany: 0.04s vs 28s per 5500 rows).
        """
        import pandas as pd
        thread_client = TushareClient()
        thread_con = duckdb.connect(DUCKDB_PATH)
        total = 0
        for trade_date in trade_dates:
            try:
                adj_recs = thread_client.call("adj_factor", trade_date=trade_date)
                adj_map = {a["ts_code"]: a.get("adj_factor") for a in adj_recs}
                total += len(adj_recs)

                # --- ods_daily: bulk INSERT via register + SELECT ---
                recs = thread_client.call("daily", trade_date=trade_date)
                recs, _ = _validate_ods_batch(recs, "daily", trade_date)
                daily_data = []
                for r in recs:
                    if code_set and r["ts_code"] not in code_set:
                        continue
                    adj = adj_map.get(r["ts_code"])
                    daily_data.append({"ts_code": r["ts_code"],
                        "trade_date": r["trade_date"], "open": r["open"],
                        "high": r["high"], "low": r["low"],
                        "close": r["close"], "vol": r["vol"],
                        "amount": r["amount"], "pct_chg": r["pct_chg"],
                        "adj_factor": adj})
                if daily_data:
                    df = pd.DataFrame(daily_data)
                    thread_con.register("_daily_batch", df)
                    thread_con.execute("""
                        INSERT OR REPLACE INTO ods_daily
                        (ts_code, trade_date, open, high, low, close,
                         vol, amount, pct_chg, adj_factor, fetched_at)
                        SELECT ts_code, trade_date, open, high, low, close,
                               vol, amount, pct_chg, adj_factor, now()
                        FROM _daily_batch
                    """)
                    thread_con.unregister("_daily_batch")
                    total += len(daily_data)

                # --- ods_daily_basic: bulk INSERT via register + SELECT ---
                recs = thread_client.call("daily_basic", trade_date=trade_date)
                basic_data = []
                for r in recs:
                    if code_set and r["ts_code"] not in code_set:
                        continue
                    basic_data.append(_daily_basic_record(r))
                if basic_data:
                    df = pd.DataFrame(basic_data)
                    _insert_ods_daily_basic(thread_con, df)
                    total += len(basic_data)

                # --- ods_moneyflow: bulk INSERT via register + SELECT ---
                recs = thread_client.call("moneyflow", trade_date=trade_date)
                mf_data = []
                for r in recs:
                    if code_set and r["ts_code"] not in code_set:
                        continue
                    mf_data.append(_moneyflow_record(r))
                mf_data = _attach_moneyflow_dc(thread_client, trade_date, mf_data)
                if mf_data:
                    df = pd.DataFrame(mf_data)
                    _insert_ods_moneyflow(thread_con, df)
                    total += len(mf_data)

            except Exception:
                logger.exception("Thread failed trade_date=%s", trade_date)
            prog.tick()
        thread_con.close()
        return total

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_fetch_chunk, chunks))

    total_rows = sum(results)
    elapsed = time.time() - t0
    prog.log_done(rows=total_rows, days=len(days))
    logger.info("ODS fetch complete: %d rows in %.0fs (%.1f days/min)",
                total_rows, elapsed, len(days) / elapsed * 60 if elapsed > 0 else 0)
    return total_rows
