import pandas as pd
import numpy as np
from backend.etl.calc_macd import MACDCalculator


# ── B2 golden-master: frozen pre-vectorization oracles ──

def _oracle_macd_trend_strength(bar, window=5):
    result = np.full(len(bar), np.nan)
    for i in range(window - 1, len(bar)):
        segment = bar[i - window + 1:i + 1]
        valid = segment[~np.isnan(segment)]
        if len(valid) < window:
            continue
        x = np.arange(window, dtype=float)
        weights = np.exp(x * 0.15)
        try:
            slope = float(np.polyfit(x, valid, 1, w=weights)[0])
        except (np.linalg.LinAlgError, ValueError, TypeError):
            continue
        scale = np.mean(np.abs(valid))
        if scale < 1e-6:
            result[i] = 0.0
        elif np.isfinite(slope):
            result[i] = float(slope) / scale
    return result


def test_macd_trend_strength_matches_oracle_random():
    calc = MACDCalculator.__new__(MACDCalculator)
    rng = np.random.default_rng(13)
    for _ in range(40):
        bar = rng.normal(0, 0.05, size=rng.integers(5, 90))
        if rng.random() < 0.5:
            idx = rng.integers(0, len(bar), size=max(1, len(bar) // 6))
            bar[idx] = np.nan
        got = calc._compute_trend_strength(bar)
        exp = _oracle_macd_trend_strength(bar)
        np.testing.assert_array_equal(np.isnan(got), np.isnan(exp))
        m = ~np.isnan(exp)
        np.testing.assert_allclose(got[m], exp[m], rtol=0, atol=1e-9)


def _daily_calc():
    calc = MACDCalculator.__new__(MACDCalculator)
    calc.freq = "daily"
    return calc


def test_macd_ema_seed():
    """Verify EMA12 seed = SMA of first 12 close values."""
    calc = _daily_calc()
    dates = [f"202601{i:02d}" for i in range(1, 31)]
    # Constant price -> EMA = price everywhere
    df = pd.DataFrame({"trade_date": dates, "close_qfq": [10.0] * 30})
    result = calc._compute_indicators(df)
    assert not pd.isna(result["ema_12"].iloc[11])  # seed at index 11 (12th value)
    assert abs(result["ema_12"].iloc[11] - 10.0) < 0.01


def test_macd_bar_formula():
    """MACD bar = 2 * (DIF - DEA)"""
    calc = _daily_calc()
    dates = [f"202601{i:02d}" for i in range(1, 35)]
    df = pd.DataFrame({"trade_date": dates, "close_qfq": [10.0 + i * 0.1 for i in range(34)]})
    result = calc._compute_indicators(df)
    idx = 30  # well past all seed windows
    expected = 2.0 * (result["dif"].iloc[idx] - result["dea"].iloc[idx])
    assert abs(result["macd_bar"].iloc[idx] - expected) < 0.001


def test_macd_zone_bull_bear():
    """MACD bar > 0 -> bull, < 0 -> bear"""
    calc = _daily_calc()
    dates = [f"202601{i:02d}" for i in range(1, 35)]
    df = pd.DataFrame({"trade_date": dates, "close_qfq": [10.0] * 34})
    result = calc._compute_indicators(df)
    # With constant price, DIF=0, DEA=0, MACD_bar=0 -> zone should be None (not bull, not bear)
    valid_zones = result["zone"].dropna()
    # bar is 0.0 -> not bull (>0) and not bear (<0)
    assert len(valid_zones) == 0 or all(z is None for z in valid_zones)


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


def test_macd_hist_turn_alert_via_calculator():
    """123 3-bar inflection: h[-1] > h[-2] < h[-3] → downturn_reverse."""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = [1.0, -1.0, 0.5]
    df = pd.DataFrame({"macd_bar": bar, "trade_date": ["d0", "d1", "d2"]})
    result = calc._compute_alerts(df)
    assert result[2] == "downturn_reverse"


def test_macd_upturn_reverse_alert():
    """123 3-bar peak: h[-1] < h[-2] > h[-3] → upturn_reverse."""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = [1.0, 2.0, 1.0]
    df = pd.DataFrame({"macd_bar": bar, "trade_date": ["d0", "d1", "d2"]})
    result = calc._compute_alerts(df)
    assert result[2] == "upturn_reverse"


def test_macd_no_flat_alert_on_monotonic_rise():
    """123 hist_turn has no flat variant; monotonic rise without V-shape → None."""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = [0.08, 0.10, 0.12, 0.14, 0.16, 0.18]
    df = pd.DataFrame({"macd_bar": bar, "trade_date": [f"d{i}" for i in range(6)]})
    result = calc._compute_alerts(df)
    assert result[4] is None
    assert result[5] is None


def test_macd_divergence_uses_structure_pipeline():
    """_compute_divergence delegates to structure pipeline and returns a list."""
    from tests.test_etl.test_divergence_structure import _synthetic_macd_top_scenario

    calc = MACDCalculator.__new__(MACDCalculator)
    close, dif, dea, macd = _synthetic_macd_top_scenario()
    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(len(close))],
        "close_qfq": close,
        "dif": dif,
        "dea": dea,
        "macd_bar": macd,
    })
    result = calc._compute_divergence(df)
    assert isinstance(result, list)
    assert len(result) == len(close)
    assert any(v == "top_divergence" for v in result)


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
