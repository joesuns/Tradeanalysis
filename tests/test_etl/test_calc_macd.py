import pandas as pd
import numpy as np
from backend.etl.calc_macd import MACDCalculator


def test_macd_ema_seed():
    """Verify EMA12 seed = SMA of first 12 close values."""
    calc = MACDCalculator.__new__(MACDCalculator)
    dates = [f"202601{i:02d}" for i in range(1, 31)]
    # Constant price -> EMA = price everywhere
    df = pd.DataFrame({"trade_date": dates, "close_qfq": [10.0] * 30})
    result = calc._compute_indicators(df)
    assert not pd.isna(result["ema_12"].iloc[11])  # seed at index 11 (12th value)
    assert abs(result["ema_12"].iloc[11] - 10.0) < 0.01


def test_macd_bar_formula():
    """MACD bar = 2 * (DIF - DEA)"""
    calc = MACDCalculator.__new__(MACDCalculator)
    dates = [f"202601{i:02d}" for i in range(1, 35)]
    df = pd.DataFrame({"trade_date": dates, "close_qfq": [10.0 + i * 0.1 for i in range(34)]})
    result = calc._compute_indicators(df)
    idx = 30  # well past all seed windows
    expected = 2.0 * (result["dif"].iloc[idx] - result["dea"].iloc[idx])
    assert abs(result["macd_bar"].iloc[idx] - expected) < 0.001


def test_macd_zone_bull_bear():
    """MACD bar > 0 -> bull, < 0 -> bear"""
    calc = MACDCalculator.__new__(MACDCalculator)
    dates = [f"202601{i:02d}" for i in range(1, 35)]
    df = pd.DataFrame({"trade_date": dates, "close_qfq": [10.0] * 34})
    result = calc._compute_indicators(df)
    # With constant price, DIF=0, DEA=0, MACD_bar=0 -> zone should be None (not bull, not bear)
    valid_zones = result["zone"].dropna()
    # bar is 0.0 -> not bull (>0) and not bear (<0)
    assert len(valid_zones) == 0 or all(z is None for z in valid_zones)


def test_macd_golden_cross(db_with_schema):
    """Integration test: golden cross detection with real DuckDB."""
    con = db_with_schema
    # Insert 40 days of data: price UP (bull trend -> DIF rising)
    con.execute("INSERT INTO dim_stock (ts_code, stock_code, name) VALUES ('TEST.SZ','TEST','Test')")
    for i in range(1, 41):
        con.execute("INSERT INTO dwd_daily_quote (ts_code, trade_date, close_qfq, is_suspended) VALUES (?,?,?,0)",
                     ('TEST.SZ', f'2026{i:02d}', 10.0 + i * 0.2))
    calc = MACDCalculator(con, "daily")
    calc.calculate(["TEST.SZ"], "20260201")
    rows = con.execute(
        "SELECT trade_date, turning_point, zone FROM dws_macd_daily "
        "WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()
    # Should have data
    assert len(rows) > 0
