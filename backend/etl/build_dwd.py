"""DWD layer construction — forward-adjusted prices, weekly aggregation, moneyflow mapping."""


def build_dwd_daily_quote(con, ts_codes=None) -> int:
    """Build dwd_daily_quote from ods_daily + ods_daily_basic.

    For each stock:
    1. Compute 前复权 (forward-adjusted) prices:
       price_qfq = price * adj_factor / latest_adj_factor
    2. LEFT JOIN ods_daily_basic for PE, market cap, turnover
    3. Detect suspension days: trading days in dim_date with no ods_daily row
       → fill with previous close, vol=0, amount=0, is_suspended=1
    """
    if ts_codes is None:
        con.execute("DELETE FROM dwd_daily_quote")
        ts_codes = [row[0] for row in con.execute(
            "SELECT ts_code FROM dim_stock"
        ).fetchall()]
    else:
        for ts_code in ts_codes:
            con.execute("DELETE FROM dwd_daily_quote WHERE ts_code = ?", [ts_code])

    for ts_code in ts_codes:
        # Get latest_adj_factor (from most recent trade_date in ods_daily)
        row = con.execute(
            "SELECT adj_factor FROM ods_daily WHERE ts_code = ? ORDER BY trade_date DESC LIMIT 1",
            [ts_code],
        ).fetchone()
        if row is None:
            # Stock has no daily price data in ODS — skip
            continue
        latest_adj_factor = row[0]

        # Step 1: INSERT OR REPLACE 前复权 prices + basic data
        con.execute(
            """INSERT OR REPLACE INTO dwd_daily_quote
                (ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq,
                 vol, amount, pct_chg, total_mv, pe_ttm, turnover_rate, volume_ratio, is_suspended)
            SELECT d.ts_code, d.trade_date,
                d.open  * d.adj_factor / ?,
                d.high  * d.adj_factor / ?,
                d.low   * d.adj_factor / ?,
                d.close * d.adj_factor / ?,
                d.vol, d.amount, d.pct_chg,
                b.total_mv, b.pe_ttm, b.turnover_rate, b.volume_ratio,
                0
            FROM ods_daily d
            LEFT JOIN ods_daily_basic b
                ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date
            WHERE d.ts_code = ?""",
            [latest_adj_factor, latest_adj_factor, latest_adj_factor, latest_adj_factor,
             ts_code],
        )

        # Step 2: Detect and fill suspension days
        con.execute(
            """INSERT OR REPLACE INTO dwd_daily_quote
                (ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq,
                 vol, amount, pct_chg, total_mv, pe_ttm, turnover_rate, volume_ratio, is_suspended)
            SELECT ?, cal.trade_date,
                prev.close_qfq, prev.close_qfq, prev.close_qfq, prev.close_qfq,
                0, 0, 0,
                prev.total_mv, prev.pe_ttm, prev.turnover_rate, prev.volume_ratio,
                1
            FROM dim_date cal
            LEFT JOIN LATERAL (
                SELECT close_qfq, total_mv, pe_ttm, turnover_rate, volume_ratio
                FROM dwd_daily_quote
                WHERE ts_code = ? AND trade_date < cal.trade_date
                ORDER BY trade_date DESC LIMIT 1
            ) prev ON TRUE
            WHERE cal.is_trade_day = 1
              AND NOT EXISTS (
                  SELECT 1 FROM dwd_daily_quote q
                  WHERE q.ts_code = ? AND q.trade_date = cal.trade_date
              )
              AND cal.trade_date <= (
                  SELECT MAX(trade_date) FROM dim_date
              )
              AND prev.close_qfq IS NOT NULL""",
            [ts_code, ts_code, ts_code],
        )

    return con.execute("SELECT COUNT(*) FROM dwd_daily_quote").fetchone()[0]


