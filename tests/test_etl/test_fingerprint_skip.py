"""Tests for DWS fingerprint skip mechanism."""
import duckdb
import pandas as pd
from backend.etl.base import (
    compute_fingerprint,
    compute_input_fingerprint,
    check_dwd_unchanged,
    load_latest_fingerprints,
    SkipReason,
)


def _make_dws(con):
    con.execute("""
        CREATE TABLE dws_test (
            ts_code TEXT, trade_date TEXT, val REAL,
            calc_date TEXT, input_fingerprint TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)


def test_load_latest_fingerprints_batch():
    """一次查询返回每股最新(MAX calc_date)指纹；无指纹的股票不在字典中。"""
    con = duckdb.connect(":memory:")
    _make_dws(con)
    # A.SZ: 旧 calc_date=old_fp, 新 calc_date=new_fp → 取 new_fp
    con.execute("INSERT INTO dws_test VALUES ('A.SZ','20260101',1,'20260604','old_fp')")
    con.execute("INSERT INTO dws_test VALUES ('A.SZ','20260101',2,'20260605','new_fp')")
    # B.SZ: 仅一次
    con.execute("INSERT INTO dws_test VALUES ('B.SZ','20260101',9,'20260604','bfp')")
    # C.SZ: 无指纹(NULL) → 不应出现在字典
    con.execute("INSERT INTO dws_test VALUES ('C.SZ','20260101',5,'20260604',NULL)")

    fps = load_latest_fingerprints(con, "dws_test", ["A.SZ", "B.SZ", "C.SZ", "D.SZ"])
    assert fps == {"A.SZ": "new_fp", "B.SZ": "bfp"}
    con.close()


def test_load_latest_fingerprints_empty():
    """空 ts_codes → 空字典，不报错。"""
    con = duckdb.connect(":memory:")
    _make_dws(con)
    assert load_latest_fingerprints(con, "dws_test", []) == {}
    con.close()


def test_check_dwd_unchanged_uses_prefetched_dict():
    """提供 latest_fps 字典时用字典比对，不查库。"""
    con = duckdb.connect(":memory:")
    _make_dws(con)  # 表为空，证明走的是字典而非 SQL
    df = pd.DataFrame({"trade_date": ["20260101"], "val": [10.0]})
    fp = compute_input_fingerprint(df)
    # 字典命中且相等 → unchanged True
    assert check_dwd_unchanged(con, "dws_test", "A.SZ", df, latest_fps={"A.SZ": fp}) is True
    # 字典命中但不等 → False
    assert check_dwd_unchanged(con, "dws_test", "A.SZ", df, latest_fps={"A.SZ": "other"}) is False
    # 字典无该股 → False(首算)
    assert check_dwd_unchanged(con, "dws_test", "A.SZ", df, latest_fps={}) is False
    con.close()


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
    fp = compute_input_fingerprint(df)
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


# ── Strategy A: domain fingerprint (P0.5) ──


def test_compute_input_fingerprint_differs_from_legacy():
    """New fingerprint format includes last_td — not equal to bare compute_fingerprint."""
    df = pd.DataFrame({
        "trade_date": ["20260101", "20260102"],
        "close": [10.0, 11.0],
    })
    assert compute_input_fingerprint(df) != compute_fingerprint(df)


def test_compute_input_fingerprint_skips_on_repeat():
    """Same df + recalc_start → identical input fingerprint."""
    df = pd.DataFrame({
        "trade_date": [f"202601{i:02d}" for i in range(1, 11)],
        "close": [10.0 + i for i in range(10)],
    })
    fp1 = compute_input_fingerprint(df, recalc_start="20260105")
    fp2 = compute_input_fingerprint(df, recalc_start="20260105")
    assert fp1 == fp2


def test_compute_input_fingerprint_changes_on_new_tail_bar():
    """Strategy A: new tail bar → last_td changes → fingerprint changes."""
    base = pd.DataFrame({
        "trade_date": [f"202601{i:02d}" for i in range(1, 11)],
        "close": [10.0] * 10,
    })
    extended = pd.concat([
        base,
        pd.DataFrame({"trade_date": ["20260111"], "close": [10.0]}),
    ], ignore_index=True)
    fp_base = compute_input_fingerprint(base, recalc_start="20260105")
    fp_ext = compute_input_fingerprint(extended, recalc_start="20260105")
    assert fp_base != fp_ext


def test_compute_input_fingerprint_window_ignores_pre_window_change():
    """Changes before recalc_start do not affect window fingerprint."""
    df1 = pd.DataFrame({
        "trade_date": [f"202601{i:02d}" for i in range(1, 11)],
        "close": [10.0] * 10,
    })
    df2 = df1.copy()
    df2.loc[0, "close"] = 99.0  # change bar before recalc_start
    fp1 = compute_input_fingerprint(df1, recalc_start="20260105")
    fp2 = compute_input_fingerprint(df2, recalc_start="20260105")
    assert fp1 == fp2


def test_compute_input_fingerprint_detects_in_window_change():
    """Changes inside recalc window still invalidate fingerprint."""
    df1 = pd.DataFrame({
        "trade_date": [f"202601{i:02d}" for i in range(1, 11)],
        "close": [10.0] * 10,
    })
    df2 = df1.copy()
    df2.loc[8, "close"] = 99.0  # change bar inside window
    fp1 = compute_input_fingerprint(df1, recalc_start="20260105")
    fp2 = compute_input_fingerprint(df2, recalc_start="20260105")
    assert fp1 != fp2


def test_check_dwd_unchanged_strategy_a_with_recalc_start():
    """Stored strategy-A fingerprint matches → skip."""
    con = duckdb.connect(":memory:")
    _make_dws(con)
    df = pd.DataFrame({
        "trade_date": [f"202601{i:02d}" for i in range(1, 11)],
        "close": [10.0 + i for i in range(10)],
    })
    fp = compute_input_fingerprint(df, recalc_start="20260105")
    con.execute(
        "INSERT INTO dws_test VALUES ('A.SZ', '20260110', 10, '20260604', ?)",
        (fp,),
    )
    assert check_dwd_unchanged(
        con, "dws_test", "A.SZ", df,
        latest_fps={"A.SZ": fp},
        recalc_start="20260105",
    ) is True
    con.close()


def test_check_dwd_unchanged_strategy_a_rejects_legacy_fp():
    """Legacy bare compute_fingerprint stored → no skip with strategy A."""
    con = duckdb.connect(":memory:")
    _make_dws(con)
    df = pd.DataFrame({
        "trade_date": ["20260101", "20260102"],
        "close": [10.0, 11.0],
    })
    legacy_fp = compute_fingerprint(df)
    con.execute(
        "INSERT INTO dws_test VALUES ('A.SZ', '20260102', 11, '20260604', ?)",
        (legacy_fp,),
    )
    assert check_dwd_unchanged(
        con, "dws_test", "A.SZ", df,
        recalc_start=None,
    ) is False
    con.close()


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


def test_volume_calculator_recalculates_when_spec_version_stale():
    """Fingerprint match but pre-v2 spec_version must still recalculate."""
    import duckdb
    from backend.etl.calc_volume import VolumeCalculator
    from backend.etl.base import compute_input_fingerprint

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
    df = con.execute(
        "SELECT * FROM dwd_daily_quote WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).df()
    fp = compute_input_fingerprint(df)
    con.execute("""
        INSERT INTO dws_volume_daily VALUES
        ('TEST.SZ', '20260129', 100, 50, 'normal', 'shrinking',
         1, 0, NULL, '20260604', ?, 'v1')
    """, [fp])

    result = calc.calculate(["TEST.SZ"], "20260604")
    assert result.calculated == 1
    assert result.total_skipped == 0
    row = con.execute("""
        SELECT trend, spec_version FROM dws_volume_daily
        WHERE ts_code='TEST.SZ' AND calc_date='20260604'
        ORDER BY trade_date DESC LIMIT 1
    """).fetchone()
    assert row[1] == "v2"
    assert row[0] != "shrinking" or row[0] in ("expanding", "shrinking", "flat")

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
