"""Tests for DWS fingerprint skip mechanism."""
import duckdb
import pandas as pd
from backend.etl.base import (
    compute_fingerprint,
    check_dwd_unchanged,
    SkipReason,
)


def test_compute_fingerprint_detects_change():
    """Different data → different fingerprint."""
    df1 = pd.DataFrame({"close": [10.0, 11.0, 12.0], "vol": [100, 200, 300]})
    df2 = pd.DataFrame({"close": [10.0, 11.0, 13.0], "vol": [100, 200, 300]})
    assert compute_fingerprint(df1) != compute_fingerprint(df2)


def test_compute_fingerprint_same_data_same_fp():
    """Same data → same fingerprint."""
    df1 = pd.DataFrame({"close": [10.0, 11.0, 12.0]})
    df2 = pd.DataFrame({"close": [10.0, 11.0, 12.0]})
    assert compute_fingerprint(df1) == compute_fingerprint(df2)


def test_compute_fingerprint_ignores_non_numeric():
    """String columns are excluded from fingerprint (auto-detect mode)."""
    df = pd.DataFrame({
        "close": [10.0, 11.0],
        "ts_code": ["A", "B"],
        "trade_date": ["20260101", "20260102"],
    })
    fp = compute_fingerprint(df)
    assert len(fp) == 16


def test_check_dwd_unchanged_with_match():
    """Fingerprint matches last stored → unchanged."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_test (
            ts_code TEXT, trade_date TEXT, val REAL,
            calc_date TEXT, input_fingerprint TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    df = pd.DataFrame({"trade_date": ["20260101"], "val": [10.0]})
    fp = compute_fingerprint(df)
    con.execute(
        "INSERT INTO dws_test VALUES ('A.SZ', '20260101', 10, '20260604', ?)",
        (fp,),
    )
    assert check_dwd_unchanged(con, "dws_test", "A.SZ", df) is True
    con.close()


def test_check_dwd_unchanged_with_mismatch():
    """Fingerprint differs → not unchanged."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_test (
            ts_code TEXT, trade_date TEXT, val REAL,
            calc_date TEXT, input_fingerprint TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    con.execute(
        "INSERT INTO dws_test VALUES ('A.SZ', '20260101', 10, '20260604', 'abc123')",
    )
    df = pd.DataFrame({"trade_date": ["20260101"], "val": [99.0]})
    assert check_dwd_unchanged(con, "dws_test", "A.SZ", df) is False
    con.close()


def test_check_dwd_unchanged_no_history():
    """No prior fingerprint → not unchanged (first calc)."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_test (
            ts_code TEXT, trade_date TEXT, val REAL,
            calc_date TEXT, input_fingerprint TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    df = pd.DataFrame({"trade_date": ["20260101"], "val": [10.0]})
    assert check_dwd_unchanged(con, "dws_test", "A.SZ", df) is False
    con.close()


def test_skip_reason_fingerprint_match():
    """FINGERPRINT_MATCH is a valid SkipReason."""
    assert SkipReason.FINGERPRINT_MATCH == "fingerprint_match"
