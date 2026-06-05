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


# ── Calculator integration ──


def test_volume_calculator_skips_on_fingerprint_match():
    """VolumeCalculator should skip stock when DWD fingerprint matches."""
    import duckdb
    from backend.etl.calc_volume import VolumeCalculator
    from backend.etl.base import CalcResult, SkipReason

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT,
            open_qfq REAL, high_qfq REAL, low_qfq REAL, close_qfq REAL,
            vol REAL, amount REAL, pct_chg REAL,
            total_mv REAL, pe_ttm REAL, turnover_rate REAL, volume_ratio REAL,
            is_suspended INTEGER
        )
    """)
    for i in range(30):
        con.execute(
            "INSERT INTO dwd_daily_quote VALUES "
            "('TEST.SZ', ?, 10,11,9,10,100,1000,0,100,15,0.5,1,0)",
            (f"202601{i:02d}",),
        )
    # Also create the DWS table (normally done by schema)
    con.execute("""
        CREATE TABLE dws_volume_daily (
            ts_code TEXT, trade_date TEXT,
            ma_vol_5 REAL, pct_vol_rank REAL, zone TEXT, trend TEXT,
            volume_ratio REAL, trend_strength REAL, divergence TEXT,
            calc_date TEXT, input_fingerprint TEXT, spec_version TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)

    calc = VolumeCalculator(con, "daily")

    # First calc — should compute
    result1 = calc.calculate(["TEST.SZ"], "20260604")
    assert result1.calculated == 1
    assert result1.total_skipped == 0

    # Second calc — same DWD data → should skip
    result2 = calc.calculate(["TEST.SZ"], "20260604")
    assert result2.calculated == 0, "Should skip — DWD unchanged"
    assert result2.total_skipped == 1
    assert SkipReason.FINGERPRINT_MATCH in result2.skipped

    con.close()


def test_volume_calculator_recalculates_when_dwd_changes():
    """VolumeCalculator should recalculate when DWD data changes."""
    import duckdb
    from backend.etl.calc_volume import VolumeCalculator

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT,
            open_qfq REAL, high_qfq REAL, low_qfq REAL, close_qfq REAL,
            vol REAL, amount REAL, pct_chg REAL,
            total_mv REAL, pe_ttm REAL, turnover_rate REAL, volume_ratio REAL,
            is_suspended INTEGER
        )
    """)
    for i in range(30):
        con.execute(
            "INSERT INTO dwd_daily_quote VALUES "
            "('TEST.SZ', ?, 10,11,9,10,100,1000,0,100,15,0.5,1,0)",
            (f"202601{i:02d}",),
        )
    con.execute("""
        CREATE TABLE dws_volume_daily (
            ts_code TEXT, trade_date TEXT,
            ma_vol_5 REAL, pct_vol_rank REAL, zone TEXT, trend TEXT,
            volume_ratio REAL, trend_strength REAL, divergence TEXT,
            calc_date TEXT, input_fingerprint TEXT, spec_version TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)

    calc = VolumeCalculator(con, "daily")

    # First calc
    calc.calculate(["TEST.SZ"], "20260604")

    # Add new DWD data
    con.execute(
        "INSERT INTO dwd_daily_quote VALUES "
        "('TEST.SZ', '20260131', 15,16,14,15,200,2000,0,100,15,0.5,1,0)",
    )

    # Second calc — DWD changed → should recalculate
    result2 = calc.calculate(["TEST.SZ"], "20260604")
    assert result2.calculated == 1, "Should recalculate — DWD changed"
    assert result2.total_skipped == 0

    con.close()
