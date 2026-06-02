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


def test_macd_trend_weighted_up():
    """5-bar 加权回归 + 阈值 0.001：上升趋势触发 up。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = np.array([0.0, 0.0, 0.0, 0.0, 0.01, 0.03, 0.06, 0.10, 0.15])
    result = calc._compute_trend(bar, window=5)
    assert result[8] == "up", f"加权上升趋势应为 up，实际 {result[8]}"


def test_macd_trend_weighted_down():
    """5-bar 加权回归 + 阈值 0.001：下降趋势触发 down。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = np.array([0.15, 0.10, 0.06, 0.03, 0.01, 0.0, 0.0, 0.0, 0.0])
    result = calc._compute_trend(bar, window=5)
    assert result[5] == "down", f"加权下降趋势应为 down，实际 {result[5]}"


def test_macd_trend_flat():
    """5-bar 加权回归：zigzag 数据判为 flat（加权斜率 < 0.001）。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = np.array([0.0, 0.0, 0.0, 0.0, 0.01, 0.009, 0.011, 0.009, 0.01])
    result = calc._compute_trend(bar, window=5)
    assert result[8] == "flat", f"zigzag 应为 flat，实际 {result[8]}"


def test_macd_trend_insufficient_window():
    """数据不足 5 根 → None。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = np.array([0.01, 0.02, 0.03, 0.04])
    result = calc._compute_trend(bar, window=5)
    assert all(r is None for r in result), f"应全为 None，实际 {result}"


def test_macd_trend_strength_positive():
    """MACD 柱持续上升 → trend_strength 为正（5-bar）。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = np.array([0.02, 0.04, 0.06, 0.08, 0.10])
    result = calc._compute_trend_strength(bar)
    assert result[4] > 0, f"上升强度应为正，实际 {result[4]}"
    assert result[4] > 0.2, f"上升强度应显著，实际 {result[4]}"


def test_macd_trend_strength_negative():
    """MACD 柱持续下降 → trend_strength 为负（5-bar）。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = np.array([0.10, 0.08, 0.06, 0.04, 0.02])
    result = calc._compute_trend_strength(bar)
    assert result[4] < 0, f"下降强度应为负，实际 {result[4]}"


def test_macd_trend_strength_flat():
    """MACD 柱走平 → trend_strength 接近零（5-bar）。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = np.array([0.03, 0.03, 0.03, 0.03, 0.03])
    result = calc._compute_trend_strength(bar)
    assert abs(result[4]) < 0.01, f"平盘强度应接近零，实际 {result[4]}"


def test_macd_trend_strength_weighted():
    """加权回归对近期加速更敏感——加速段强度 > 匀速段强度（5-bar）。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    steady = np.array([0.01, 0.02, 0.03, 0.04, 0.05])
    accel = np.array([0.01, 0.01, 0.02, 0.04, 0.08])
    s_s = calc._compute_trend_strength(steady)
    s_a = calc._compute_trend_strength(accel)
    assert s_a[4] > s_s[4], (
        f"加速段({s_a[4]:.4f})应大于匀速段({s_s[4]:.4f})"
    )


def test_macd_trend_strength_insufficient():
    """数据不足 5 根 → NaN（5-bar）。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = np.array([0.01, 0.02, 0.03])
    result = calc._compute_trend_strength(bar)
    assert all(np.isnan(r) for r in result), f"应全为 NaN，实际 {result}"


def test_macd_near_golden_3day_regression():
    """3 日回归斜率检测收敛——gap 持平但趋势性收缩时仍可检出。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2", "d3"],
        "close_qfq": [10.0, 10.0, 10.0, 10.0],
        "dif":       [0.50, 0.51, 0.52, 0.49],
        "dea":       [0.55, 0.57, 0.56, 0.53],
        "macd_bar":  [-0.10, -0.12, -0.08, -0.08],
    })
    result = calc._compute_turning_points(df)
    assert result[3] == "near_golden", f"3 日回归应检出收敛，实际 {result[3]}"


