"""Tests for ODS row diff before INSERT.

Float tolerance layers (see data-model spec §3.0):
- ODS diff: FLOAT_ABS_TOL=1e-4, FLOAT_LARGE_ABS_TOL=1.0, FLOAT_RTOL=1e-5 (ods_diff.py)
- DWS equivalence: atol=1e-9 (append/FULL/narrow golden tests)
"""
import duckdb
import pytest

from backend.db.schema import create_all_tables
from backend.fetch.ods_diff import (
    diff_changed_columns,
    partition_changed_daily,
    partition_changed_rows_detailed,
    values_equal,
    ODS_DAILY_DIFF_COLS,
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


def test_diff_changed_columns_insert_vs_update():
    cols = ["vol", "close"]
    row = {"ts_code": "000001.SZ", "trade_date": "20260612", "vol": 100, "close": 10.0}
    assert diff_changed_columns(row, None, cols) == ["vol", "close"]
    existing = {"vol": 100, "close": 10.0}
    assert diff_changed_columns(row, existing, cols) == []
    assert diff_changed_columns({**row, "vol": 200}, existing, cols) == ["vol"]


def test_partition_changed_rows_detailed_vol_only(db_with_schema):
    con = db_with_schema
    con.execute("""
        INSERT INTO ods_daily
        (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor)
        VALUES ('000001.SZ', '20260612', 10, 11, 9, 10.5, 100, 1000, 1.0, 1.0)
    """)
    incoming = [{
        "ts_code": "000001.SZ", "trade_date": "20260612",
        "open": 10, "high": 11, "low": 9, "close": 10.5,
        "vol": 200, "amount": 1000, "pct_chg": 1.0, "adj_factor": 1.0,
    }]
    changed, unchanged, events = partition_changed_rows_detailed(
        con, "ods_daily", ODS_DAILY_DIFF_COLS, incoming,
    )
    assert len(changed) == 1
    assert unchanged == 0
    assert ("000001.SZ", "20260612", "ods_daily", "vol", False) in events
    assert all(e[3] != "close" for e in events)


def test_partition_changed_rows_detailed_insert_marks_is_insert(db_with_schema):
    con = db_with_schema
    incoming = [{
        "ts_code": "000002.SZ", "trade_date": "20260612",
        "open": 20, "high": 21, "low": 19, "close": 20.5,
        "vol": 200, "amount": 2000, "pct_chg": 2.0, "adj_factor": 1.0,
    }]
    changed, unchanged, events = partition_changed_rows_detailed(
        con, "ods_daily", ODS_DAILY_DIFF_COLS, incoming,
    )
    assert len(changed) == 1
    assert all(e[4] is True for e in events)
    assert len(events) == len(ODS_DAILY_DIFF_COLS)


def test_write_ods_daily_diff_emits_field_events(db_with_schema):
    con = db_with_schema
    rows = [{
        "ts_code": "600831.SH", "trade_date": "20260612",
        "open": 10, "high": 11, "low": 9, "close": 10.5,
        "vol": 100, "amount": 1000, "pct_chg": 1.0, "adj_factor": 1.0,
    }]
    result = _write_ods_daily_diff(con, rows)
    assert result.rows_written == 1
    assert ("600831.SH", "20260612", "ods_daily", "close", True) in result.changed_field_events


def test_fetch_result_merge_field_events():
    a = FetchResult(
        rows_written=1,
        changed_pairs=[("000001.SZ", "20260612")],
        changed_field_events=[
            ("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False),
        ],
    )
    b = FetchResult(
        rows_written=1,
        changed_pairs=[("000001.SZ", "20260612")],
        changed_field_events=[
            ("000001.SZ", "20260612", "ods_moneyflow", "net_amount_dc", False),
        ],
    )
    merged = a.merge(b)
    assert len(merged.changed_field_events) == 2


def test_fetch_result_to_completeness_field_events():
    fr = FetchResult(
        api_rows=100,
        rows_written=2,
        rows_unchanged=98,
        changed_pairs=[("000001.SZ", "20260612"), ("000002.SZ", "20260612")],
        changed_field_events=[
            ("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False),
            ("000002.SZ", "20260612", "ods_daily", "vol", False),
        ],
    )
    comp = fr.to_completeness()
    assert comp["changed_field_events_count"] == 2
    assert comp["affected_ods_columns"] == ["circ_mv", "vol"]
    assert comp["changed_codes_count"] == 2


def test_net_amount_dc_patch_emits_field_events(db_with_schema):
    from backend.fetch.ods_daily import _apply_net_amount_dc_patch
    import pandas as pd

    con = db_with_schema
    con.execute("""
        INSERT INTO ods_moneyflow
        (ts_code, trade_date, buy_lg_vol, sell_lg_vol, net_mf_amount, net_amount_dc)
        VALUES ('000001.SZ', '20260612', 100, 50, 1000, NULL)
    """)
    patch = pd.DataFrame([{
        "ts_code": "000001.SZ", "trade_date": "20260612", "net_amount_dc": 123.45,
    }])
    result = _apply_net_amount_dc_patch(con, patch)
    assert int(result) == 1
    assert any(
        e[3] == "net_amount_dc" and e[2] == "ods_moneyflow"
        for e in result.changed_field_events
    )


@pytest.fixture
def db_with_schema():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    yield con
    con.close()
