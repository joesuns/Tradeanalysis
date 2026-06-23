"""Tests for backend/etl/orchestrator.py — auto-fetch strategy selection."""

import logging

import duckdb
from datetime import date, timedelta

from backend.etl.orchestrator import (
    _count_trading_days,
    _choose_fetch_strategy,
    _init_calc_progress,
    _report_calc_progress,
)


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


def test_week_end_bars_windowed_not_full_history():
    """全历史 week-end 够但 warmup 窗口内不足 → 仍应 weekly_fetch。"""
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
    con.execute("INSERT INTO dim_stock VALUES ('STALE.SZ', '20200101', NULL)")

    calc_weeks = _seq_week_end_dates(130, start=(2023, 1, 6))
    calc_date = calc_weeks[-1]
    stale_weeks = _seq_week_end_dates(150, start=(2015, 1, 9))
    for td in calc_weeks:
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
    for td in stale_weeks:
        if td < calc_weeks[0]:
            con.execute("INSERT OR IGNORE INTO dim_date VALUES (?, 1, 1)", [td])
            con.execute("INSERT INTO dwd_weekly_quote VALUES ('STALE.SZ', ?)", [td])
    for td in _seq_trade_dates(260, start=(2025, 1, 1)):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('STALE.SZ', ?)", [td])

    result = check_data_completeness(
        con, ["STALE.SZ"], calc_date=calc_date, min_daily_rows=250)
    assert "STALE.SZ" in result["ok"]
    assert "STALE.SZ" in result["weekly_fetch"]
    wf = result["weekly_fetch"]["STALE.SZ"]
    assert wf["week_end_bars"] < WEEKLY_WARMUP_WEEKS
    assert wf["week_end_bars"] == 0
    assert wf["available_week_ends"] >= WEEKLY_WARMUP_WEEKS

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
    from tests.test_etl.helpers import patch_run_calc_dim_deps

    patch_run_calc_dim_deps(monkeypatch)

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
    con.execute("CREATE TABLE dwd_weekly_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_moneyflow (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE ods_moneyflow (ts_code TEXT, trade_date TEXT)")
    con.execute("INSERT INTO ods_daily VALUES ('A.SZ', '20260605')")
    con.execute("INSERT INTO dwd_daily_quote VALUES ('A.SZ', '20260604')")

    stale = find_stale_dwd_codes(con, ["A.SZ"], "20260605")
    assert stale == ["A.SZ"]

    con.close()


