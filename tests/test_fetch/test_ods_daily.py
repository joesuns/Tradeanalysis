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
