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


def test_near_golden_ma_3day_regression():
    """MA 含噪收敛——3 日回归仍能检出。"""
    calc = MACalculator.__new__(MACalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2", "d3"],
        "close_qfq": [10.0, 10.1, 10.2, 10.3],
        "ma_5":      [9.5, 9.6, 9.8, 9.9],
        "ma_10":     [10.5, 10.5, 10.4, 10.2],
        "ma5_slope": [0.5, 0.5, 0.5, 0.5],
        "ma10_slope":[0.5, 0.5, 0.5, 0.5],
    })
    result = calc._compute_turning_points(df)
    # 间距: 1.0, 0.9, 0.6, 0.3。3 日回归 [0.9,0.6,0.3] 斜率负 → 收敛
    # 0.3/10.2=2.9% < 15% → near_golden
    assert result[3] == "near_golden", f"MA 含噪收敛应触发 near_golden，实际 {result[3]}"


def test_near_golden_ma():
    """MA5 < MA10, gap narrowing, gap/MA10 < 15% → near_golden."""
    calc = MACalculator.__new__(MACalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2"],
        "close_qfq": [10.0, 10.1, 10.2],
        "ma_5":      [9.8, 9.9, 10.0],
        "ma_10":     [10.5, 10.4, 10.3],
        "ma5_slope": [0.5, 0.5, 0.5],
        "ma10_slope":[0.5, 0.5, 0.5],
    })
    result = calc._compute_turning_points(df)
    # Day 1: MA5=9.9 < MA10=10.4, gap=0.5/10.4=4.8% < 15%
    # Day 2: gap narrows: 0.4 < 0.5, MA5 < MA10 → near_golden
    assert result[2] == "near_golden", f"Expected near_golden, got {result[2]}"


def test_near_dead_ma():
    """MA5 > MA10, gap narrowing, gap/MA10 < 15% → near_dead."""
    calc = MACalculator.__new__(MACalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2"],
        "close_qfq": [10.0, 10.1, 10.2],
        "ma_5":      [10.5, 10.4, 10.3],
        "ma_10":     [9.8, 9.9, 10.0],
        "ma5_slope": [0.5, 0.5, 0.5],
        "ma10_slope":[0.5, 0.5, 0.5],
    })
    result = calc._compute_turning_points(df)
    assert result[2] == "near_dead", f"Expected near_dead, got {result[2]}"


def test_tangle_needs_cross_count():
    """Tangle requires gap < 3% AND >= 2 crosses in last 10 days.

    Without sufficient recent crosses, even a small gap should NOT be tangle.
    """
    calc = MACalculator.__new__(MACalculator)
    n = 20
    # MA5 and MA10 run parallel with 2% gap — no crosses at all
    ma5 = np.array([10.0 + i * 0.1 for i in range(n)])
    ma10 = np.array([10.2 + i * 0.1 for i in range(n)])  # gap = 0.2, ~2% of MA10
    s5 = np.full(n, 0.5)   # both rising
    s10 = np.full(n, 0.5)

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": ma5,
        "ma_5": ma5,
        "ma_10": ma10,
        "ma5_slope": s5,
        "ma10_slope": s10,
    })
    result = calc._compute_alignment(df)
    # Gap ~2% < 3%, but zero crosses in last 10 days → NOT tangle
    # Should be bull_strong (MA5 > MA10, both slopes up)
    # Wait, MA5=10.0 < MA10=10.2 initially → bull not triggered
    # MA5 crosses above MA10 when? ma5 = 10+i*0.1, ma10 = 10.2+i*0.1
    # ma5 never exceeds ma10 because they're parallel with constant gap
    # So MA5 < MA10 always. Both slopes > 0.3 → bear_rolling
    for i in range(10, n):
        gap = abs(ma5[i] - ma10[i]) / ma10[i]
        assert gap < 0.03, f"Gap at {i} should be < 3%"
        assert result[i] != "tangle", (
            f"Index {i}: gap={gap:.3%} < 3% but no crosses, should NOT be tangle, got {result[i]}"
        )


def test_tangle_with_enough_crosses():
    """Tangle triggers when gap < 3% AND >= 2 crosses in last 10 days."""
    calc = MACalculator.__new__(MACalculator)
    n = 20
    # Create MA lines that zigzag and cross frequently, then converge
    ma5  = np.array([10.0, 10.3, 9.9, 10.2, 9.8, 10.2, 9.9, 10.1, 9.8, 10.1,
                     10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    ma10 = np.array([10.1, 10.1, 10.1, 10.1, 10.1, 10.1, 10.1, 10.1, 10.1, 10.1,
                     10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    s5 = np.full(n, 0.0)
    s10 = np.full(n, 0.0)

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": ma5,
        "ma_5": ma5,
        "ma_10": ma10,
        "ma5_slope": s5,
        "ma10_slope": s10,
    })
    result = calc._compute_alignment(df)
    # Crosses at indices 1,2,3,4,5,6,7,8 (8 crosses between indices 0-9)
    # Indices 10-13 should have 2+ crosses in last 10 days + gap=0 → tangle
    assert result[10] == "tangle", f"Index 10 should be tangle, got {result[10]}"
    assert result[11] == "tangle", f"Index 11 should be tangle, got {result[11]}"
    assert result[12] == "tangle", f"Index 12 should be tangle, got {result[12]}"


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
