"""DWD layer construction — forward-adjusted prices, weekly aggregation, moneyflow mapping."""

import logging
import time
from typing import List, Optional, Set

from backend.etl.dwd_weekly_sql import week_trunc_expr, weekly_insert_select_sql

logger = logging.getLogger(__name__)

_LARGE_REBUILD_WARN = 500


def _find_dwd_gap_stocks(con, ts_codes: Optional[List[str]] = None) -> List[str]:
    """Stocks whose DWD span still has internal calendar gaps (needs suspension fill).

    Uses dwd_daily_quote row count vs dim_date trading days — not ODS vs calendar.
    After a successful fill, d.n equals calendar days so the stock is excluded on
    the next rebuild (ODS gaps from suspensions remain permanently).
    """
    inner_filter = ""
    params: list = []
    if ts_codes is not None:
        if len(ts_codes) == 0:
            return []
        placeholders = ",".join(["?" for _ in ts_codes])
        inner_filter = f"WHERE ts_code IN ({placeholders})"
        params = list(ts_codes)

    rows = con.execute(
        f"""
        SELECT d.ts_code
        FROM (
            SELECT ts_code, COUNT(*) AS n,
                   MIN(trade_date) AS mn, MAX(trade_date) AS mx
            FROM dwd_daily_quote
            {inner_filter}
            GROUP BY ts_code
        ) d
        JOIN dim_date dd
          ON dd.is_trade_day = 1
         AND dd.trade_date >= d.mn AND dd.trade_date <= d.mx
        GROUP BY d.ts_code, d.n
        HAVING d.n < COUNT(dd.trade_date)
        """,
        params,
    ).fetchall()
    return [r[0] for r in rows]


def find_adj_changed_codes(con, ts_codes: List[str], trade_date: str) -> List[str]:
    """Stocks whose latest ODS adj_factor changed on trade_date (full qfq rebuild needed)."""
    if not ts_codes:
        return []
    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(
        f"""
        WITH ranked AS (
            SELECT ts_code, trade_date, adj_factor,
                   ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
            FROM ods_daily
            WHERE ts_code IN ({placeholders})
        )
        SELECT cur.ts_code
        FROM ranked cur
        JOIN ranked prev ON cur.ts_code = prev.ts_code AND prev.rn = 2
        WHERE cur.rn = 1
          AND cur.trade_date = ?
          AND cur.adj_factor IS NOT NULL
          AND prev.adj_factor IS NOT NULL
          AND cur.adj_factor <> prev.adj_factor
        """,
        list(ts_codes) + [trade_date],
    ).fetchall()
    return [r[0] for r in rows]


def find_qfq_drift_codes(con, ts_codes: List[str]) -> List[str]:
    """Stocks whose stored close_qfq no longer matches current ODS adj factors."""
    if not ts_codes:
        return []
    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(
        f"""
        SELECT DISTINCT d.ts_code
        FROM dwd_daily_quote d
        JOIN ods_daily o
          ON d.ts_code = o.ts_code AND d.trade_date = o.trade_date
        JOIN (
            SELECT ts_code, adj_factor AS latest_adj
            FROM ods_daily
            WHERE (ts_code, trade_date) IN (
                SELECT ts_code, MAX(trade_date) FROM ods_daily GROUP BY ts_code
            )
        ) la ON d.ts_code = la.ts_code
        WHERE d.ts_code IN ({placeholders})
          AND o.adj_factor IS NOT NULL
          AND la.latest_adj IS NOT NULL AND la.latest_adj <> 0
          AND ABS(
              d.close_qfq - o.close * o.adj_factor / la.latest_adj
          ) > 1e-6
        """,
        list(ts_codes),
    ).fetchall()
    return [r[0] for r in rows]


def find_stocks_needing_qfq_refresh(
    con, ts_codes: List[str], trade_date: str,
) -> List[str]:
    """Stocks needing qfq price UPDATE: latest-day adj change or historical drift."""
    if not ts_codes:
        return []
    full_set: Set[str] = set()
    full_set.update(find_adj_changed_codes(con, ts_codes, trade_date))
    full_set.update(find_qfq_drift_codes(con, ts_codes))
    return sorted(full_set)