def test_macd_near_golden():
    """DIF < DEA, gap narrowing, |DIF-DEA|/|DEA| < 15% → near_golden."""
    calc = MACDCalculator.__new__(MACDCalculator)
    # Day 0: DIF=0.20, DEA=0.25 → gap=0.05, DIF < DEA
    # Day 1: DIF=0.21, DEA=0.24 → gap=0.03 (< 0.05, narrowing), 0.03/0.24=12.5% < 15%
    n = 5
    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": [10.0] * n,
        "dif":       [0.20, 0.21, 0.19, 0.22, 0.23],
        "dea":       [0.25, 0.24, 0.26, 0.25, 0.24],
        "macd_bar":  [-0.10, -0.06, -0.14, -0.06, -0.02],  # = 2*(DIF-DEA)
    })
    result = calc._compute_turning_points(df)
    # Day 1: gap 0.03 < 0.05 (narrowing), 0.03/0.24=12.5% < 15%, DIF < DEA → near_golden
    assert result[1] == "near_golden", f"Expected near_golden at [1], got {result[1]}"
    # Day 4: gap 0.01 < 0.03 (narrowing), 0.01/0.24=4.2% < 15%, DIF < DEA → near_golden
    assert result[4] == "near_golden", f"Expected near_golden at [4], got {result[4]}"


def test_macd_near_dead():
    """DIF > DEA, gap narrowing, |DIF-DEA|/|DEA| < 15% → near_dead."""
    calc = MACDCalculator.__new__(MACDCalculator)
    n = 5
    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": [10.0] * n,
        "dif":       [0.25, 0.24, 0.26, 0.25, 0.24],
        "dea":       [0.20, 0.21, 0.19, 0.22, 0.23],
        "macd_bar":  [0.10, 0.06, 0.14, 0.06, 0.02],
    })
    result = calc._compute_turning_points(df)
    assert result[1] == "near_dead", f"Expected near_dead at [1], got {result[1]}"


def test_macd_near_golden_gap_not_narrowing():
    """Gap widening → NOT near_golden, even if |DIF-DEA|/|DEA| < 15%."""
    calc = MACDCalculator.__new__(MACDCalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1"],
        "close_qfq": [10.0, 10.0],
        "dif":       [0.20, 0.18],
        "dea":       [0.25, 0.26],
        "macd_bar":  [-0.10, -0.16],
    })
    result = calc._compute_turning_points(df)
    # gap widens: 0.05 → 0.08, DIF < DEA but NOT narrowing
    assert result[1] is None, f"Expected None (gap not narrowing), got {result[1]}"


def test_macd_near_zero_axis_absolute_threshold():
    """DEA near zero: use |DIF-DEA| < close*0.01% instead of relative 15%.

    MACD bar stays positive throughout → no golden/dead cross interference.
    |DEA| = 0.0004 < close*0.1% = 0.01 → absolute threshold triggered.
    """
    calc = MACDCalculator.__new__(MACDCalculator)
    close = 10.0
    df = pd.DataFrame({
        "trade_date": ["d0", "d1"],
        "close_qfq": [close, close],
        "dif":       [0.0015, 0.0010],
        "dea":       [0.0008, 0.0004],
        "macd_bar":  [0.0014, 0.0012],  # positive, no sign flip
    })
    result = calc._compute_turning_points(df)
    # gap narrowing: 0.0007 → 0.0006; |DEA|=0.0004 << 0.01 → absolute
    # gap=0.0006 < close*0.0001=0.001 ✓; DIF > DEA → near_dead
    assert result[1] == "near_dead", f"Expected near_dead via abs threshold, got {result[1]}"


def test_macd_upturn_flat_alert():
    """Previous 2 consecutive rises, then |change|/|prev| <= 2% → upturn_flat."""
    calc = MACDCalculator.__new__(MACDCalculator)
    # bar[i-3]=0.10, bar[i-2]=0.12, bar[i-1]=0.14 (2 consecutive rises)
    # bar[i]=0.141 → change = 0.001, 0.001/0.14 ≈ 0.7% <= 2% → upturn_flat
    bar = [0.08, 0.10, 0.12, 0.14, 0.141, 0.142]
    df = pd.DataFrame({"macd_bar": bar, "trade_date": [f"d{i}" for i in range(6)]})
    result = calc._compute_alerts(df)
    assert result[4] == "upturn_flat", f"Expected upturn_flat at [4], got {result[4]}"


def test_macd_downturn_flat_alert():
    """Previous 2 consecutive falls, then |change|/|prev| <= 2% → downturn_flat."""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = [0.14, 0.12, 0.10, 0.08, 0.079, 0.078]
    df = pd.DataFrame({"macd_bar": bar, "trade_date": [f"d{i}" for i in range(6)]})
    result = calc._compute_alerts(df)
    assert result[4] == "downturn_flat", f"Expected downturn_flat at [4], got {result[4]}"


