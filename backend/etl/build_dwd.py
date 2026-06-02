"""DWD layer construction — forward-adjusted prices, weekly aggregation, moneyflow mapping."""


def build_dwd_daily_quote(con, ts_codes=None) -> int:
    """Build dwd_daily_quote: single-pass batch 前复权 for all stocks.

    Uses a window-function subquery to compute latest_adj_factor per stock
    in one pass, then joins against ods_daily + ods_daily_basic for a single
    mass INSERT. Suspension days are filled in a separate batch pass using
    LATERAL JOIN per stock (necessary because prev-close is stock-specific).
    """
    if ts_codes is None:
        con.execute("DELETE FROM dwd_daily_quote")
        code_filter = ""
        params = []
    elif len(ts_codes) == 0:
        return 0  # nothing to do
    else:
        placeholders = ",".join(["?" for _ in ts_codes])
        con.execute(f"DELETE FROM dwd_daily_quote WHERE ts_code IN ({placeholders})", ts_codes)
        code_filter = f"AND d.ts_code IN ({placeholders})"
        params = ts_codes

    # Step 1: Single-pass batch 前复权 for ALL stocks
    # latest_adj factor computed via correlated max-date subquery per stock
    con.execute(
        f"""INSERT OR REPLACE INTO dwd_daily_quote
            (ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq,
             vol, amount, pct_chg, total_mv, pe_ttm, turnover_rate, volume_ratio, is_suspended)
        SELECT d.ts_code, d.trade_date,
            d.open  * d.adj_factor / la.latest_adj,
            d.high  * d.adj_factor / la.latest_adj,
            d.low   * d.adj_factor / la.latest_adj,
            d.close * d.adj_factor / la.latest_adj,
            d.vol, d.amount, d.pct_chg,
            b.total_mv, b.pe_ttm, b.turnover_rate, b.volume_ratio,
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
        WHERE 1=1 {code_filter}""",
        params,
    )

    # Step 2: Batch suspension detection (per-stock LATERAL is unavoidable
    # for correct prev-close lookup, but at least precomputed in one pass)
    codes_to_fill = ts_codes if ts_codes else [
        r[0] for r in con.execute("SELECT ts_code FROM dim_stock").fetchall()
    ]
    for ts_code in codes_to_fill:
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
              AND cal.trade_date <= (SELECT MAX(trade_date) FROM ods_daily WHERE ts_code = ?)
              AND prev.close_qfq IS NOT NULL""",
            [ts_code, ts_code, ts_code, ts_code],
        )

    if ts_codes:
        placeholders = ",".join(["?" for _ in ts_codes])
        return con.execute(
            f"SELECT COUNT(*) FROM dwd_daily_quote WHERE ts_code IN ({placeholders})",
            ts_codes,
        ).fetchone()[0]
    else:
        return con.execute("SELECT COUNT(*) FROM dwd_daily_quote").fetchone()[0]


def build_dwd_weekly_quote(con, ts_codes=None) -> int:
    """Build rolling weekly bars: each trading day gets a week-to-date bar.

    Uses DuckDB window functions partitioned by ISO week. Each day aggregates
    all non-suspended days in the same week up to and including the current day.
    open uses FIRST_VALUE (Monday's open), close uses current day's close.
    vol/amount normalized to 5-day equivalent (SUM/active_days*5).
    """
    if ts_codes is None:
        con.execute("DELETE FROM dwd_weekly_quote")
        ts_code_filter = ""
        params = []
    elif len(ts_codes) == 0:
        return 0
    else:
        placeholders = ",".join(["?" for _ in ts_codes])
        con.execute(f"DELETE FROM dwd_weekly_quote WHERE ts_code IN ({placeholders})", ts_codes)
        ts_code_filter = f"AND d.ts_code IN ({placeholders})"
        params = ts_codes

    con.execute(
        f"""INSERT OR REPLACE INTO dwd_weekly_quote
            (ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq,
             vol, amount, pct_chg, total_mv, pe_ttm, turnover_rate, volume_ratio,
             active_days)
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
        WHERE d.is_suspended = 0 {ts_code_filter}
        WINDOW w AS (PARTITION BY d.ts_code,
                     strftime(CAST(substr(d.trade_date,1,4)||'-'||substr(d.trade_date,5,2)||'-'||substr(d.trade_date,7,2) AS DATE), '%Y-%W')
                     ORDER BY d.trade_date
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)""",
        params,
    )

    if ts_codes:
        placeholders = ",".join(["?" for _ in ts_codes])
        return con.execute(
            f"SELECT COUNT(*) FROM dwd_weekly_quote WHERE ts_code IN ({placeholders})",
            ts_codes,
        ).fetchone()[0]
    else:
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
    elif len(ts_codes) == 0:
        return 0
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

    if ts_codes:
        placeholders = ",".join(["?" for _ in ts_codes])
        return con.execute(
            f"SELECT COUNT(*) FROM dwd_daily_moneyflow WHERE ts_code IN ({placeholders})",
            ts_codes,
        ).fetchone()[0]
    else:
        return con.execute("SELECT COUNT(*) FROM dwd_daily_moneyflow").fetchone()[0]