def find_stocks_needing_full_daily_insert(
    con, ts_codes: List[str], trade_date: str,
) -> List[str]:
    """Stocks with ODS on trade_date but no prior DWD rows (new listing tail)."""
    if not ts_codes:
        return []
    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(
        f"""
        SELECT o.ts_code
        FROM ods_daily o
        WHERE o.trade_date = ? AND o.ts_code IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM dwd_daily_quote d
              WHERE d.ts_code = o.ts_code AND d.trade_date < ?
          )
        """,
        [trade_date] + list(ts_codes) + [trade_date],
    ).fetchall()
    return sorted({r[0] for r in rows})


def find_stocks_missing_dwd_trade_date(
    con, ts_codes: List[str], trade_date: str,
) -> List[str]:
    """Stocks with ODS on trade_date but DWD daily max strictly before it."""
    if not ts_codes:
        return []
    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(
        f"""
        SELECT o.ts_code
        FROM ods_daily o
        LEFT JOIN (
            SELECT ts_code, MAX(trade_date) AS mx
            FROM dwd_daily_quote
            WHERE ts_code IN ({placeholders})
            GROUP BY ts_code
        ) d ON o.ts_code = d.ts_code
        WHERE o.trade_date = ?
          AND o.ts_code IN ({placeholders})
          AND (d.mx IS NULL OR d.mx < ?)
        """,
        list(ts_codes) + [trade_date] + list(ts_codes) + [trade_date],
    ).fetchall()
    return sorted({r[0] for r in rows})


def find_stocks_needing_full_daily_rebuild(
    con, ts_codes: List[str], trade_date: str,
) -> List[str]:
    """Union helper: qfq refresh + full insert (no DELETE for qfq-only stocks)."""
    full_set: Set[str] = set(
        find_stocks_needing_qfq_refresh(con, ts_codes, trade_date),
    )
    full_set.update(
        find_stocks_needing_full_daily_insert(con, ts_codes, trade_date),
    )
    return sorted(full_set)


def _check_daily_basic_coverage(con, ts_codes: list, trade_date: str,
                                 min_ratio: float = 0.80) -> bool:
    """Return True if ods_daily_basic covers >= min_ratio of ts_codes for trade_date.

    Uses total_mv IS NOT NULL as the liveness signal (pe_ttm is naturally NULL for
    loss-making stocks).
    """
    if not ts_codes:
        return True
    placeholders = ",".join(["?" for _ in ts_codes])
    covered = con.execute(f"""
        SELECT COUNT(DISTINCT b.ts_code)
        FROM ods_daily_basic b
        WHERE b.trade_date = ? AND b.ts_code IN ({placeholders})
          AND b.total_mv IS NOT NULL
    """, [trade_date] + list(ts_codes)).fetchone()[0]
    return covered / len(ts_codes) >= min_ratio


def refresh_qfq_prices(con, ts_codes: List[str]) -> int:
    """UPDATE dwd_daily_quote qfq columns from ODS adj factors (no DELETE)."""
    if not ts_codes:
        return 0

    placeholders = ",".join(["?" for _ in ts_codes])
    logger.info(
        "progress dwd.qfq_update: started | stocks=%d", len(ts_codes),
    )

    result = con.execute(
        f"""
        UPDATE dwd_daily_quote AS q
        SET
            open_qfq  = o.open  * o.adj_factor / la.latest_adj,
            high_qfq  = o.high  * o.adj_factor / la.latest_adj,
            low_qfq   = o.low   * o.adj_factor / la.latest_adj,
            close_qfq = o.close * o.adj_factor / la.latest_adj
        FROM ods_daily AS o
        JOIN (
            SELECT ts_code, adj_factor AS latest_adj
            FROM ods_daily
            WHERE (ts_code, trade_date) IN (
                SELECT ts_code, MAX(trade_date) FROM ods_daily GROUP BY ts_code
            )
        ) la ON o.ts_code = la.ts_code
        WHERE q.ts_code = o.ts_code
          AND q.trade_date = o.trade_date
          AND q.ts_code IN ({placeholders})
          AND o.adj_factor IS NOT NULL
          AND la.latest_adj IS NOT NULL AND la.latest_adj <> 0
        """,
        list(ts_codes),
    )
    n = result.fetchone()[0]
    logger.info("progress dwd.qfq_update: done | rows=%d", n)
    return n


