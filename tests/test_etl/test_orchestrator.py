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


# ── weekly warmup ──


def test_resolve_weekly_warmup_start_counts_week_ends():
    """第 120 个 is_week_end=1 交易日即为 weekly warmup 起点。"""
    import duckdb
    from backend.etl.orchestrator import resolve_weekly_warmup_start, WEEKLY_WARMUP_WEEKS

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY,
            is_trade_day INTEGER,
            is_week_end INTEGER
        )
    """)
    dates = [f"2024{i:04d}" for i in range(1000, 1000 + 130)]
    for d in dates:
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [d])
    con.execute("INSERT INTO dim_date VALUES ('20241201', 1, 0)")

    start = resolve_weekly_warmup_start(con, dates[-1], WEEKLY_WARMUP_WEEKS)
    assert start == dates[130 - 120]

    con.close()


def test_compute_fetch_range_uses_weekly_warmup_when_deeper():
    """weekly 120 周起点早于 daily 250 日时，fetch 应从更早日期开始。"""
    import duckdb
    from backend.etl.orchestrator import _compute_fetch_range

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")

    tdays = []
    for i in range(600):
        td = f"2024{(i // 30) + 1:02d}{(i % 30) + 1:02d}"
        we = 1 if (i % 4 == 3) else 0
        con.execute(
            "INSERT OR REPLACE INTO dim_date VALUES (?, 1, ?)", [td, we]
        )
        tdays.append(td)
    end = tdays[-1]

    con.execute(
        "INSERT INTO dim_stock VALUES ('DEEP.SZ', '20200101', NULL)"
    )
    for td in tdays[-250:]:
        con.execute(
            "INSERT INTO ods_daily VALUES ('DEEP.SZ', ?)", [td]
        )

    start, end_out = _compute_fetch_range(con, "DEEP.SZ", end)
    assert start is not None
    assert start < tdays[-250]

    con.close()


# ── check_data_completeness batch ──


def test_check_data_completeness_batch_results():
    """Robust to both per-stock and batch implementations."""
    import duckdb
    from backend.etl.orchestrator import check_data_completeness

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT)""")
    con.execute("""
        CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT)""")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    # A.SZ: 260 rows (>= 250, OK) + 125 week-end bars (>= 120, OK)
    # B.SZ: 100 rows (< 250, missing)
    # C.SZ: 0 rows (not in DWD at all)
    week_dates = [f"2024{i:04d}" for i in range(1000, 1125)]
    for d in week_dates:
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [d])
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('A.SZ', ?)", [d])
    for i in range(260):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('A.SZ', ?)",
                    (f"2026{i//12:02d}{i%12+1:02d}",))
    for i in range(100):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('B.SZ', ?)",
                    (f"2026{i//12:02d}{i%12+1:02d}",))

    result = check_data_completeness(
        con, ["A.SZ", "B.SZ", "C.SZ"], min_daily_rows=250)

    assert result["ok"] == ["A.SZ"]
    assert "B.SZ" in result["missing"]
    assert result["missing"]["B.SZ"]["dwd_rows"] == 100
    assert "C.SZ" in result["missing"]
    assert result["missing"]["C.SZ"]["dwd_rows"] == 0

    con.close()


def test_check_data_completeness_requires_week_end_bars():
    """dwd_rows 够但 week_end_bars < 120 → missing（weekly_warmup）。"""
    import duckdb
    from backend.etl.orchestrator import check_data_completeness, WEEKLY_WARMUP_WEEKS

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dwd_daily_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_weekly_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    con.execute("INSERT INTO dim_stock VALUES ('W.SZ', '20200101', NULL)")

    for i in range(260):
        td = f"2026{i//12:02d}{i%12+1:02d}"
        con.execute("INSERT INTO dwd_daily_quote VALUES ('W.SZ', ?)", [td])
    for i in range(50):
        td = f"2025{i+1:02d}05"
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('W.SZ', ?)", [td])

    result = check_data_completeness(con, ["W.SZ"], min_daily_rows=250)
    assert "W.SZ" in result["missing"]
    assert result["missing"]["W.SZ"]["week_end_bars"] == 50
    assert result["missing"]["W.SZ"]["reason"] == "weekly_warmup"
    assert WEEKLY_WARMUP_WEEKS == 120

    con.close()


# ── stale ODS / DWD freshness ──


def test_find_stale_ods_codes_detects_missing_latest_day():
    """Warmup-OK stocks missing analysis_date in ODS are stale."""
    import duckdb
    from backend.etl.orchestrator import find_stale_ods_codes

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    con.execute("""
        CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)
    """)
    con.execute("INSERT INTO dim_stock VALUES ('A.SZ', '20200101', NULL)")
    con.execute("INSERT INTO dim_stock VALUES ('B.SZ', '20200101', NULL)")
    con.execute("INSERT INTO ods_daily VALUES ('A.SZ', '20260604')")
    con.execute("INSERT INTO ods_daily VALUES ('B.SZ', '20260605')")

    stale = find_stale_ods_codes(con, ["A.SZ", "B.SZ"], "20260605")
    assert stale == ["A.SZ"]

    con.close()


def test_find_stale_ods_codes_skips_not_yet_listed():
    """IPO after analysis_date should not be flagged stale."""
    import duckdb
    from backend.etl.orchestrator import find_stale_ods_codes

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    con.execute("INSERT INTO dim_stock VALUES ('NEW.SZ', '20260610', NULL)")

    stale = find_stale_ods_codes(con, ["NEW.SZ"], "20260605")
    assert stale == []

    con.close()


def test_find_stale_dwd_codes_detects_ods_without_dwd():
    """ODS present on date but DWD max behind → stale DWD."""
    import duckdb
    from backend.etl.orchestrator import find_stale_dwd_codes

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("INSERT INTO ods_daily VALUES ('A.SZ', '20260605')")
    con.execute("INSERT INTO dwd_daily_quote VALUES ('A.SZ', '20260604')")

    stale = find_stale_dwd_codes(con, ["A.SZ"], "20260605")
    assert stale == ["A.SZ"]

    con.close()