def test_find_stale_dwd_codes_detects_moneyflow_behind_daily():
    """Daily DWD fresh but moneyflow behind → still stale."""
    import duckdb
    from backend.etl.orchestrator import find_stale_dwd_codes

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE ods_moneyflow (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_weekly_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_moneyflow (ts_code TEXT, trade_date TEXT)")
    con.execute("INSERT INTO ods_daily VALUES ('A.SZ', '20260605')")
    con.execute("INSERT INTO ods_moneyflow VALUES ('A.SZ', '20260605')")
    con.execute("INSERT INTO dwd_daily_quote VALUES ('A.SZ', '20260605')")
    con.execute("INSERT INTO dwd_weekly_quote VALUES ('A.SZ', '20260605')")
    con.execute("INSERT INTO dwd_daily_moneyflow VALUES ('A.SZ', '20260604')")

    stale = find_stale_dwd_codes(con, ["A.SZ"], "20260605")
    assert stale == ["A.SZ"]

    con.close()


def test_find_stale_dwd_codes_detects_weekly_behind_daily():
    """Daily DWD fresh but weekly behind → still stale."""
    import duckdb
    from backend.etl.orchestrator import find_stale_dwd_codes

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_weekly_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_moneyflow (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE ods_moneyflow (ts_code TEXT, trade_date TEXT)")
    con.execute("INSERT INTO ods_daily VALUES ('A.SZ', '20260605')")
    con.execute("INSERT INTO dwd_daily_quote VALUES ('A.SZ', '20260605')")
    con.execute("INSERT INTO dwd_weekly_quote VALUES ('A.SZ', '20260604')")

    stale = find_stale_dwd_codes(con, ["A.SZ"], "20260605")
    assert stale == ["A.SZ"]

    con.close()


def test_find_stale_dwd_codes_skips_moneyflow_when_no_ods_moneyflow():
    """No ODS moneyflow on date → do not flag stale for missing DWD moneyflow."""
    import duckdb
    from backend.etl.orchestrator import find_stale_dwd_codes

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_weekly_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_moneyflow (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE ods_moneyflow (ts_code TEXT, trade_date TEXT)")
    con.execute("INSERT INTO ods_daily VALUES ('BJ.BJ', '20260605')")
    con.execute("INSERT INTO dwd_daily_quote VALUES ('BJ.BJ', '20260605')")
    con.execute("INSERT INTO dwd_weekly_quote VALUES ('BJ.BJ', '20260605')")

    stale = find_stale_dwd_codes(con, ["BJ.BJ"], "20260605")
    assert stale == []

    con.close()


# ── P2: multiprocessing worker resolution ──


def test_resolve_calc_workers_env_override(monkeypatch):
    monkeypatch.setenv("CALC_WORKERS", "4")
    from backend.etl.orchestrator import resolve_calc_workers
    assert resolve_calc_workers() == 4


def test_resolve_calc_workers_env_minimum_one(monkeypatch):
    monkeypatch.setenv("CALC_WORKERS", "0")
    from backend.etl.orchestrator import resolve_calc_workers
    assert resolve_calc_workers() == 1


def test_resolve_calc_workers_default_capped(monkeypatch):
    import multiprocessing
    monkeypatch.delenv("CALC_WORKERS", raising=False)
    from backend.etl.orchestrator import resolve_calc_workers
    expected = max(1, min(multiprocessing.cpu_count() - 1, 8))
    assert resolve_calc_workers() == expected


def test_resolve_calc_workers_invalid_falls_back(monkeypatch):
    """非法 CALC_WORKERS（非数字）→ 回退默认值，不崩溃。"""
    import multiprocessing
    monkeypatch.setenv("CALC_WORKERS", "abc")
    from backend.etl.orchestrator import resolve_calc_workers
    expected = max(1, min(multiprocessing.cpu_count() - 1, 8))
    assert resolve_calc_workers() == expected


def test_should_skip_calc_idempotent():
    import json
    import duckdb
    from backend.db.schema import create_all_tables
    from backend.etl.orchestrator import _should_skip_calc_idempotent

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    comp = json.dumps({"calc_date": "20260602", "stocks": 5388})
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('1', 'calc_dws', 't0', 't1', 'success', 1, '', ?)""",
        [comp],
    )
    assert _should_skip_calc_idempotent(con, "20260602", False, False, True) is True
    assert _should_skip_calc_idempotent(con, "20260602", False, True, True) is True
    assert _should_skip_calc_idempotent(con, "20260602", True, False, True) is False
    assert _should_skip_calc_idempotent(con, "20260603", False, False, True) is False
    con.close()


def test_should_skip_calc_idempotent_force_requires_prior_etl():
    import duckdb
    from backend.db.schema import create_all_tables
    from backend.etl.orchestrator import _should_skip_calc_idempotent

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    assert _should_skip_calc_idempotent(con, "20260602", False, True, True) is False
    con.close()


def test_run_calc_force_same_day_skip(monkeypatch):
    """--force on unchanged data should short-circuit like idempotent skip."""
    import json
    import duckdb
    from backend.db.schema import create_all_tables
    from backend.etl.orchestrator import run_calc

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    comp = json.dumps({
        "calc_date": "20260602",
        "stocks": 5388,
        "ods_max": "20260602",
    })
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('1', 'calc_dws', 't0', 't1', 'success', 1, '', ?)""",
        [comp],
    )
    called = {"n": 0}

    def fail_chunk(*a, **k):
        called["n"] += 1
        return 0

    monkeypatch.setattr("backend.etl.orchestrator._calc_stock_chunk", fail_chunk)
    run_calc(con, ts_codes=None, calc_date="20260602", auto_fetch=False, force=True)
    assert called["n"] == 0
    row = con.execute(
        "SELECT data_completeness FROM ods_etl_log WHERE id != '1' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert json.loads(row[0])["force_same_day_skip"] is True
    con.close()


def test_run_calc_idempotent_skip_without_subset(monkeypatch):
    """Full-market run_calc must hit idempotent gate before resolving active codes."""
    import json
    import duckdb
    from backend.db.schema import create_all_tables
    from backend.etl.orchestrator import run_calc

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    comp = json.dumps({"calc_date": "20260602", "stocks": 5388})
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('1', 'calc_dws', 't0', 't1', 'success', 1, '', ?)""",
        [comp],
    )
    called = {"n": 0, "active_codes": 0}

    def fail_chunk(*a, **k):
        called["n"] += 1
        return 0

    def boom_active_codes(*a, **k):
        called["active_codes"] += 1
        raise AssertionError("get_all_active_codes should not run on idempotent skip")

    monkeypatch.setattr("backend.etl.orchestrator._calc_stock_chunk", fail_chunk)
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", boom_active_codes)
    run_calc(con, ts_codes=None, calc_date="20260602", auto_fetch=False)
    assert called["n"] == 0
    assert called["active_codes"] == 0
    row = con.execute(
        "SELECT data_completeness FROM ods_etl_log WHERE id != '1' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert json.loads(row[0])["idempotent_skip"] is True
    con.close()


def test_should_skip_calc_idempotent_false_when_spec_stale():
    import duckdb
    from backend.db.schema import create_all_tables
    from backend.etl.orchestrator import _should_skip_calc_idempotent

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('1', 'calc_dws', 't0', 't1', 'success', 1, '',
                   '{"calc_date":"20260602"}')"""
    )
    con.execute(
        "INSERT INTO dws_calc_state "
        "(ts_code, freq, indicator, last_trade_date, history_fp, spec_version, updated_calc_date) "
        "VALUES ('000001.SZ', 'daily', 'ma', '20260602', 'abc', 'v1', '20260602')"
    )
    assert _should_skip_calc_idempotent(
        con, "20260602", user_subset=False, force=False, skip_stale_fetch=True,
    ) is False
    con.close()


def test_run_calc_logs_batch_chunk_split(monkeypatch):
    """Successful calc_dws logs batch_only/chunk_stocks/ods_max in data_completeness."""
    import json
    import duckdb
    from backend.db.schema import create_all_tables
    from backend.etl.orchestrator import run_calc

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
        "VALUES ('A.SZ', '20260605', 1,1,1,1,1,1)"
    )
    etl_end_kwargs = {}

    def capture_end(con, lid, step, t0, status, **kwargs):
        etl_end_kwargs.update(kwargs)

    monkeypatch.setattr("backend.etl.orchestrator._calc_stock_chunk", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.orchestrator._calc_full_work_chunk", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.orchestrator.resolve_calc_workers", lambda: 1)
    monkeypatch.setattr(
        "backend.fetch.ods_daily.get_all_active_codes",
        lambda con: ["A.SZ", "B.SZ", "C.SZ"],
    )
    monkeypatch.setattr(
        "backend.etl.orchestrator.check_data_completeness",
        lambda *a, **k: {"ok": ["A.SZ", "B.SZ", "C.SZ"], "missing": {}, "weekly_fetch": {}},
    )
    monkeypatch.setattr("backend.etl.orchestrator._filter_delisted", lambda *a, **k: (a[1], {}))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start", lambda *a: ("lid", 0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", capture_end)
    monkeypatch.setattr("backend.etl.orchestrator.run_checkpoint", lambda *a: None)
    monkeypatch.setattr(
        "backend.etl.calc_batch_append.run_batch_append_phase",
        lambda con, codes, calc_date, force=False, preflight_ctx=None, indicator_filter=None: {
            "chunk_codes": ["C.SZ"],
            "completed_keys": set(),
            "agg_by_key": {},
            "stock_modes": {
                "C.SZ": {
                    ("macd", "weekly"): ("FULL", []),
                    ("kpattern", "weekly"): ("FULL", []),
                },
            },
            "full_items": [
                ("C.SZ", ("macd", "weekly")),
                ("C.SZ", ("kpattern", "weekly")),
            ],
            "chunk_work_items": 2,
        },
    )

    run_calc(con, ts_codes=None, calc_date="20260605", auto_fetch=False)

    comp = etl_end_kwargs["data_completeness"]
    assert comp["calc_date"] == "20260605"
    assert comp["stocks"] == 3
    assert comp["ods_max"] == "20260605"
    assert comp["batch_only"] == 2
    assert comp["chunk_stocks"] == 1
    assert comp["chunk_work_items"] == 2
    con.close()


