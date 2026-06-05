"""Tests for backtest engine core — market regime, tradability, forward returns."""
import duckdb
import tempfile
import os
import pytest

# Module under test — will fail to import until engine.py is created
# from backend.backtest.engine import BacktestEngine


@pytest.fixture
def temp_index_db():
    """Temp DuckDB with 000001.SH daily data spanning bull/bear/sideways regimes."""
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    con = duckdb.connect(path)
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT,
            open_qfq REAL, high_qfq REAL, low_qfq REAL, close_qfq REAL,
            vol REAL, pct_chg REAL
        )
    """)
    # Insert 150 days: MA60 crossing pattern
    import numpy as np
    # Days 1-30:  sideways (close=10, MA60 stable)
    # Days 31-70: uptrend (close ramps 10→15, close > MA60 → bull)
    # Days 71-110: downtrend (close drops 15→10, close < MA60 → bear)
    # Days 111-150: sideways recovery (close=10→10.5)
    for i in range(1, 151):
        if i <= 30:
            c = 10.0
        elif i <= 70:
            c = 10.0 + (i - 30) * 0.125  # 10→15 over 40 days
        elif i <= 110:
            c = 15.0 - (i - 70) * 0.125  # 15→10 over 40 days
        else:
            c = 10.0 + (i - 110) * 0.0125  # 10→10.5 over 40 days
        con.execute(
            "INSERT INTO dwd_daily_quote VALUES (?,?,?,?,?,?,?,?)",
            ("000001.SH", f"2026{i:03d}", c, c + 0.1, c - 0.1, c, 1000000, 0.0),
        )
    con.close()
    yield path
    os.unlink(path)
    wal = path + ".wal"
    if os.path.exists(wal):
        os.unlink(wal)


def test_market_regime_ma60_bull_detected(temp_index_db):
    """Bull market detected when close > MA60 after uptrend."""
    from backend.backtest.engine import get_market_regime

    regime = get_market_regime(temp_index_db, "2026070")  # Day 70: bull peak
    assert regime == "bull", f"Expected bull at up-trend peak, got {regime}"


def test_market_regime_ma60_bear_detected(temp_index_db):
    """Bear market detected when close < MA60 after downtrend."""
    from backend.backtest.engine import get_market_regime

    regime = get_market_regime(temp_index_db, "2026110")  # Day 110: bear trough
    assert regime == "bear", f"Expected bear at down-trend trough, got {regime}"


def test_market_regime_ma60_sideways_detected(temp_index_db):
    """Sideways market when close near MA60 (within 2% band)."""
    from backend.backtest.engine import get_market_regime

    # Day 25: still sideways before uptrend begins
    regime = get_market_regime(temp_index_db, "2026025")
    assert regime == "sideways", f"Expected sideways before trend, got {regime}"
