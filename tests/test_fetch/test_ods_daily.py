"""Tests for per-stock incremental fetch in backend.fetch.ods_daily."""

import duckdb
from backend.fetch.ods_daily import (
    _get_missing_days_for_stock,
    _get_missing_ranges_per_stock,
    fetch_stocks_incremental,
)


def test_get_missing_days_for_stock():
    """per-stock detection: only filter out dates already present for THAT stock."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    # 000001.SZ has 5 days
    for d in ["20260101", "20260102", "20260103", "20260104", "20260105"]:
        con.execute("INSERT INTO ods_daily VALUES ('000001.SZ', ?)", (d,))
    # 000002.SZ has only 1 day
    con.execute("INSERT INTO ods_daily VALUES ('000002.SZ', '20260101')")

    all_days = ["20260101", "20260102", "20260103", "20260104", "20260105"]

    missing_1 = _get_missing_days_for_stock(con, "000001.SZ", all_days)
    missing_2 = _get_missing_days_for_stock(con, "000002.SZ", all_days)

    assert len(missing_1) == 0, f"000001 should have no missing days, got {missing_1}"
    assert len(missing_2) == 4, f"000002 should miss 4 days, got {len(missing_2)}"
    assert "20260102" in missing_2
    con.close()


def test_get_missing_ranges_merges_consecutive():
    """Consecutive missing days should be merged into a single range."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    # Only the last day has data
    con.execute("INSERT INTO ods_daily VALUES ('TEST.SZ', '20260105')")

    days = ["20260101", "20260102", "20260103", "20260104", "20260105"]
    ranges = _get_missing_ranges_per_stock(con, "TEST.SZ", days)

    assert len(ranges) == 1, f"Expected 1 missing range, got {len(ranges)}: {ranges}"
    assert ranges[0] == ("20260101", "20260104")
    con.close()


def test_get_missing_ranges_no_gap():
    """When data is complete, no missing ranges should be returned."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    for d in ["20260101", "20260102", "20260103"]:
        con.execute("INSERT INTO ods_daily VALUES ('FULL.SZ', ?)", (d,))

    days = ["20260101", "20260102", "20260103"]
    ranges = _get_missing_ranges_per_stock(con, "FULL.SZ", days)
    assert len(ranges) == 0
    con.close()


def test_fetch_stocks_incremental_skips_when_complete():
    """When data is already complete, only trade_cal is called — no data APIs."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    for d in ["20260101", "20260102", "20260103"]:
        con.execute("INSERT INTO ods_daily VALUES ('FULL.SZ', ?)", (d,))

    api_calls = []

    class FakeClient:
        def call(self, api, **kwargs):
            api_calls.append(api)
            if api == "trade_cal":
                return [{"cal_date": d} for d in
                        ["20260101", "20260102", "20260103"]]
            return []

    n = fetch_stocks_incremental(
        FakeClient(), con, ["FULL.SZ"], start="20260101", end="20260103"
    )
    assert n == 0, f"Should not have fetched any data, got {n}"
    # trade_cal is always called; daily/daily_basic/moneyflow should NOT be called
    data_apis = [a for a in api_calls if a != "trade_cal"]
    assert len(data_apis) == 0, f"Should not have called data APIs, got {data_apis}"
    con.close()


