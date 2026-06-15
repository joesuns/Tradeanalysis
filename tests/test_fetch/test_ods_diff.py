"""Tests for ODS row diff before INSERT."""
import duckdb
import pytest

from backend.db.schema import create_all_tables
from backend.fetch.ods_diff import (
    partition_changed_daily,
    values_equal,
)
from backend.fetch.ods_daily import _write_ods_daily_diff
from backend.fetch.fetch_result import FetchResult


def test_values_equal_float_tolerance():
    assert values_equal(1.0, 1.0 + 1e-10)
    assert not values_equal(1.0, 1.01)


def test_values_equal_duckdb_api_roundtrip_golden():
    """Real 000001.SZ 20260612: API vs DuckDB float32 storage — must be unchanged."""
    db = {
        "open": 11.0, "high": 11.25,
        "low": 10.880000114440918, "close": 11.239999771118164,
        "vol": 2032355.5, "amount": 2263043.0,
        "pct_chg": 2.7421998977661133, "adj_factor": 139.00799560546875,
    }
    api = {
        "open": 11.0, "high": 11.25, "low": 10.88, "close": 11.24,
        "vol": 2032355.46, "amount": 2263042.93057,
        "pct_chg": 2.7422, "adj_factor": 139.008,
    }
    for col in db:
        assert values_equal(api[col], db[col]), col

    assert values_equal(49841.46, 49841.4609375)  # moneyflow amount float32
    assert values_equal(float("nan"), None)  # pe_ttm missing: API NaN vs DB NULL
    assert not values_equal(1.0, 1.05)  # real adj change still detected


def test_partition_changed_daily_new_and_unchanged(db_with_schema):
    con = db_with_schema
    con.execute("""
        INSERT INTO ods_daily
        (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor)
        VALUES ('000001.SZ', '20260612', 10, 11, 9, 10.5, 100, 1000, 1.0, 1.0)
    """)
    incoming_same = [{
        "ts_code": "000001.SZ", "trade_date": "20260612",
        "open": 10, "high": 11, "low": 9, "close": 10.5,
        "vol": 100, "amount": 1000, "pct_chg": 1.0, "adj_factor": 1.0,
    }]
    changed, unchanged = partition_changed_daily(con, incoming_same)
    assert changed == []
    assert unchanged == 1

    incoming_new = [{
        "ts_code": "000002.SZ", "trade_date": "20260612",
        "open": 20, "high": 21, "low": 19, "close": 20.5,
        "vol": 200, "amount": 2000, "pct_chg": 2.0, "adj_factor": 1.0,
    }]
    changed, unchanged = partition_changed_daily(con, incoming_new)
    assert len(changed) == 1
    assert unchanged == 0


def test_write_ods_daily_diff_adj_change(db_with_schema):
    con = db_with_schema
    con.execute("""
        INSERT INTO ods_daily
        (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor)
        VALUES ('600831.SH', '20260612', 10, 11, 9, 10.5, 100, 1000, 1.0, 1.0)
    """)
    rows = [{
        "ts_code": "600831.SH", "trade_date": "20260612",
        "open": 10, "high": 11, "low": 9, "close": 10.5,
        "vol": 100, "amount": 1000, "pct_chg": 1.0, "adj_factor": 1.05,
    }]
    result = _write_ods_daily_diff(con, rows)
    assert isinstance(result, FetchResult)
    assert result.rows_written == 1
    assert result.rows_unchanged == 0
    adj = con.execute(
        "SELECT adj_factor FROM ods_daily WHERE ts_code='600831.SH'"
    ).fetchone()[0]
    assert adj == pytest.approx(1.05, abs=1e-6)


@pytest.fixture
def db_with_schema():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    yield con
    con.close()
