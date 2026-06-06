"""Tests for backend/etl/orchestrator.py — auto-fetch strategy selection."""

import duckdb
from datetime import date, timedelta

from backend.etl.orchestrator import _count_trading_days, _choose_fetch_strategy


def _seq_trade_dates(n, start=(2026, 1, 1)):
    """Return n consecutive valid YYYYMMDD strings from start."""
    y, m, d = start
    base = date(y, m, d)
    return [(base + timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def _seq_week_end_dates(n, start=(2024, 1, 5)):
    """Return n week-end YYYYMMDD strings spaced 7 calendar days apart."""
    y, m, d = start
    base = date(y, m, d)
    return [(base + timedelta(days=7 * i)).strftime("%Y%m%d") for i in range(n)]


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


def test_available_week_ends_respects_list_date():
    """available_week_ends = [list_date, end_date] 内 dim_date week-end 数。"""
    import duckdb
    from backend.etl.orchestrator import _available_week_ends_batch

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    con.execute("INSERT INTO dim_stock VALUES ('IPO.SZ', '20260101', NULL)")
    con.execute("INSERT INTO dim_stock VALUES ('OLD.SZ', '20200101', NULL)")
    for td in _seq_week_end_dates(130, start=(2020, 1, 3)):
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
    for td in _seq_week_end_dates(10, start=(2026, 1, 2)):
        con.execute("INSERT OR IGNORE INTO dim_date VALUES (?, 1, 1)", [td])

    end = "20261231"
    counts_ipo = _available_week_ends_batch(con, ["IPO.SZ"], end)
    counts_old = _available_week_ends_batch(con, ["OLD.SZ"], "20251231")
    assert counts_ipo["IPO.SZ"] == 10
    assert counts_old["OLD.SZ"] == 130

    con.close()


def test_check_data_completeness_ipo_exempt_from_weekly_gate():
    """上市不足 120 周：daily_ok 即 ok，weekly 不足不阻断 calc。"""
    import duckdb
    from backend.etl.orchestrator import check_data_completeness

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
    con.execute("INSERT INTO dim_stock VALUES ('IPO.SZ', '20260101', NULL)")
    for td in _seq_trade_dates(260):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('IPO.SZ', ?)", [td])
    for td in _seq_week_end_dates(50, start=(2026, 1, 2)):
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('IPO.SZ', ?)", [td])

    result = check_data_completeness(
        con, ["IPO.SZ"], calc_date="20261231", min_daily_rows=250)
    assert "IPO.SZ" in result["ok"]
    assert "IPO.SZ" not in result["missing"]
    assert "IPO.SZ" not in result.get("weekly_fetch", {})

    con.close()


def test_check_data_completeness_mature_weekly_fetch_bucket():
    """成熟股 week-end 不足：仍可 calc，但进 weekly_fetch 触发补历史。"""
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
    con.execute("INSERT INTO dim_stock VALUES ('OLD.SZ', '20200101', NULL)")
    for td in _seq_week_end_dates(130, start=(2020, 1, 3)):
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
    for td in _seq_trade_dates(260):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('OLD.SZ', ?)", [td])
    for td in _seq_week_end_dates(50, start=(2025, 1, 3)):
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('OLD.SZ', ?)", [td])

    result = check_data_completeness(
        con, ["OLD.SZ"], calc_date="20261231", min_daily_rows=250)
    assert "OLD.SZ" in result["ok"]
    assert "OLD.SZ" not in result["missing"]
    assert "OLD.SZ" in result["weekly_fetch"]
    wf = result["weekly_fetch"]["OLD.SZ"]
    assert wf["week_end_bars"] == 50
    assert wf["weekly_required"] == WEEKLY_WARMUP_WEEKS
    assert wf["available_week_ends"] >= WEEKLY_WARMUP_WEEKS

    con.close()


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
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    for code in ("A.SZ", "B.SZ", "C.SZ"):
        con.execute("INSERT INTO dim_stock VALUES (?, '20200101', NULL)", [code])
    # A.SZ: 260 rows (>= 250, OK) + 125 week-end bars (>= 120, OK)
    # B.SZ: 100 rows (< 250, missing)
    # C.SZ: 0 rows (not in DWD at all)
    week_dates = _seq_week_end_dates(125)
    for d in week_dates:
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [d])
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('A.SZ', ?)", [d])
    for td in _seq_trade_dates(260):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('A.SZ', ?)", (td,))
    for td in _seq_trade_dates(100):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('B.SZ', ?)", (td,))

    result = check_data_completeness(
        con, ["A.SZ", "B.SZ", "C.SZ"], calc_date="20261231", min_daily_rows=250)

    assert result["ok"] == ["A.SZ"]
    assert "B.SZ" in result["missing"]
    assert result["missing"]["B.SZ"]["dwd_rows"] == 100
    assert "C.SZ" in result["missing"]
    assert result["missing"]["C.SZ"]["dwd_rows"] == 0

    con.close()


def test_check_data_completeness_mature_low_week_ends_goes_to_weekly_fetch():
    """成熟股 week-end 不足：ok 可 calc，weekly_fetch 触发 fetch。"""
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
    for td in _seq_week_end_dates(130, start=(2020, 1, 3)):
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
    for td in _seq_trade_dates(260):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('W.SZ', ?)", [td])
    for td in _seq_week_end_dates(50, start=(2025, 1, 3)):
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('W.SZ', ?)", [td])

    result = check_data_completeness(
        con, ["W.SZ"], calc_date="20261231", min_daily_rows=250)
    assert "W.SZ" in result["ok"]
    assert "W.SZ" not in result["missing"]
    assert "W.SZ" in result["weekly_fetch"]
    assert result["weekly_fetch"]["W.SZ"]["week_end_bars"] == 50
    assert WEEKLY_WARMUP_WEEKS == 120

    con.close()


def test_classify_still_missing_includes_week_end_bars_in_detail():
    from backend.etl.orchestrator import _classify_still_missing
    from backend.etl.base import SkipReason

    missing = {
        "X.SZ": {
            "dwd_rows": 100,
            "week_end_bars": 50,
            "weekly_required": 120,
            "available_week_ends": 130,
            "min_date": "20250101",
            "max_date": "20260601",
            "reason": "daily_warmup",
        }
    }
    classified = _classify_still_missing(None, missing)
    detail = classified[SkipReason.INSUFFICIENT_ROWS][0][1]
    assert "week_end_bars=50" in detail
    assert "50/120" in detail


def test_run_calc_skips_rebuild_when_only_weekly_fetch_and_ods_full(monkeypatch):
    """weekly_fetch + ODS 已满 + to_fetch 空 → 不得 rebuild_all_dwd。"""
    import duckdb
    from backend.etl import orchestrator as orch

    con = duckdb.connect(":memory:")
    rebuild_calls = []

    def fake_rebuild(con, ts_codes=None):
        rebuild_calls.append(list(ts_codes) if ts_codes else None)
        return {"daily_quote": 0, "weekly_quote": 0, "moneyflow": 0}

    monkeypatch.setattr(orch, "rebuild_all_dwd", fake_rebuild)
    monkeypatch.setattr(orch, "_filter_delisted", lambda c, codes, d: (codes, {}))
    monkeypatch.setattr(orch, "check_data_completeness", lambda c, codes, **kw: {
        "ok": ["OLD.SZ"],
        "missing": {},
        "weekly_fetch": {"OLD.SZ": {"reason": "weekly_warmup", "week_end_bars": 50}},
    })
    monkeypatch.setattr(orch, "_compute_fetch_range", lambda *a, **k: (None, None))
    monkeypatch.setattr(orch, "find_stale_ods_codes", lambda *a, **k: [])
    monkeypatch.setattr(orch, "_calc_stock_chunk", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start", lambda *a: ("lid", 0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)
    monkeypatch.setattr(orch, "run_checkpoint", lambda *a: None)

    orch.run_calc(con, ["OLD.SZ"], calc_date="20260605", auto_fetch=True,
                  skip_stale_fetch=True)

    assert rebuild_calls == []
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
