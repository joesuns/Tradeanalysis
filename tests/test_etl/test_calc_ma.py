import pandas as pd
import numpy as np
from backend.etl.calc_ma import MACalculator


def test_ma5_ma10_formula():
    """Verify MA5 = SMA(close, 5), MA10 = SMA(close, 10)."""
    calc = MACalculator.__new__(MACalculator)
    dates = [f"202601{i:02d}" for i in range(1, 31)]
    # Linearly increasing prices: 10, 10.1, 10.2, ...
    prices = [10.0 + i * 0.1 for i in range(30)]
    df = pd.DataFrame({"trade_date": dates, "close_qfq": prices})
    result = calc._compute_indicators(df)

    # MA5 at index 4 (5th value): average of first 5
    expected_ma5 = np.mean(prices[0:5])
    assert abs(result["ma_5"].iloc[4] - expected_ma5) < 0.001

    # MA10 at index 9 (10th value): average of first 10
    expected_ma10 = np.mean(prices[0:10])
    assert abs(result["ma_10"].iloc[9] - expected_ma10) < 0.001

    # Early values (before period) should be NaN
    assert pd.isna(result["ma_5"].iloc[3])
    assert pd.isna(result["ma_10"].iloc[8])


def test_bias_formula():
    """Verify bias = (close - MA) / MA * 100."""
    calc = MACalculator.__new__(MACalculator)
    dates = [f"202601{i:02d}" for i in range(1, 21)]
    prices = [10.0 + i * 0.1 for i in range(20)]
    df = pd.DataFrame({"trade_date": dates, "close_qfq": prices})
    result = calc._compute_indicators(df)

    idx = 10  # well past all seeds
    ma5 = result["ma_5"].iloc[idx]
    close = prices[idx]
    expected_bias = (close - ma5) / ma5 * 100.0
    assert abs(result["bias_ma5"].iloc[idx] - expected_bias) < 0.01


def test_alignment_bull_strong():
    """Verify bull_strong alignment when MA5 > MA10 and both slopes > 0.3."""
    calc = MACalculator.__new__(MACalculator)
    # Build prices where MA5 > MA10 and both are rising
    # Steadily increasing prices ensure MA5 > MA10 and positive slopes
    dates = [f"202601{i:02d}" for i in range(1, 41)]
    prices = [10.0 + i * 0.2 for i in range(40)]
    df = pd.DataFrame({"trade_date": dates, "close_qfq": prices})
    result = calc._compute_indicators(df)

    # By index 30+, with strong uptrend, alignment should be bull_strong
    valid = result["alignment"].dropna()
    bull_strong_count = (valid == "bull_strong").sum()
    assert bull_strong_count > 0, f"Expected bull_strong in uptrend, got: {valid.unique()}"


def test_golden_cross(db_with_schema):
    """Integration test: golden cross detection with real DuckDB."""
    con = db_with_schema
    con.execute(
        "INSERT INTO dim_stock (ts_code, stock_code, name) VALUES ('TEST.SZ','TEST','Test')"
    )

    # Prices that cause MA5 to cross above MA10 around day 15
    # First 10 days: flat at 10 (MA5 = MA10)
    # Then rise sharply so MA5 pulls above MA10
    for i in range(1, 31):
        if i <= 10:
            price = 10.0
        elif i <= 15:
            price = 10.0 + (i - 10) * 0.05  # slow rise
        else:
            price = 10.25 + (i - 15) * 0.5  # fast rise
        con.execute(
            "INSERT INTO dwd_daily_quote (ts_code, trade_date, close_qfq, is_suspended) VALUES (?,?,?,0)",
            ("TEST.SZ", f"202601{i:02d}", price),
        )

    calc = MACalculator(con, "daily")
    calc.calculate(["TEST.SZ"], "20260201")

    rows = con.execute(
        "SELECT trade_date, alignment, turning_point FROM dws_ma_daily "
        "WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    assert len(rows) > 0
    # At least one golden_cross should be detected
    turning_points = [r[2] for r in rows if r[2] is not None]
    assert "golden_cross" in turning_points, (
        f"Expected golden_cross, got turning points: {turning_points}"
    )