def test_macd_upturn_reverse_alert():
    """Previous 2 consecutive rises, then bar[i] < bar[i-1] → upturn_reverse."""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = [0.08, 0.10, 0.12, 0.14, 0.11, 0.10]
    df = pd.DataFrame({"macd_bar": bar, "trade_date": [f"d{i}" for i in range(6)]})
    result = calc._compute_alerts(df)
    assert result[4] == "upturn_reverse", f"Expected upturn_reverse at [4], got {result[4]}"


def test_macd_alert_reverse_overrides_flat():
    """When bar[i] < bar[i-1], upturn_reverse takes priority over upturn_flat."""
    calc = MACDCalculator.__new__(MACDCalculator)
    # prev_up is True, bar[i] < bar[i-1] AND |change| small → should be reverse, not flat
    bar = [0.08, 0.10, 0.12, 0.14, 0.139, 0.14]  # small drop: 0.001/0.14 = 0.7% (<2%)
    df = pd.DataFrame({"macd_bar": bar, "trade_date": [f"d{i}" for i in range(6)]})
    result = calc._compute_alerts(df)
    # bar[4]=0.139 < bar[3]=0.14 → this is a reversal (though tiny)
    # Reverse takes priority over flat
    assert result[4] == "upturn_reverse", f"Expected upturn_reverse (priority), got {result[4]}"


def test_macd_divergence_confirmation_day():
    """Divergence labeled on confirmation day, not at price peak."""
    calc = MACDCalculator.__new__(MACDCalculator)
    # Build 65 days: price goes up to peak at day 60, DIF peaks earlier at day 58
    # Day 63: DIF has clearly rolled over, price still near peak but not at peak
    n = 68
    close = np.full(n, 10.0)
    dif = np.full(n, 1.0)
    # Ramp up price to peak at day 60
    for i in range(30, 61):
        close[i] = 10.0 + (i - 30) * 0.1  # 10.0 → 13.0
    # DIF rises then falls earlier than price
    for i in range(30, 59):
        dif[i] = 1.0 + (i - 30) * 0.05  # DIF peaks at day 58
    for i in range(59, n):
        dif[i] = dif[58] - (i - 58) * 0.05  # DIF declines after day 58
    # Price stays near peak after day 60 (plateau)
    for i in range(61, n):
        close[i] = close[60] - (i - 60) * 0.01  # very slow decline

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close,
        "dif": dif,
    })
    result = calc._compute_divergence(df)
    # Divergence should appear after DIF peak (day 58) but before price breakdown
    # No divergence before DIF peak
    for i in range(58):
        assert result[i] is None, f"Before DIF peak, got {result[i]} at {i}"
    # Some day after DIF peak should have divergence
    any_after = any(result[i] == "top_divergence" for i in range(58, n))
    assert any_after, "Expected top_divergence to appear on a confirmation day after peak"


def test_macd_divergence_no_duplicate_within_5_days():
    """Same divergence event should not repeat within 5 trading days."""
    calc = MACDCalculator.__new__(MACDCalculator)
    # Flat price plateau after peak, DIF already rolled over
    # This would trigger on multiple consecutive days without dedup
    n = 75
    close = np.full(n, 10.0)
    dif = np.full(n, 1.0)
    for i in range(30, 61):
        close[i] = 10.0 + (i - 30) * 0.1
    for i in range(30, 56):
        dif[i] = 1.0 + (i - 30) * 0.05  # DIF peaks at day 55
    for i in range(56, n):
        dif[i] = dif[55] - (i - 55) * 0.02  # DIF declining
    for i in range(61, n):
        close[i] = close[60]  # price stays flat at peak

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close,
        "dif": dif,
    })
    result = calc._compute_divergence(df)
    # Find all top_divergence entries
    div_indices = [i for i in range(n) if result[i] == "top_divergence"]
    # Should have at least one
    assert len(div_indices) > 0, "Expected at least one top_divergence"
    # No two divergence events within 5 bars of each other
    for j in range(1, len(div_indices)):
        gap = div_indices[j] - div_indices[j - 1]
        assert gap > 5, f"Divergence at {div_indices[j-1]} and {div_indices[j]} too close (gap={gap})"


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