def test_run_calc_uses_thread_pool(monkeypatch):
    """run_calc should dispatch chunks via ThreadPoolExecutor (not multiprocessing)."""
    import duckdb
    from backend.etl.orchestrator import run_calc
    from tests.test_etl.helpers import patch_run_calc_dim_deps

    patch_run_calc_dim_deps(monkeypatch)

    con = duckdb.connect(":memory:")
    calls = []

    def fake_chunk(chunk, calc_date, incremental, batch_ctx=None):
        calls.append((tuple(chunk), calc_date, incremental, batch_ctx))
        return 0

    monkeypatch.setattr("backend.etl.orchestrator._calc_stock_chunk", fake_chunk)
    monkeypatch.setattr("backend.etl.orchestrator.resolve_calc_workers", lambda: 2)
    monkeypatch.setattr(
        "backend.etl.orchestrator.check_data_completeness",
        lambda *a, **k: {"ok": ["A.SZ", "B.SZ"], "missing": {}, "weekly_fetch": {}},
    )
    monkeypatch.setattr("backend.etl.orchestrator._filter_delisted", lambda *a, **k: (a[1], {}))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start", lambda *a: (1, 0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.orchestrator.run_checkpoint", lambda *a: None)

    run_calc(con, ts_codes=["A.SZ", "B.SZ"], calc_date="20260605", auto_fetch=False)

    # 2 workers, 2 stocks → chunk_size=1 → 2 个单股 chunk
    assert len(calls) == 2
    assert {c[0] for c in calls} == {("A.SZ",), ("B.SZ",)}
    assert all(c[1] == "20260605" for c in calls)
    con.close()