def build_dwd_weekly_quote(con, ts_codes=None) -> int:
    """Aggregate dwd_daily_quote into weekly bars.

    Rules:
    - Week end date = the last trading day of each week (dim_date.is_week_end=1)
    - open_qfq = first day's open, close_qfq = last day's close
    - high_qfq = MAX, low_qfq = MIN
    - vol / amount normalized to 5-day equivalent: SUM(vol) / active_days * 5
    - pct_chg = SUM(pct_chg) (log-return accumulation, NOT price ratio)
    - total_mv, pe_ttm, turnover_rate, volume_ratio = last day's values
    - Only is_suspended=0 rows counted
    - Exclude weeks with active_days < 3
    """
    if ts_codes is None:
        con.execute("DELETE FROM dwd_weekly_quote")
        ts_code_filter = ""
        params = []
    else:
        placeholders = ",".join(["?" for _ in ts_codes])
        con.execute(
            f"DELETE FROM dwd_weekly_quote WHERE ts_code IN ({placeholders})",
            ts_codes,
        )
        ts_code_filter = f"AND q.ts_code IN ({placeholders})"
        params = ts_codes

    con.execute(
        f"""INSERT OR REPLACE INTO dwd_weekly_quote
            (ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq,
             vol, amount, pct_chg, total_mv, pe_ttm, turnover_rate, volume_ratio,
             active_days)
        WITH week_ends AS (
            SELECT year, week_of_year, trade_date AS week_end_date
            FROM dim_date
            WHERE is_trade_day = 1 AND is_week_end = 1
        ),
        daily_with_week AS (
            SELECT q.*, d.year, d.week_of_year
            FROM dwd_daily_quote q
            JOIN dim_date d ON q.trade_date = d.trade_date
            WHERE q.is_suspended = 0
              {ts_code_filter}
        ),
        daily_with_week_end AS (
            SELECT d.*, w.week_end_date
            FROM daily_with_week d
            JOIN week_ends w
                ON d.year = w.year AND d.week_of_year = w.week_of_year
        )
        SELECT
            ts_code,
            week_end_date,
            arg_min(open_qfq, trade_date)  AS open_qfq,
            MAX(high_qfq)                   AS high_qfq,
            MIN(low_qfq)                    AS low_qfq,
            arg_max(close_qfq, trade_date)  AS close_qfq,
            SUM(vol) / COUNT(*) * 5         AS vol,
            SUM(amount) / COUNT(*) * 5      AS amount,
            SUM(pct_chg)                    AS pct_chg,
            arg_max(total_mv, trade_date)      AS total_mv,
            arg_max(pe_ttm, trade_date)        AS pe_ttm,
            arg_max(turnover_rate, trade_date) AS turnover_rate,
            arg_max(volume_ratio, trade_date)  AS volume_ratio,
            COUNT(*)                        AS active_days
        FROM daily_with_week_end
        GROUP BY ts_code, week_end_date
        HAVING COUNT(*) >= 3""",
        params,
    )

    return con.execute("SELECT COUNT(*) FROM dwd_weekly_quote").fetchone()[0]


def build_dwd_daily_moneyflow(con, ts_codes=None) -> int:
    """Map ods_moneyflow → dwd_daily_moneyflow.

    - net_mf_vol, net_mf_amount: direct copy
    - buy_lg_vol, sell_lg_vol, buy_elg_vol, sell_elg_vol: direct copy
    - total_vol = buy_sm_vol + buy_md_vol + buy_lg_vol + buy_elg_vol
      (buy-side sum, used as DDX denominator)
    """
    if ts_codes is None:
        con.execute("DELETE FROM dwd_daily_moneyflow")
        ts_code_filter = ""
        params = []
    else:
        placeholders = ",".join(["?" for _ in ts_codes])
        con.execute(
            f"DELETE FROM dwd_daily_moneyflow WHERE ts_code IN ({placeholders})",
            ts_codes,
        )
        ts_code_filter = f"WHERE m.ts_code IN ({placeholders})"
        params = ts_codes

    con.execute(
        f"""INSERT OR REPLACE INTO dwd_daily_moneyflow
            (ts_code, trade_date, net_mf_vol, net_mf_amount,
             buy_lg_vol, sell_lg_vol, buy_elg_vol, sell_elg_vol, total_vol)
        SELECT
            ts_code,
            trade_date,
            net_mf_vol,
            net_mf_amount,
            buy_lg_vol,
            sell_lg_vol,
            buy_elg_vol,
            sell_elg_vol,
            COALESCE(buy_sm_vol, 0)
                + COALESCE(buy_md_vol, 0)
                + COALESCE(buy_lg_vol, 0)
                + COALESCE(buy_elg_vol, 0) AS total_vol
        FROM ods_moneyflow m
        {ts_code_filter}""",
        params,
    )

    return con.execute("SELECT COUNT(*) FROM dwd_daily_moneyflow").fetchone()[0]
