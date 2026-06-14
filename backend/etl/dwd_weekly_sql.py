"""Shared SQL for dwd_weekly_quote rolling WTD bars."""


def _trade_date_to_date_expr(col: str = "d.trade_date") -> str:
    return (
        f"CAST(substr({col},1,4)||'-'||substr({col},5,2)||'-'||substr({col},7,2) AS DATE)"
    )


def week_trunc_expr(col: str = "d.trade_date") -> str:
    return f"date_trunc('week', {_trade_date_to_date_expr(col)})"


def weekly_insert_select_sql(ts_code_filter: str, week_filter: str = "") -> str:
    dt = _trade_date_to_date_expr("d.trade_date")
    return f"""
        SELECT
            d.ts_code,
            d.trade_date,
            FIRST_VALUE(d.open_qfq) OVER w AS open_qfq,
            MAX(d.high_qfq) OVER w AS high_qfq,
            MIN(d.low_qfq) OVER w AS low_qfq,
            d.close_qfq AS close_qfq,
            SUM(d.vol) OVER w / COUNT(*) OVER w * 5 AS vol,
            SUM(d.amount) OVER w / COUNT(*) OVER w * 5 AS amount,
            SUM(d.pct_chg) OVER w AS pct_chg,
            d.total_mv, d.pe_ttm, d.turnover_rate, d.volume_ratio,
            COUNT(*) OVER w AS active_days
        FROM dwd_daily_quote d
        WHERE d.is_suspended = 0 {ts_code_filter}{week_filter}
        WINDOW w AS (PARTITION BY d.ts_code, date_trunc('week', {dt})
                     ORDER BY d.trade_date
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    """