def _count_progress_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("progress calc.stocks:")
            and "%" in r.getMessage()]


def test_calc_progress_throttled_at_5pct(caplog):
    """100 stocks → step=5 → ~20 progress lines, last at 100%."""
    _init_calc_progress(100)
    with caplog.at_level(logging.INFO):
        for _ in range(100):
            _report_calc_progress()

    lines = _count_progress_lines(caplog)
    # step = 100 // 20 = 5 → fires at 5,10,...,100 → 20 lines
    assert len(lines) == 20, f"expected 20 throttled lines, got {len(lines)}"
    last = lines[-1]
    assert "100/100 (100%)" in last
    assert "stocks/s" in last and "ETA" in last


def test_calc_progress_always_logs_final_tick(caplog):
    """Non-multiple-of-step totals still emit a final 100% line."""
    _init_calc_progress(7)  # step = max(1, 7//20) = 1 → every tick logs
    with caplog.at_level(logging.INFO):
        for _ in range(7):
            _report_calc_progress()

    lines = _count_progress_lines(caplog)
    assert lines, "expected at least one progress line"
    assert "7/7 (100%)" in lines[-1]


def test_calc_progress_thread_safe_total(caplog):
    """Concurrent ticks from multiple threads count to the true global total."""
    import threading as _t

    _init_calc_progress(200)
    with caplog.at_level(logging.INFO):
        def worker():
            for _ in range(50):
                _report_calc_progress()

        threads = [_t.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

    lines = _count_progress_lines(caplog)
    assert lines, "expected progress lines"
    # 4 threads × 50 ticks = 200 = total → final line must read 200/200
    assert "200/200 (100%)" in lines[-1]


def test_count_week_end_bars_ignores_dwd_without_dim_stock():
    """dwd_weekly 有数据但 dim_stock 无行 → 不得计入 week_end_bars。"""
    from backend.etl.orchestrator import _count_week_end_bars_batch

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dwd_weekly_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    week_ends = _seq_week_end_dates(130, start=(2020, 1, 3))
    calc_date = week_ends[-1]
    for td in week_ends:
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
    for td in week_ends[-50:]:
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('ORPHAN.SZ', ?)", [td])

    counts = _count_week_end_bars_batch(con, ["ORPHAN.SZ"], calc_date)
    assert counts["ORPHAN.SZ"] == 0

    con.close()


def test_calc_stock_chunk_reuses_batch_ctx_without_reload(monkeypatch):
    """When batch_ctx is provided, chunk must not call load_calc_state_batch / batch_load_*."""
    import importlib

    import pandas as pd

    import backend.config as cfg
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS
    from backend.etl.orchestrator import _calc_stock_chunk

    calls = []

    def spy_load_state(*args, **kwargs):
        calls.append("state")
        return {}

    def spy_quote_tails(*args, **kwargs):
        calls.append("quote")
        return {}

    def spy_dde_tails(*args, **kwargs):
        calls.append("dde")
        return {}

    monkeypatch.setenv("CALC_FAST_SKIP", "1")
    monkeypatch.setenv("CALC_APPEND", "1")
    importlib.reload(cfg)

    monkeypatch.setattr("backend.etl.calc_state.load_calc_state_batch", spy_load_state)
    monkeypatch.setattr("backend.etl.calc_fast_skip.batch_load_quote_tails", spy_quote_tails)
    monkeypatch.setattr("backend.etl.calc_fast_skip.batch_load_dde_tails", spy_dde_tails)
    real_connect = duckdb.connect
    monkeypatch.setattr("duckdb.connect", lambda path, **kw: real_connect(":memory:"))
    monkeypatch.setattr("backend.etl.orchestrator.resolve_recalc_start", lambda *a, **k: None)

    def fake_preflight(ts_code, state_map, daily_q, weekly_q, daily_dde, weekly_dde, **kwargs):
        modes = {(s[0], s[1]): ("SKIP", []) for s in CALC_ROUTE_SPECS}
        return modes, {}

    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.preflight_stock_modes_with_fps", fake_preflight,
    )
    monkeypatch.setattr("backend.etl.orchestrator._write_skip_log_batch", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.orchestrator._count_calc_rows", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.calc_state.upsert_calc_state_batch", lambda *a, **k: 0)

    minimal_df = pd.DataFrame({"trade_date": ["20260608"], "close_qfq": [10.0]})
    batch_ctx = {
        "state_map": {},
        "daily_tails": {"A.SZ": minimal_df},
        "weekly_tails": {},
        "dde_daily": {},
        "dde_weekly": {},
        "completed_keys": set(),
    }

    _calc_stock_chunk(["A.SZ"], "20260609", incremental=True, batch_ctx=batch_ctx)

    assert "state" not in calls
    assert "quote" not in calls
    assert "dde" not in calls