def rebuild_dwd_for_stale(con, ts_codes: List[str], trade_date: str) -> dict:
    """Rebuild stale stocks: incremental tail when DWD_INCREMENTAL=1, else full."""
    from backend.config import DWD_INCREMENTAL

    if DWD_INCREMENTAL:
        return rebuild_dwd_incremental(con, ts_codes, trade_date)
    return rebuild_all_dwd(con, ts_codes)


def rebuild_dwd_incremental(con, ts_codes: List[str], trade_date: str) -> dict:
    """Rebuild DWD for a stale stock subset on a new trading day.

    Daily: qfq UPDATE for adj/drift; full insert for new listings; tail INSERT
    otherwise. Weekly: week-partition for tail; full rebuild for qfq/insert stocks;
    moneyflow tail INSERT.
    """
    if not ts_codes:
        return {"daily_quote": 0, "weekly_quote": 0, "moneyflow": 0}

    logger.info(
        "progress dwd.rebuild_incremental: started | stocks=%d date=%s",
        len(ts_codes), trade_date,
    )
    t0 = time.monotonic()
    qfq_codes = find_stocks_needing_qfq_refresh(con, ts_codes, trade_date)
    insert_codes = find_stocks_needing_full_daily_insert(
        con, ts_codes, trade_date,
    )
    missing_day = find_stocks_missing_dwd_trade_date(con, ts_codes, trade_date)
    tail_daily_codes = [c for c in missing_day if c not in set(insert_codes)]
    full_weekly_codes = sorted(set(qfq_codes) | set(insert_codes))
    tail_weekly_codes = [
        c for c in tail_daily_codes if c not in set(full_weekly_codes)
    ]

    n_daily = 0
    if qfq_codes:
        n_daily += refresh_qfq_prices(con, qfq_codes)
    if insert_codes:
        logger.info(
            "dwd incremental: full daily insert for %d stocks (no prior DWD)",
            len(insert_codes),
        )
        n_daily += build_dwd_daily_quote(con, insert_codes)
    if tail_daily_codes:
        from backend.config import DWD_DAILY_BASIC_MIN_COVERAGE
        if not _check_daily_basic_coverage(
            con, tail_daily_codes, trade_date, DWD_DAILY_BASIC_MIN_COVERAGE,
        ):
            placeholders = ",".join(["?" for _ in tail_daily_codes])
            covered = con.execute(f"""
                SELECT COUNT(DISTINCT b.ts_code)
                FROM ods_daily_basic b
                WHERE b.trade_date = ? AND b.ts_code IN ({placeholders})
                  AND b.total_mv IS NOT NULL
            """, [trade_date] + list(tail_daily_codes)).fetchone()[0]
            ratio = covered / len(tail_daily_codes) if tail_daily_codes else 0
            logger.warning(
                "dwd incremental: daily_basic coverage %.1f%% below threshold %.0f%% — "
                "deferring tail INSERT for %d stocks (will retry on next run)",
                ratio * 100, DWD_DAILY_BASIC_MIN_COVERAGE * 100,
                len(tail_daily_codes),
            )
            deferred = set(tail_daily_codes)
            tail_daily_codes.clear()
            tail_weekly_codes = [
                c for c in tail_weekly_codes if c not in deferred
            ]
        if tail_daily_codes:
            logger.info(
                "dwd incremental: tail INSERT for %d stocks on %s",
                len(tail_daily_codes), trade_date,
            )
            n_daily += build_dwd_daily_quote(
                con, tail_daily_codes, incremental_trade_date=trade_date,
            )

    n_weekly = 0
    if tail_weekly_codes:
        n_weekly += build_dwd_weekly_quote(
            con, tail_weekly_codes, incremental_trade_date=trade_date,
        )
    if full_weekly_codes:
        n_weekly += build_dwd_weekly_quote(con, full_weekly_codes)
    n_mf = build_dwd_daily_moneyflow(
        con, ts_codes, incremental_trade_date=trade_date,
    )
    result = {
        "daily_quote": n_daily,
        "weekly_quote": n_weekly,
        "moneyflow": n_mf,
    }
    logger.info(
        "progress dwd.rebuild_incremental: done | %.0fs | %s",
        time.monotonic() - t0, result,
    )
    return result


