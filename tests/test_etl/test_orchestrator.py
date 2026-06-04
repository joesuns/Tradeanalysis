"""Tests for backend/etl/orchestrator.py — auto-fetch strategy selection."""

import duckdb
from backend.etl.orchestrator import _count_trading_days, _choose_fetch_strategy


def test_count_trading_days_basic():
    """Count trading days between two dates in dim_date."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY,
            is_trade_day INTEGER
        )
    """)
    # Insert 5 consecutive trading days
    for i in range(1, 6):
        con.execute(
            "INSERT INTO dim_date VALUES (?, 1)",
            (f"2026010{i}",),
        )

    count = _count_trading_days(con, "20260101", "20260105")
    assert count == 5, f"Expected 5 trading days, got {count}"

    con.close()


def test_count_trading_days_skips_non_trade_days():
    """Only counts rows where is_trade_day = 1."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY,
            is_trade_day INTEGER
        )
    """)
    con.execute("INSERT INTO dim_date VALUES ('20260101', 1)")
    con.execute("INSERT INTO dim_date VALUES ('20260102', 1)")
    con.execute("INSERT INTO dim_date VALUES ('20260103', 0)")  # weekend
    con.execute("INSERT INTO dim_date VALUES ('20260104', 0)")  # weekend
    con.execute("INSERT INTO dim_date VALUES ('20260105', 1)")

    count = _count_trading_days(con, "20260101", "20260105")
    assert count == 3, f"Expected 3 trading days (excl weekends), got {count}"

    con.close()


def test_count_trading_days_empty_range():
    """Returns 0 when no trading days in range."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY,
            is_trade_day INTEGER
        )
    """)
    con.execute("INSERT INTO dim_date VALUES ('20260101', 1)")

    count = _count_trading_days(con, "20260105", "20260110")
    assert count == 0, f"Expected 0, got {count}"

    con.close()


# ── _choose_fetch_strategy ──


def test_choose_date_batched_when_stocks_exceed_tdays():
    """Date-batched when stocks > trading days: fewer API calls."""
    assert _choose_fetch_strategy(5000, 250) is True   # full market
    assert _choose_fetch_strategy(500, 10) is True     # many stocks, short range


def test_choose_stock_batched_when_tdays_exceed_or_equal():
    """Stock-batched when trading days >= stocks: more targeted."""
    assert _choose_fetch_strategy(50, 250) is False     # few stocks
    assert _choose_fetch_strategy(10, 500) is False     # very few stocks, long range
    assert _choose_fetch_strategy(250, 250) is False    # equal: stock is more precise


def test_choose_with_edge_cases():
    """Single stock or single day — boundary cases."""
    assert _choose_fetch_strategy(1, 250) is False      # 1 stock → stock-batched
    assert _choose_fetch_strategy(100, 1) is True       # 1 day, many stocks → date-batched


# ── _compute_fetch_range coverage threshold ──


def test_compute_fetch_range_rejects_95_percent_coverage():
    """19/20=95% ODS coverage — old threshold accepted this, new 100% rejects."""
    import duckdb
    from backend.etl.orchestrator import _compute_fetch_range

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_stock (ts_code TEXT PRIMARY KEY,
            list_date TEXT, delist_date TEXT)
    """)
    con.execute("INSERT INTO dim_stock VALUES ('TEST.SZ', '20240101', NULL)")
    con.execute("""
        CREATE TABLE dim_date (trade_date TEXT PRIMARY KEY, is_trade_day INTEGER)
    """)
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_quote (ts_code TEXT, trade_date TEXT)")

    # 30 trading days in dim_date
    days = [f"202606{i:02d}" for i in range(1, 31)]  # 20260601 ~ 20260630
    for d in days:
        con.execute("INSERT INTO dim_date VALUES (?, 1)", (d,))

    # ODS has 19 of the last 20 days (20260611~20260630, missing 20260630)
    for d in days[-20:-1]:  # 20260611 ~ 20260629 = 19 dates
        con.execute("INSERT INTO ods_daily VALUES ('TEST.SZ', ?)", (d,))

    start, end = _compute_fetch_range(con, "TEST.SZ", "20260630", lookback_tdays=20)
    # Coverage = 19/20 = 95%. Old: 19 >= 20*0.95=19 → skip (None,None)
    # New: 19 < 20 → NOT skip → returns actual range
    assert start is not None, (
        "95% coverage should NOT skip with 100% threshold — need actual range")
    assert end is not None

    con.close()


def test_compute_fetch_range_accepts_full_coverage():
    """100% ODS coverage IS enough to skip."""
    import duckdb
    from backend.etl.orchestrator import _compute_fetch_range

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_stock (ts_code TEXT PRIMARY KEY,
            list_date TEXT, delist_date TEXT)
    """)
    con.execute("INSERT INTO dim_stock VALUES ('FULL.SZ', '20240101', NULL)")
    con.execute("""
        CREATE TABLE dim_date (trade_date TEXT PRIMARY KEY, is_trade_day INTEGER)
    """)
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_quote (ts_code TEXT, trade_date TEXT)")

    days = [f"202606{i:02d}" for i in range(1, 31)]
    for d in days:
        con.execute("INSERT INTO dim_date VALUES (?, 1)", (d,))
    for d in days[-20:]:  # all 20 dates
        con.execute("INSERT INTO ods_daily VALUES ('FULL.SZ', ?)", (d,))

    start, end = _compute_fetch_range(con, "FULL.SZ", "20260630", lookback_tdays=20)
    assert start is None, f"100% coverage should skip, got start={start}"
    assert end is None

    con.close()
