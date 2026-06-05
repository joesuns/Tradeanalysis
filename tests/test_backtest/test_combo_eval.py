"""Tests for multi-dimension signal resonance backtesting."""
import duckdb
import tempfile
import os
import pytest


@pytest.fixture
def temp_combo_db():
    """Temp DuckDB with minimal kpattern + MACD data for combo testing."""
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    con = duckdb.connect(path)

    # DWS kpattern table
    con.execute("""
        CREATE TABLE dws_kpattern_daily (
            ts_code TEXT, trade_date TEXT, calc_date TEXT,
            yang_bao_yin INTEGER, yang_ke_yin INTEGER,
            mu_bei_xian INTEGER, bi_lei_zhen INTEGER,
            gao_kai_chang_yin INTEGER, yin_bao_yang INTEGER,
            yin_ke_yang INTEGER, strength REAL,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    con.execute("""
        CREATE VIEW v_dws_kpattern_daily_latest AS
        SELECT * FROM dws_kpattern_daily d WHERE calc_date = (
            SELECT MAX(calc_date) FROM dws_kpattern_daily
            WHERE ts_code = d.ts_code AND trade_date = d.trade_date
        )
    """)

    # DWS MACD table
    con.execute("""
        CREATE TABLE dws_macd_daily (
            ts_code TEXT, trade_date TEXT, calc_date TEXT,
            ema_12 REAL, ema_26 REAL, dif REAL, dea REAL, macd_bar REAL,
            divergence TEXT, zone TEXT, turning_point TEXT, alert TEXT,
            trend TEXT, trend_strength REAL,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    con.execute("""
        CREATE VIEW v_dws_macd_daily_latest AS
        SELECT * FROM dws_macd_daily d WHERE calc_date = (
            SELECT MAX(calc_date) FROM dws_macd_daily
            WHERE ts_code = d.ts_code AND trade_date = d.trade_date
        )
    """)

    # DWS DDE table
    con.execute("""
        CREATE TABLE dws_dde_daily (
            ts_code TEXT, trade_date TEXT, calc_date TEXT,
            net_mf_amount REAL, ddx REAL, ddx2 REAL,
            trend TEXT, alert TEXT, divergence TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    con.execute("""
        CREATE VIEW v_dws_dde_daily_latest AS
        SELECT * FROM dws_dde_daily d WHERE calc_date = (
            SELECT MAX(calc_date) FROM dws_dde_daily
            WHERE ts_code = d.ts_code AND trade_date = d.trade_date
        )
    """)

    # Insert: stock A has yang_ke_yin + MACD golden_cross on 20260115
    con.execute(
        "INSERT INTO dws_kpattern_daily VALUES "
        "('TEST.SZ', '20260115', '20260101', 0, 1, 0, 0, 0, 0, 0, 0.8)"
    )
    con.execute(
        "INSERT INTO dws_macd_daily VALUES "
        "('TEST.SZ', '20260115', '20260101', 1.0, 0.5, 0.5, 0.3, 0.4, "
        "NULL, 'bull', 'golden_cross', NULL, 'up', 0.1)"
    )

    # Stock B has yang_ke_yin WITHOUT MACD golden_cross on same date
    con.execute(
        "INSERT INTO dws_kpattern_daily VALUES "
        "('TEST2.SZ', '20260115', '20260101', 0, 1, 0, 0, 0, 0, 0, 0.7)"
    )
    con.execute(
        "INSERT INTO dws_macd_daily VALUES "
        "('TEST2.SZ', '20260115', '20260101', 1.0, 0.5, 0.5, 0.3, 0.4, "
        "NULL, 'bull', NULL, NULL, 'up', 0.05)"
    )

    con.close()
    yield path
    os.unlink(path)
    wal = path + ".wal"
    if os.path.exists(wal):
        os.unlink(wal)


def test_find_combo_yang_ke_yin_golden_cross(temp_combo_db):
    """阳克阴 + MACD金叉 共振: 只返回同时满足的股票."""
    from backend.backtest.combo_eval import find_combo_signals

    signals = find_combo_signals(
        temp_combo_db,
        "20260115",
        patterns=["yang_ke_yin"],
        macd_turning_point="golden_cross",
    )

    # Only TEST.SZ has both yang_ke_yin AND golden_cross
    ts_codes = [s["ts_code"] for s in signals]
    assert "TEST.SZ" in ts_codes, "TEST.SZ should match yang_ke_yin + golden_cross"
    assert "TEST2.SZ" not in ts_codes, (
        "TEST2.SZ should NOT match — has yang_ke_yin but NOT golden_cross"
    )
    assert len(signals) == 1


def test_find_combo_yang_ke_yin_dde_divergence(temp_combo_db):
    """阳克阴 + DDE底背离 共振: 无匹配时返回空列表."""
    from backend.backtest.combo_eval import find_combo_signals

    signals = find_combo_signals(
        temp_combo_db,
        "20260115",
        patterns=["yang_ke_yin"],
        dde_divergence="bottom_divergence",
    )

    # No DDE data inserted with bottom_divergence, should return empty
    assert signals == [], f"Expected empty, got {len(signals)} signals"