def rebuild_all_dwd(con, ts_codes=None) -> dict:
    """Rebuild all 3 DWD tables in correct order. Single entry point for DWD refresh.

    Returns {"daily_quote": n, "weekly_quote": n, "moneyflow": n}.
    """
    if ts_codes is not None and len(ts_codes) > _LARGE_REBUILD_WARN:
        logger.warning(
            "large subset rebuild: %d stocks via rebuild_all_dwd (prefer rebuild_dwd_for_stale)",
            len(ts_codes),
        )
    stock_label = len(ts_codes) if ts_codes else "all"
    logger.info("progress dwd.rebuild: started | stocks=%s", stock_label)
    t0 = time.monotonic()
    result = {
        "daily_quote": build_dwd_daily_quote(con, ts_codes),
        "weekly_quote": build_dwd_weekly_quote(con, ts_codes),
        "moneyflow": build_dwd_daily_moneyflow(con, ts_codes),
    }
    logger.info(
        "progress dwd.rebuild: done | %.0fs | %s",
        time.monotonic() - t0, result,
    )
    return result


def build_dwd_daily_quote(
    con, ts_codes=None, incremental_trade_date: Optional[str] = None,
) -> int:
    """Build dwd_daily_quote: single-pass batch 前复权 for all stocks.

    Uses a window-function subquery to compute latest_adj_factor per stock
    in one pass, then joins against ods_daily + ods_daily_basic for a single
    mass INSERT. Suspension days are filled in a separate batch pass using
    LATERAL JOIN per stock (necessary because prev-close is stock-specific).

    incremental_trade_date: when set with ts_codes, skip DELETE and only
    INSERT OR REPLACE rows for that trade_date (daily tail update).
    """
    if incremental_trade_date is not None and ts_codes is None:
        raise ValueError("incremental_trade_date requires ts_codes")

    if ts_codes is None:
        con.execute("DELETE FROM dwd_daily_quote")
        code_filter = ""
        date_filter = ""
        params = []
    elif len(ts_codes) == 0:
        return 0  # nothing to do
    else:
        placeholders = ",".join(["?" for _ in ts_codes])
        if incremental_trade_date is None:
            con.execute(
                f"DELETE FROM dwd_daily_quote WHERE ts_code IN ({placeholders})",
                ts_codes,
            )
            date_filter = ""
            params = list(ts_codes)
        else:
            date_filter = "AND d.trade_date = ?"
            params = list(ts_codes) + [incremental_trade_date]
        code_filter = f"AND d.ts_code IN ({placeholders})"

    code_params = list(ts_codes) if ts_codes else []

    stock_label = len(ts_codes) if ts_codes else "all"
    mode = f"tail={incremental_trade_date}" if incremental_trade_date else "full"
    logger.info("progress dwd.daily_quote: started | stocks=%s mode=%s", stock_label, mode)

    # Step 0: Diagnostic — count rows that will be excluded due to missing/zero
    # adj_factor (would otherwise silently produce NULL close_qfq).
    excluded = con.execute(
        f"""SELECT COUNT(*)
        FROM ods_daily d
        JOIN (
            SELECT ts_code, adj_factor AS latest_adj
            FROM ods_daily
            WHERE (ts_code, trade_date) IN (
                SELECT ts_code, MAX(trade_date) FROM ods_daily GROUP BY ts_code
            )
        ) la ON d.ts_code = la.ts_code
        WHERE (d.adj_factor IS NULL OR la.latest_adj IS NULL OR la.latest_adj = 0)
          {code_filter}""",
        code_params,
    ).fetchone()[0]
    if excluded:
        logger.warning("build_dwd_daily_quote: excluding %d rows with missing/zero "
                       "adj_factor (qfq price unavailable)", excluded)

    # Step 1: Single-pass batch 前复权 for ALL stocks
    # latest_adj factor computed via correlated max-date subquery per stock
    # Rows with NULL adj_factor or NULL/zero latest_adj are excluded to avoid
    # silently producing NULL close_qfq (or division-by-zero).
    con.execute(
        f"""INSERT OR REPLACE INTO dwd_daily_quote
            (ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq,
             vol, amount, pct_chg, total_mv, circ_mv, pe_ttm, turnover_rate,
             volume_ratio, is_suspended)
        SELECT d.ts_code, d.trade_date,
            d.open  * d.adj_factor / la.latest_adj,
            d.high  * d.adj_factor / la.latest_adj,
            d.low   * d.adj_factor / la.latest_adj,
            d.close * d.adj_factor / la.latest_adj,
            d.vol, d.amount, d.pct_chg,
            b.total_mv, b.circ_mv, b.pe_ttm, b.turnover_rate, b.volume_ratio,
            0
        FROM ods_daily d
        JOIN (
            SELECT ts_code, adj_factor AS latest_adj
            FROM ods_daily
            WHERE (ts_code, trade_date) IN (
                SELECT ts_code, MAX(trade_date) FROM ods_daily GROUP BY ts_code
            )
        ) la ON d.ts_code = la.ts_code
        LEFT JOIN ods_daily_basic b
            ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date
        WHERE d.adj_factor IS NOT NULL
          AND la.latest_adj IS NOT NULL AND la.latest_adj <> 0
          {code_filter}{date_filter}""",
        params,
    )

    # Step 2: Suspension fill only for stocks with DWD calendar gaps (not ODS gaps).
    if incremental_trade_date is not None:
        fill_list = []
    else:
        codes_to_fill = ts_codes if ts_codes else [
            r[0] for r in con.execute("SELECT ts_code FROM dim_stock").fetchall()
        ]
        gap_stocks = set(_find_dwd_gap_stocks(con, ts_codes))
        fill_list = [c for c in codes_to_fill if c in gap_stocks]
    if fill_list:
        from backend.etl.progress import StageProgress
        sp = StageProgress("dwd.suspension_fill", len(fill_list), count_step=50, unit="stocks")
        sp.log_start()
        for ts_code in fill_list:
            con.execute(
                """INSERT OR REPLACE INTO dwd_daily_quote
                    (ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq,
                     vol, amount, pct_chg, total_mv, circ_mv, pe_ttm, turnover_rate,
                     volume_ratio, is_suspended)
                SELECT ?, cal.trade_date,
                    prev.close_qfq, prev.close_qfq, prev.close_qfq, prev.close_qfq,
                    0, 0, 0,
                    prev.total_mv, prev.circ_mv, prev.pe_ttm, prev.turnover_rate,
                    prev.volume_ratio,
                    1
                FROM dim_date cal
                LEFT JOIN LATERAL (
                    SELECT close_qfq, total_mv, circ_mv, pe_ttm, turnover_rate,
                           volume_ratio
                    FROM dwd_daily_quote
                    WHERE ts_code = ? AND trade_date < cal.trade_date
                    ORDER BY trade_date DESC LIMIT 1
                ) prev ON TRUE
                WHERE cal.is_trade_day = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM dwd_daily_quote q
                      WHERE q.ts_code = ? AND q.trade_date = cal.trade_date
                  )
                  AND cal.trade_date <= (SELECT MAX(trade_date) FROM ods_daily WHERE ts_code = ?)
                  AND prev.close_qfq IS NOT NULL""",
                [ts_code, ts_code, ts_code, ts_code],
            )
            sp.tick()
        sp.log_done()

    if incremental_trade_date is not None and ts_codes:
        placeholders = ",".join(["?" for _ in ts_codes])
        n = con.execute(
            f"SELECT COUNT(*) FROM dwd_daily_quote "
            f"WHERE ts_code IN ({placeholders}) AND trade_date = ?",
            list(ts_codes) + [incremental_trade_date],
        ).fetchone()[0]
    elif ts_codes:
        placeholders = ",".join(["?" for _ in ts_codes])
        n = con.execute(
            f"SELECT COUNT(*) FROM dwd_daily_quote WHERE ts_code IN ({placeholders})",
            ts_codes,
        ).fetchone()[0]
    else:
        n = con.execute("SELECT COUNT(*) FROM dwd_daily_quote").fetchone()[0]
    logger.info("progress dwd.daily_quote: done | rows=%d", n)
    return n


