"""Build dwd_index_daily and dwd_index_weekly from ODS index tables."""
import logging

logger = logging.getLogger(__name__)

# DuckDB date parsing helper: YYYYMMDD → YYYY-MM-DD → DATE
# (same approach as dwd_weekly_sql.py)
_DATE_PARSE = "CAST(substr(trade_date,1,4)||'-'||substr(trade_date,5,2)||'-'||substr(trade_date,7,2) AS DATE)"

# Week anchor: Monday (date_trunc('week', dt)), consistent with stock dwd_weekly_quote
_WEEK_TRUNC = f"date_trunc('week', {_DATE_PARSE})"


def build_dwd_index_daily(con) -> int:
    """Build dwd_index_daily from ods_index_daily LEFT JOIN ods_index_dailybasic.
    Full rebuild (DELETE + INSERT) — index data volume is tiny (~5K rows per index).

    NOTE: close_qfq = close (indices don't need qfq adjustment, column name aligns
    with calculator SIGNATURE_COLS). is_suspended = 0 always (indices never suspend).
    """
    logger.info("progress build_dwd_index_daily: started")

    con.execute("DELETE FROM dwd_index_daily")
    con.execute("""
        INSERT INTO dwd_index_daily (
            ts_code, trade_date,
            close, open, high, low, pre_close, pct_chg, vol, amount,
            close_qfq, total_mv, pe_ttm, pb, turnover_rate, is_suspended
        )
        SELECT
            d.ts_code,
            d.trade_date,
            d.close,
            d.open,
            d.high,
            d.low,
            d.pre_close,
            d.pct_chg,
            d.vol,
            d.amount,
            d.close AS close_qfq,
            b.total_mv,
            b.pe_ttm,
            b.pb,
            b.turnover_rate,
            0 AS is_suspended
        FROM ods_index_daily d
        LEFT JOIN ods_index_dailybasic b
            ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date
        ORDER BY d.ts_code, d.trade_date
    """)

    n = con.execute("SELECT COUNT(*) FROM dwd_index_daily").fetchone()[0]
    logger.info("progress build_dwd_index_daily: done | rows=%d", n)
    return n


def build_dwd_index_weekly(con) -> int:
    """Build dwd_index_weekly by aggregating dwd_index_daily to ISO weeks.

    Week anchor: Monday (date_trunc('week', dt)), consistent with stock dwd_weekly_quote.
    Output trade_date is Friday of the ISO week (anchor + 4 days).
    """
    logger.info("progress build_dwd_index_weekly: started")

    con.execute("DELETE FROM dwd_index_weekly")
    con.execute(f"""
        INSERT INTO dwd_index_weekly (
            ts_code, trade_date,
            close, open, high, low, pct_chg, vol, amount,
            close_qfq, total_mv, pe_ttm, turnover_rate, active_days
        )
        SELECT
            ts_code,
            strftime({_WEEK_TRUNC} + INTERVAL 4 DAY, '%Y%m%d') AS trade_date,
            LAST(close ORDER BY trade_date) AS close,
            FIRST(open ORDER BY trade_date) AS open,
            MAX(high) AS high,
            MIN(low) AS low,
            (LAST(close ORDER BY trade_date) / NULLIF(FIRST(open ORDER BY trade_date), 0) - 1) * 100 AS pct_chg,
            SUM(vol) AS vol,
            SUM(amount) AS amount,
            LAST(close_qfq ORDER BY trade_date) AS close_qfq,
            LAST(total_mv ORDER BY trade_date) AS total_mv,
            LAST(pe_ttm ORDER BY trade_date) AS pe_ttm,
            LAST(turnover_rate ORDER BY trade_date) AS turnover_rate,
            COUNT(*) AS active_days
        FROM dwd_index_daily
        WHERE close IS NOT NULL
        GROUP BY ts_code, {_WEEK_TRUNC}
        ORDER BY ts_code, {_WEEK_TRUNC}
    """)

    n = con.execute("SELECT COUNT(*) FROM dwd_index_weekly").fetchone()[0]
    logger.info("progress build_dwd_index_weekly: done | rows=%d", n)
    return n


def build_dwd_index_all(con) -> dict:
    """Build both index DWD tables. Returns {{table: row_count}}."""
    result = {}
    result["dwd_index_daily"] = build_dwd_index_daily(con)
    result["dwd_index_weekly"] = build_dwd_index_weekly(con)
    return result