def test_get_missing_days_empty_trading_days():
    """Empty trading day list should return empty list."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    result = _get_missing_days_for_stock(con, "TEST.SZ", [])
    assert result == []
    con.close()


# ── _get_trading_days with ts_codes ──


def test_get_trading_days_with_ts_codes_partial_coverage():
    """Date skipped ONLY when ALL target stocks have data (per-stock check)."""
    from backend.fetch.ods_daily import _get_trading_days

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT,
            open REAL, high REAL, low REAL, close REAL, vol REAL,
            amount REAL, pct_chg REAL, adj_factor REAL)
    """)
    # Stock A: all 3 dates. Stock B: only 20260101.
    for d in ["20260101", "20260102", "20260103"]:
        con.execute(
            "INSERT INTO ods_daily VALUES ('A.SZ', ?, 10,11,9,10,100,1000,0,1)", (d,))
    con.execute(
        "INSERT INTO ods_daily VALUES ('B.SZ', '20260101', 10,11,9,10,100,1000,0,1)")

    class FakeClient:
        def call(self, api, **kwargs):
            return [{"cal_date": d} for d in ["20260101", "20260102", "20260103"]]

    # Without ts_codes: date-global — ANY stock has data → date skipped
    days_all = _get_trading_days(FakeClient(), "20260101", "20260103", con=con)
    assert len(days_all) == 0, f"date-global: all dates have SOME data, got {days_all}"

    # With ts_codes=["A.SZ"]: A has all 3 dates → all skipped
    days_a = _get_trading_days(FakeClient(), "20260101", "20260103",
                               con=con, ts_codes=["A.SZ"])
    assert len(days_a) == 0, f"A.SZ has all dates, got {days_a}"

    # With ts_codes=["B.SZ"]: B has 20260101, missing 01/02 and 01/03
    # → 20260101 is covered → skipped; 01/02 and 01/03 returned
    days_b = _get_trading_days(FakeClient(), "20260101", "20260103",
                               con=con, ts_codes=["B.SZ"])
    assert len(days_b) == 2, (
        f"B has 20260101 (covered), missing 01/02 and 01/03, got {days_b}")
    assert "20260102" in days_b
    assert "20260103" in days_b

    # With ts_codes=["A.SZ", "B.SZ"]: 20260101 both have → covered → skipped
    # 20260102 and 20260103: A has, B doesn't → not covered → returned
    days_both = _get_trading_days(FakeClient(), "20260101", "20260103",
                                  con=con, ts_codes=["A.SZ", "B.SZ"])
    assert len(days_both) == 2, (
        f"Both: only 20260101 fully covered, 01/02 and 01/03 returned, got {days_both}")
    assert "20260102" in days_both
    assert "20260103" in days_both

    con.close()


# ── _validate_ods_batch ──


def test_validate_ods_batch_all_valid():
    """Clean data should all pass validation."""
    from backend.fetch.ods_daily import _validate_ods_batch

    recs = [
        {"ts_code": "A.SZ", "trade_date": "20260101",
         "open": 10, "high": 12, "low": 9, "close": 11, "vol": 100, "amount": 1000},
        {"ts_code": "B.SZ", "trade_date": "20260101",
         "open": 20, "high": 22, "low": 19, "close": 21, "vol": 200, "amount": 2000},
    ]
    valid, invalid = _validate_ods_batch(recs, "daily")
    assert valid == 2
    assert invalid == 0


def test_validate_ods_batch_rejects_bad_ohlc():
    """high < low or missing close should be rejected."""
    from backend.fetch.ods_daily import _validate_ods_batch

    recs = [
        {"ts_code": "A.SZ", "trade_date": "20260101",
         "open": 10, "high": 12, "low": 9, "close": 11, "vol": 100, "amount": 1000},
        {"ts_code": "B.SZ", "trade_date": "20260101",
         "open": 10, "high": 8, "low": 9, "close": 11, "vol": 100,
         "amount": 1000},  # high < low
        {"ts_code": "C.SZ", "trade_date": "20260101",
         "open": 10, "high": 12, "low": 9, "close": None, "vol": 100,
         "amount": 1000},  # missing close
    ]
    valid, invalid = _validate_ods_batch(recs, "daily")
    assert valid == 1
    assert invalid == 2


def test_validate_ods_batch_missing_required_field():
    """Missing open → rejected."""
    from backend.fetch.ods_daily import _validate_ods_batch

    recs = [
        {"ts_code": "D.SZ", "trade_date": "20260101",
         "open": None, "high": 12, "low": 9, "close": 11,
         "vol": 100, "amount": 1000},  # missing open
    ]
    valid, invalid = _validate_ods_batch(recs, "daily")
    assert valid == 0
    assert invalid == 1