def build_dwd_weekly_quote(
    con, ts_codes=None, incremental_trade_date: Optional[str] = None,
) -> int:
    """Build rolling weekly bars: each trading day gets a week-to-date bar.

    Uses DuckDB window functions partitioned by week via date_trunc('week', ...)
    (Monday-anchored, so cross-year weeks stay in one partition). Each day
    aggregates all non-suspended days in the same week up to and including it.
    open uses FIRST_VALUE (Monday's open), close uses current day's close.
    vol/amount normalized to 5-day equivalent (SUM/active_days*5).

    incremental_trade_date: when set with ts_codes, DELETE and INSERT only the
    week partition containing that trade_date (weekly tail update).
    """
    if incremental_trade_date is not None and ts_codes is None:
        raise ValueError("incremental_trade_date requires ts_codes")

    week_date_expr = (
        "date_trunc('week', CAST(substr(?,1,4)||'-'||substr(?,5,2)||'-'||substr(?,7,2) AS DATE))"
    )
    week_params = (
        [incremental_trade_date, incremental_trade_date, incremental_trade_date]
        if incremental_trade_date is not None
        else []
    )

    if ts_codes is None:
        con.execute("DELETE FROM dwd_weekly_quote")
        ts_code_filter = ""
        week_filter = ""
        params = []
    elif len(ts_codes) == 0:
        return 0
    else:
        placeholders = ",".join(["?" for _ in ts_codes])
        if incremental_trade_date is None:
            con.execute(
                f"DELETE FROM dwd_weekly_quote WHERE ts_code IN ({placeholders})",
                ts_codes,
            )
            week_filter = ""
            params = list(ts_codes)
        else:
            con.execute(
                f"""DELETE FROM dwd_weekly_quote w
                WHERE w.ts_code IN ({placeholders})
                  AND {week_trunc_expr('w.trade_date')} = {week_date_expr}""",
                list(ts_codes) + week_params,
            )
            week_filter = f" AND {week_trunc_expr('d.trade_date')} = {week_date_expr}"
            params = list(ts_codes) + week_params
        ts_code_filter = f"AND d.ts_code IN ({placeholders})"

    stock_label = len(ts_codes) if ts_codes else "all"
    if incremental_trade_date:
        mode = f"week={incremental_trade_date}"
    else:
        mode = "full"
    logger.info(
        "progress dwd.weekly_quote: started | stocks=%s mode=%s", stock_label, mode,
    )

    con.execute(
        f"""INSERT OR REPLACE INTO dwd_weekly_quote
            (ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq,
             vol, amount, pct_chg, total_mv, pe_ttm, turnover_rate, volume_ratio,
             active_days)
        {weekly_insert_select_sql(ts_code_filter, week_filter)}""",
        params,
    )

    if incremental_trade_date is not None and ts_codes:
        placeholders = ",".join(["?" for _ in ts_codes])
        n = con.execute(
            f"""SELECT COUNT(*) FROM dwd_weekly_quote w
            WHERE w.ts_code IN ({placeholders})
              AND {week_trunc_expr('w.trade_date')} = {week_date_expr}""",
            list(ts_codes) + week_params,
        ).fetchone()[0]
    elif ts_codes:
        placeholders = ",".join(["?" for _ in ts_codes])
        n = con.execute(
            f"SELECT COUNT(*) FROM dwd_weekly_quote WHERE ts_code IN ({placeholders})",
            ts_codes,
        ).fetchone()[0]
    else:
        n = con.execute("SELECT COUNT(*) FROM dwd_weekly_quote").fetchone()[0]
    logger.info("progress dwd.weekly_quote: done | rows=%d", n)
    return n