def test_calc_stock_chunk_passes_dwd_fp_cache_to_preflight(monkeypatch):
    """Chunk fallthrough preflight must wire DWD fingerprint gate (code review M2)."""
    import importlib

    import pandas as pd

    import backend.config as cfg
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS
    from backend.etl.orchestrator import _calc_stock_chunk

    preflight_kwargs = []

    def fake_build_cache(con, codes, calc_date):
        return {("__probe__", "daily"): {"recalc_start": calc_date}}

    def fake_preflight(ts_code, state_map, daily_q, weekly_q, daily_dde, weekly_dde, **kwargs):
        preflight_kwargs.append(kwargs)
        modes = {(s[0], s[1]): ("SKIP", []) for s in CALC_ROUTE_SPECS}
        return modes, {}

    monkeypatch.setenv("CALC_FAST_SKIP", "1")
    monkeypatch.setenv("CALC_APPEND", "1")
    importlib.reload(cfg)

    monkeypatch.setattr(
        "backend.etl.calc_dwd_fp_gate.build_dwd_fp_cache", fake_build_cache,
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.load_calc_state_batch", lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_quote_tails",
        lambda *a, **k: {"A.SZ": pd.DataFrame({"trade_date": ["20260608"], "close_qfq": [10.0]})},
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_dde_tails", lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.preflight_stock_modes_with_fps", fake_preflight,
    )
    monkeypatch.setattr("backend.etl.orchestrator.resolve_recalc_start", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.orchestrator._write_skip_log_batch", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.orchestrator._count_calc_rows", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.calc_state.upsert_calc_state_batch", lambda *a, **k: 0)

    real_connect = __import__("duckdb").connect
    monkeypatch.setattr("duckdb.connect", lambda path, **kw: real_connect(":memory:"))

    _calc_stock_chunk(["A.SZ"], "20260609", incremental=True, batch_ctx=None)

    assert len(preflight_kwargs) == 1
    assert preflight_kwargs[0]["dwd_fp_cache"] == {("__probe__", "daily"): {"recalc_start": "20260609"}}
    assert preflight_kwargs[0]["con"] is not None


def test_write_skip_log_batch_summary_when_not_verbose():
    """verbose=False + >100 fingerprint_match → single __batch__ summary row."""
    from backend.db.schema import create_all_tables
    from backend.etl.base import SkipReason
    from backend.etl.orchestrator import _write_skip_log_batch

    con = duckdb.connect(":memory:")
    create_all_tables(con)

    classified = {
        SkipReason.FINGERPRINT_MATCH: [
            (f"{i:06d}.SZ", "batch_append: preflight skip") for i in range(101)
        ],
    }
    _write_skip_log_batch(con, "20260609", "macd", "daily", classified, verbose=False)

    rows = con.execute(
        "SELECT ts_code, detail FROM ods_calc_skip_log WHERE calc_date = '20260609'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "__batch__"
    assert rows[0][1] == "batch_skip=101"
    con.close()


def test_write_skip_log_batch_verbose_writes_per_stock():
    """verbose=True (default) writes one row per skipped stock."""
    from backend.db.schema import create_all_tables
    from backend.etl.base import SkipReason
    from backend.etl.orchestrator import _write_skip_log_batch

    con = duckdb.connect(":memory:")
    create_all_tables(con)

    classified = {
        SkipReason.FINGERPRINT_MATCH: [
            (f"{i:06d}.SZ", "detail") for i in range(101)
        ],
    }
    _write_skip_log_batch(con, "20260609", "macd", "daily", classified)

    n = con.execute(
        "SELECT COUNT(*) FROM ods_calc_skip_log WHERE calc_date = '20260609'"
    ).fetchone()[0]
    assert n == 101
    con.close()


def test_count_week_end_bars_respects_list_date_floor():
    """week_end 计数窗口下限 = max(weekly_start, list_date)。"""
    from backend.etl.orchestrator import _count_week_end_bars_batch

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dwd_weekly_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    week_ends = _seq_week_end_dates(130, start=(2020, 1, 3))
    calc_date = week_ends[-1]
    list_date = week_ends[-30]  # only last 30 week-ends are post-IPO
    con.execute("INSERT INTO dim_stock VALUES ('IPO2.SZ', ?, NULL)", [list_date])
    for td in week_ends:
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('IPO2.SZ', ?)", [td])

    counts = _count_week_end_bars_batch(con, ["IPO2.SZ"], calc_date)
    assert counts["IPO2.SZ"] == 30

    con.close()


def test_run_calc_refreshes_state_after_stale_dwd_rebuild(temp_db, monkeypatch):
    """G3 stale DWD rebuild 后应 refresh affected codes。"""
    from backend.etl import orchestrator as orch
    from tests.test_etl.helpers import patch_run_calc_dim_deps

    patch_run_calc_dim_deps(monkeypatch)

    refresh_calls = []

    monkeypatch.setattr(orch, "_should_skip_calc_idempotent", lambda *a, **k: False)
    monkeypatch.setattr(orch, "_filter_delisted", lambda c, codes, d: (codes, {}))
    monkeypatch.setattr(orch, "check_data_completeness", lambda con, codes, calc_date=None: {
        "ok": ["A.SZ"],
        "missing": {},
        "weekly_fetch": {},
    })
    monkeypatch.setattr(orch, "find_stale_ods_codes", lambda *a, **k: [])
    monkeypatch.setattr(orch, "find_stale_dwd_codes", lambda con, codes, calc_date: ["A.SZ"])

    def fake_rebuild(con, codes, trade_date):
        return {"daily_quote": 1, "weekly_quote": 0, "moneyflow": 0}

    monkeypatch.setattr(orch, "rebuild_dwd_for_stale", fake_rebuild)
    def fake_refresh(con, codes, calc_date, dwd_result, return_artifacts=False):
        refresh_calls.append((list(codes), calc_date, dwd_result))
        summary = {"records_written": 1}
        if return_artifacts:
            return summary, None
        return summary

    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.maybe_refresh_state_after_dwd_rebuild",
        fake_refresh,
    )
    monkeypatch.setattr(
        "backend.etl.calc_batch_append.run_batch_append_phase",
        lambda *a, **k: {"chunk_codes": [], "completed_keys": set(), "agg_by_key": {}},
    )
    monkeypatch.setattr(orch, "resolve_calc_workers", lambda: 1)
    monkeypatch.setattr(orch, "_calc_stock_chunk", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start", lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)
    monkeypatch.setattr(orch, "run_checkpoint", lambda *a: None)

    con = temp_db
    orch.run_calc(
        con, ts_codes=["A.SZ"], auto_fetch=True, calc_date="20260610",
        skip_stale_fetch=False,
    )
    assert len(refresh_calls) == 1
    assert refresh_calls[0][0] == ["A.SZ"]


def test_merge_preflight_after_dwd_rebuild_preserves_base_ctx(monkeypatch):
    """Patch refresh must merge into existing ctx, not discard it."""
    from backend.etl.calc_preflight_context import CalcPreflightContext
    from backend.etl.orchestrator import _merge_preflight_after_dwd_rebuild

    base = CalcPreflightContext(
        calc_date="20260612",
        source="refresh_state",
        stale_codes=["KEEP.SZ"],
        state_map={},
        daily_tails={"KEEP.SZ": "keep_tail"},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={"KEEP.SZ": {("macd", "daily"): ("SKIP", [])}},
        fp_cache_by_stock={"KEEP.SZ": {("macd", "daily"): "fp_keep"}},
    )
    patch_bundle = {
        "daily_tails": {"PATCH.SZ": "patch_tail"},
        "weekly_tails": {},
        "dde_daily": {},
        "dde_weekly": {},
        "stock_modes": {"PATCH.SZ": {("macd", "daily"): ("APPEND", ["20260612"])}},
        "fp_cache_by_stock": {"PATCH.SZ": {("macd", "daily"): "fp_patch"}},
        "state_map": {},
    }

    def fake_refresh(con, codes, calc_date, dwd_result, return_artifacts=False):
        if return_artifacts:
            return {"stocks": 1, "records_written": 0}, patch_bundle
        return {"stocks": 1, "records_written": 0}

    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.maybe_refresh_state_after_dwd_rebuild",
        fake_refresh,
    )
    merged = _merge_preflight_after_dwd_rebuild(
        None, ["PATCH.SZ"], "20260612", {"daily_quote": 1}, base,
    )
    assert merged.daily_tails["KEEP.SZ"] == "keep_tail"
    assert merged.daily_tails["PATCH.SZ"] == "patch_tail"
    assert merged.stock_modes["PATCH.SZ"][("macd", "daily")][0] == "APPEND"


def test_run_calc_stale_dwd_merge_preserves_preflight_ctx(temp_db, monkeypatch):
    """G3 stale DWD path must pass merged preflight_ctx into batch_append."""
    from backend.etl import orchestrator as orch
    from backend.etl.calc_preflight_context import CalcPreflightContext
    from tests.test_etl.helpers import patch_run_calc_dim_deps

    patch_run_calc_dim_deps(monkeypatch)

    monkeypatch.setattr(
        "backend.fetch.ods_daily.get_all_active_codes", lambda con: ["A.SZ"],
    )

    base_ctx = CalcPreflightContext(
        calc_date="20260610",
        source="refresh_state",
        stale_codes=["KEEP.SZ"],
        state_map={},
        daily_tails={"KEEP.SZ": "keep"},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={"KEEP.SZ": {("macd", "daily"): ("SKIP", [])}},
        fp_cache_by_stock={"KEEP.SZ": {("macd", "daily"): "fp_k"}},
    )
    captured = {"preflight_ctx": None}

    def capture_batch(con, codes, calc_date, force=False, preflight_ctx=None, indicator_filter=None):
        captured["preflight_ctx"] = preflight_ctx
        return {
            "chunk_codes": [],
            "completed_keys": set(),
            "agg_by_key": {},
            "stock_modes": {},
            "full_items": [],
            "chunk_work_items": 0,
            "preflight_source": "refresh" if preflight_ctx else "cold",
            "tails_load_skipped": bool(preflight_ctx),
            "preflight_elapsed_sec": 0,
            "batch_full_items": 0,
            "full_by_indicator": {},
        }

    patch_bundle = {
        "daily_tails": {"A.SZ": "patch"},
        "weekly_tails": {},
        "dde_daily": {},
        "dde_weekly": {},
        "stock_modes": {"A.SZ": {("macd", "daily"): ("APPEND", ["20260610"])}},
        "fp_cache_by_stock": {"A.SZ": {("macd", "daily"): "fp_a"}},
        "state_map": {},
    }

    monkeypatch.setattr(orch, "_should_skip_calc_idempotent", lambda *a, **k: False)
    monkeypatch.setattr(orch, "_filter_delisted", lambda c, codes, d: (codes, {}))
    monkeypatch.setattr(orch, "check_data_completeness", lambda con, codes, calc_date=None: {
        "ok": ["A.SZ"],
        "missing": {},
        "weekly_fetch": {},
    })
    monkeypatch.setattr(orch, "find_stale_ods_codes", lambda *a, **k: [])
    monkeypatch.setattr(orch, "find_stale_dwd_codes", lambda con, codes, calc_date: ["A.SZ"])
    monkeypatch.setattr(
        orch, "rebuild_dwd_for_stale",
        lambda con, codes, trade_date: {"daily_quote": 1, "weekly_quote": 0, "moneyflow": 0},
    )
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.maybe_refresh_state_after_dwd_rebuild",
        lambda con, codes, calc_date, dwd_result, return_artifacts=False: (
            ({"records_written": 0}, patch_bundle) if return_artifacts
            else {"records_written": 0}
        ),
    )
    monkeypatch.setattr(
        "backend.etl.calc_batch_append.run_batch_append_phase", capture_batch,
    )
    monkeypatch.setattr(orch, "resolve_calc_workers", lambda: 1)
    monkeypatch.setattr(orch, "_calc_stock_chunk", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start", lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)
    monkeypatch.setattr(orch, "run_checkpoint", lambda *a: None)

    orch.run_calc(
        con=temp_db,
        ts_codes=None,
        auto_fetch=True,
        calc_date="20260610",
        skip_stale_fetch=False,
        preflight_ctx=base_ctx,
    )
    ctx = captured["preflight_ctx"]
    assert ctx is not None
    assert ctx.daily_tails["KEEP.SZ"] == "keep"
    assert ctx.daily_tails["A.SZ"] == "patch"