def build_dwd_daily_moneyflow(
    con, ts_codes=None, incremental_trade_date: Optional[str] = None,
) -> int:
    """Map ods_moneyflow → dwd_daily_moneyflow.

    - net_mf_vol, net_mf_amount: direct copy
    - buy_lg_vol, sell_lg_vol, buy_elg_vol, sell_elg_vol: direct copy
    - total_vol = buy_sm_vol + buy_md_vol + buy_lg_vol + buy_elg_vol
      (buy-side sum, used as DDX denominator)
    """
    if incremental_trade_date is not None and ts_codes is None:
        raise ValueError("incremental_trade_date requires ts_codes")

    if ts_codes is None:
        con.execute("DELETE FROM dwd_daily_moneyflow")
        ts_code_filter = ""
        date_filter = ""
        params = []
    elif len(ts_codes) == 0:
        return 0
    else:
        placeholders = ",".join(["?" for _ in ts_codes])
        if incremental_trade_date is None:
            con.execute(
                f"DELETE FROM dwd_daily_moneyflow WHERE ts_code IN ({placeholders})",
                ts_codes,
            )
            ts_code_filter = f"WHERE m.ts_code IN ({placeholders})"
            date_filter = ""
            params = list(ts_codes)
        else:
            ts_code_filter = f"WHERE m.ts_code IN ({placeholders})"
            date_filter = "AND m.trade_date = ?"
            params = list(ts_codes) + [incremental_trade_date]

    stock_label = len(ts_codes) if ts_codes else "all"
    mode = f"tail={incremental_trade_date}" if incremental_trade_date else "full"
    logger.info("progress dwd.moneyflow: started | stocks=%s mode=%s", stock_label, mode)

    con.execute(
        f"""INSERT OR REPLACE INTO dwd_daily_moneyflow
            (ts_code, trade_date, net_mf_vol, net_mf_amount, net_amount_dc,
             buy_lg_vol, sell_lg_vol, buy_elg_vol, sell_elg_vol, total_vol)
        SELECT
            ts_code,
            trade_date,
            net_mf_vol,
            net_mf_amount,
            net_amount_dc,
            buy_lg_vol,
            sell_lg_vol,
            buy_elg_vol,
            sell_elg_vol,
            COALESCE(buy_sm_vol, 0)
                + COALESCE(buy_md_vol, 0)
                + COALESCE(buy_lg_vol, 0)
                + COALESCE(buy_elg_vol, 0) AS total_vol
        FROM ods_moneyflow m
        {ts_code_filter}{date_filter}""",
        params,
    )

    if incremental_trade_date is not None and ts_codes:
        placeholders = ",".join(["?" for _ in ts_codes])
        n = con.execute(
            f"SELECT COUNT(*) FROM dwd_daily_moneyflow "
            f"WHERE ts_code IN ({placeholders}) AND trade_date = ?",
            list(ts_codes) + [incremental_trade_date],
        ).fetchone()[0]
    elif ts_codes:
        placeholders = ",".join(["?" for _ in ts_codes])
        n = con.execute(
            f"SELECT COUNT(*) FROM dwd_daily_moneyflow WHERE ts_code IN ({placeholders})",
            ts_codes,
        ).fetchone()[0]
    else:
        n = con.execute("SELECT COUNT(*) FROM dwd_daily_moneyflow").fetchone()[0]
    logger.info("progress dwd.moneyflow: done | rows=%d", n)
    return n
