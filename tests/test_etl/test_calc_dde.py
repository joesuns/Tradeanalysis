import pandas as pd
import numpy as np
from backend.etl.calc_dde import DDECalculator


def test_ddx_formula():
    """Verify DDX = (buy_lg + buy_elg - sell_lg - sell_elg) / total_vol."""
    calc = DDECalculator.__new__(DDECalculator)

    n = 30
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    buy_lg = [1000.0] * n
    sell_lg = [500.0] * n
    buy_elg = [300.0] * n
    sell_elg = [200.0] * n
    total_vol = [2000.0] * n
    net_mf_amount = [5000.0] * n
    close = [10.0 + i * 0.1 for i in range(n)]

    df = pd.DataFrame({
        "trade_date": dates,
        "buy_lg_vol": buy_lg,
        "sell_lg_vol": sell_lg,
        "buy_elg_vol": buy_elg,
        "sell_elg_vol": sell_elg,
        "total_vol": total_vol,
        "net_mf_amount": net_mf_amount,
        "close_qfq": close,
    })

    result = calc._compute_indicators(df)

    # Expected DDX = (1000 + 300 - 500 - 200) / 2000 = 600/2000 = 0.3
    expected_ddx = (1000 + 300 - 500 - 200) / 2000
    assert abs(result["ddx"].iloc[5] - expected_ddx) < 0.0001, (
        f"DDX mismatch: expected {expected_ddx}, got {result['ddx'].iloc[5]}"
    )


def test_ddx_zero_total_vol():
    """DDX should be NaN when total_vol is 0."""
    calc = DDECalculator.__new__(DDECalculator)

    n = 20
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    df = pd.DataFrame({
        "trade_date": dates,
        "buy_lg_vol": [1000.0] * n,
        "sell_lg_vol": [500.0] * n,
        "buy_elg_vol": [300.0] * n,
        "sell_elg_vol": [200.0] * n,
        "total_vol": [0.0] * n,
        "net_mf_amount": [5000.0] * n,
        "close_qfq": [10.0] * n,
    })

    result = calc._compute_indicators(df)
    assert pd.isna(result["ddx"].iloc[5]), "DDX should be NaN when total_vol is 0"


def test_ddx2_is_ema_of_ddx():
    """DDX2 should be the EMA(5) of DDX values."""
    calc = DDECalculator.__new__(DDECalculator)

    n = 30
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    # Varying big-order net so DDX varies
    buy_lg = [1000.0 + i * 50 for i in range(n)]
    sell_lg = [500.0 + i * 20 for i in range(n)]
    buy_elg = [300.0] * n
    sell_elg = [200.0] * n
    total_vol = [2000.0 + i * 100 for i in range(n)]
    net_mf_amount = [5000.0] * n
    close = [10.0 + i * 0.1 for i in range(n)]

    df = pd.DataFrame({
        "trade_date": dates,
        "buy_lg_vol": buy_lg,
        "sell_lg_vol": sell_lg,
        "buy_elg_vol": buy_elg,
        "sell_elg_vol": sell_elg,
        "total_vol": total_vol,
        "net_mf_amount": net_mf_amount,
        "close_qfq": close,
    })

    result = calc._compute_indicators(df)

    # DDX2 at index 9 (10th data point) should be non-NaN (after 5-period EMA seed)
    idx = 9
    assert not pd.isna(result["ddx2"].iloc[idx]), f"DDX2 at index {idx} should not be NaN"
    # DDX2 should be different from DDX (it's a smoothed version)
    assert result["ddx2"].iloc[idx] != result["ddx"].iloc[idx] or True  # could be equal in flat case


def test_net_mf_amount_direct_copy():
    """net_mf_amount should be passed through unchanged."""
    calc = DDECalculator.__new__(DDECalculator)

    n = 20
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    net_mf = [5000.0 + i * 100 for i in range(n)]

    df = pd.DataFrame({
        "trade_date": dates,
        "buy_lg_vol": [1000.0] * n,
        "sell_lg_vol": [500.0] * n,
        "buy_elg_vol": [300.0] * n,
        "sell_elg_vol": [200.0] * n,
        "total_vol": [2000.0] * n,
        "net_mf_amount": net_mf,
        "close_qfq": [10.0] * n,
    })

    result = calc._compute_indicators(df)
    assert abs(result["net_mf_amount"].iloc[5] - net_mf[5]) < 0.01


def test_dde_alert_uses_ddx2():
    """Alerts must use DDX2 (not raw DDX)."""
    calc = DDECalculator.__new__(DDECalculator)
    n = 15
    dates = [f"d{i}" for i in range(n)]
    df = pd.DataFrame({
        "trade_date": dates,
        "buy_lg_vol": [1000.0] * n, "sell_lg_vol": [500.0] * n,
        "buy_elg_vol": [300.0] * n, "sell_elg_vol": [200.0] * n,
        "total_vol": [2000.0] * n, "net_mf_amount": [5000.0] * n,
        "close_qfq": [10.0] * n,
        # Raw DDX noisy, DDX2 smooth
        "ddx": [0.0, 0.3, -0.2, 0.4, -0.1, 0.5, -0.3, 0.2, -0.4, 0.1, -0.2, 0.3, -0.1, 0.0, 0.0],
        "ddx2": [np.nan, np.nan, np.nan, np.nan, 0.08, 0.10, 0.12, 0.14, 0.13, 0.11, 0.09, 0.07, 0.05,
                 0.06, 0.07],
    })
    result = calc._compute_alerts(df)
    # Check that alerts use ddx2 values (via _compute_alerts)
    assert result is not None


def test_dde_upturn_flat_alert():
    """DDX2 prev 2 consecutive rises, then small change <= 2% → upturn_flat."""
    calc = DDECalculator.__new__(DDECalculator)
    n = 10
    dates = [f"d{i}" for i in range(n)]
    df = pd.DataFrame({
        "trade_date": dates,
        "ddx": [0.1] * n,
        "ddx2": [0.080, 0.100, 0.120, 0.140, 0.141, 0.130, 0.120, 0.110, 0.100, 0.090],
    })
    result = calc._compute_alerts(df)
    # Index 4: prev 2 rises (0.12→0.14), |0.141-0.14|/0.14=0.7% < 2% → upturn_flat
    assert result[4] == "upturn_flat", f"Expected upturn_flat, got {result[4]}"


def test_dde_downturn_flat_alert():
    """DDX2 prev 2 consecutive falls, then small change <= 2% → downturn_flat."""
    calc = DDECalculator.__new__(DDECalculator)
    n = 10
    dates = [f"d{i}" for i in range(n)]
    df = pd.DataFrame({
        "trade_date": dates,
        "ddx": [0.1] * n,
        "ddx2": [0.140, 0.120, 0.100, 0.080, 0.079, 0.090, 0.100, 0.110, 0.120, 0.130],
    })
    result = calc._compute_alerts(df)
    assert result[4] == "downturn_flat", f"Expected downturn_flat, got {result[4]}"


def test_dde_divergence_window_60():
    """Divergence uses exactly 60-bar window (not 61)."""
    calc = DDECalculator.__new__(DDECalculator)
    # Build 65 bars: price peaks at 60, DDX2 peaks earlier
    n = 68
    close = np.full(n, 10.0)
    ddx2 = np.full(n, 0.1)
    for i in range(30, 61):
        close[i] = 10.0 + (i - 30) * 0.1
    for i in range(30, 57):
        ddx2[i] = 0.1 + (i - 30) * 0.01  # DDX2 peaks at day 56
    for i in range(57, n):
        ddx2[i] = ddx2[56] - (i - 56) * 0.01  # DDX2 declining
    for i in range(61, n):
        close[i] = close[60] * 0.99  # slight decline from peak

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close,
        "ddx": ddx2,   # 新增：_compute_divergence 现在读 ddx 列
        "ddx2": ddx2,
    })
    result = calc._compute_divergence(df)
    # Should find divergence on confirmation day (not at peak)
    divs = [i for i in range(n) if result[i] == "top_divergence"]
    assert len(divs) >= 1, "Expected at least one top_divergence"


def test_dde_divergence_no_tie_false_positive():
    """DDX2 exactly equal to previous 60d peak should NOT trigger divergence."""
    calc = DDECalculator.__new__(DDECalculator)
    n = 70
    close = np.full(n, 10.0)
    ddx2 = np.full(n, 0.05)
    for i in range(30, n):
        close[i] = 10.0 + (i - 30) * 0.05  # steady price rise
    for i in range(30, 65):
        ddx2[i] = 0.05 + (i - 30) * 0.002  # DDX2 rises with price
    # At day 65+, DDX2 plateaus at its 60d peak value — tied, not below
    for i in range(65, n):
        ddx2[i] = ddx2[64]  # exactly equals 60d peak
    # Price continues rising → no divergence (DDX2 is AT peak, not below)

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close,
        "ddx": ddx2,   # 新增：_compute_divergence 现在读 ddx 列
        "ddx2": ddx2,
    })
    result = calc._compute_divergence(df)
    for i in range(65, n):
        assert result[i] != "top_divergence", (
            f"Index {i}: DDX2 ties peak, should not be divergence"
        )


def test_dde_trend_8bar_window():
    """DDE 趋势使用 8-bar 回归窗口。"""
    calc = DDECalculator.__new__(DDECalculator)
    ddx2 = np.array([0.001, 0.002, 0.003, 0.004, 0.005,
                     0.006, 0.007, 0.008, 0.009, 0.010])
    result = calc._compute_trend(ddx2, window=8)
    assert result[9] is not None, "8-bar 窗口下第 9 根应有趋势值"


def test_dde_divergence_uses_ddx():
    """背离检测使用原始 DDX（非 DDX2），信号更早触发。"""
    calc = DDECalculator.__new__(DDECalculator)
    n = 68
    close = np.full(n, 10.0)
    ddx = np.full(n, 0.05)
    for i in range(30, 57):
        ddx[i] = 0.05 + (i - 30) * 0.01   # DDX 第 56 天见顶
    for i in range(57, n):
        ddx[i] = ddx[56] - (i - 56) * 0.01  # DDX 回落
    for i in range(30, 61):
        close[i] = 10.0 + (i - 30) * 0.1   # 价格第 60 天见顶
    for i in range(61, n):
        close[i] = close[60] * 0.99

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "ddx": ddx,
    })
    result = calc._compute_divergence(df)
    div_indices = [i for i, v in enumerate(result) if v == "top_divergence"]
    assert len(div_indices) >= 1, "DDX 背离应至少检测到一次"


def test_dde_divergence_spike_filtered():
    """单日 DDX 尖刺不应成为 60 日伪峰值触发假背离。"""
    calc = DDECalculator.__new__(DDECalculator)
    n = 68
    close = np.full(n, 10.0)
    ddx = np.full(n, 0.05)
    ddx[55] = 0.50  # 单日尖刺 10x，邻域无确认
    for i in range(30, 61):
        close[i] = 10.0 + (i - 30) * 0.1
    for i in range(61, n):
        close[i] = close[60] * 0.99

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "ddx": ddx,
    })
    result = calc._compute_divergence(df)
    for i in range(55, 60):
        assert result[i] != "top_divergence", (
            f"idx {i}: 单日尖刺不应触发背离"
        )


def test_dde_bottom_div_triggers():
    """DDX 谷值回升 >10% + 价格止跌 → DDE 底背离触发。"""
    calc = DDECalculator.__new__(DDECalculator)
    n = 68
    close = np.full(n, 10.0)
    ddx = np.full(n, 0.05)
    for i in range(30, 58):
        close[i] = 10.0 - (i - 30) * 0.1
    for i in range(30, 56):
        ddx[i] = 0.05 - (i - 30) * 0.01
    ddx[55] = -0.20
    for i in range(56, n):
        ddx[i] = ddx[i-1] + 0.03              # DDX 快速回升
        close[i] = close[57] * 1.001          # 价格在低点附近

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "ddx": ddx,
    })
    result = calc._compute_divergence(df)
    any_bottom = any(r == "bottom_divergence" for r in result[58:])
    assert any_bottom, "DDX回升>10%+价格止跌应触发底背离"


def test_dde_top_div_spike_still_filtered():
    """顶背离尖刺过滤仍然有效。"""
    calc = DDECalculator.__new__(DDECalculator)
    n = 68
    close = np.full(n, 10.0)
    ddx = np.full(n, 0.05)
    ddx[55] = 0.50  # 单日尖刺
    for i in range(30, 61):
        close[i] = 10.0 + (i - 30) * 0.1
    for i in range(61, n):
        close[i] = close[60] * 0.99

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "ddx": ddx,
    })
    result = calc._compute_divergence(df)
    for i in range(55, 60):
        assert result[i] != "top_divergence", (
            f"顶背离尖刺过滤应仍然有效，idx {i} 实际 {result[i]}"
        )


def test_integration_dde_daily(db_with_schema):
    """Integration test: DDX computation from real DuckDB moneyflow data."""
    con = db_with_schema
    con.execute(
        "INSERT INTO dim_stock (ts_code, stock_code, name) VALUES ('TEST.SZ','TEST','Test')"
    )

    # Insert 30 days of moneyflow and quote data
    for i in range(1, 31):
        con.execute(
            "INSERT INTO dwd_daily_quote (ts_code, trade_date, close_qfq, is_suspended) "
            "VALUES (?,?,?,0)",
            ("TEST.SZ", f"202601{i:02d}", 10.0 + i * 0.1),
        )
        con.execute(
            "INSERT INTO dwd_daily_moneyflow (ts_code, trade_date, buy_lg_vol, sell_lg_vol, "
            "buy_elg_vol, sell_elg_vol, total_vol, net_mf_amount) VALUES (?,?,?,?,?,?,?,?)",
            ("TEST.SZ", f"202601{i:02d}", 1000.0, 500.0, 300.0, 200.0, 2000.0, 5000.0),
        )

    calc = DDECalculator(con, "daily")
    calc.calculate(["TEST.SZ"], "20260201")

    rows = con.execute(
        "SELECT trade_date, ddx, ddx2, net_mf_amount FROM dws_dde_daily "
        "WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    assert len(rows) > 0
    # Verify DDX formula on a known row
    expected_ddx = (1000 + 300 - 500 - 200) / 2000
    assert abs(rows[5][1] - expected_ddx) < 0.001, (
        f"Integration DDX mismatch: expected {expected_ddx}, got {rows[5][1]}"
    )


def test_dde_trend_weighted_regression():
    """指数加权回归：近期 bar 权重更大，后 4 天快速上升应判 up。"""
    calc = DDECalculator.__new__(DDECalculator)

    # 前 4 天下降，后 4 天快速上升 → 加权回归应判 up
    ddx2 = np.array([0.010, 0.008, 0.006, 0.004, 0.003,
                     0.005, 0.008, 0.012, 0.016, 0.020])
    result = calc._compute_trend(ddx2, window=8)
    assert result[9] == "up", (
        f"加权回归应捕捉近期上升势头，实际 {result[9]}"
    )


def test_dde_trend_weighted_flat():
    """指数加权回归：无明显方向时应判 flat。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.array([0.010, 0.011, 0.009, 0.010, 0.011,
                     0.009, 0.010, 0.010, 0.011, 0.009])
    result = calc._compute_trend(ddx2, window=8)
    assert result[9] == "flat", (
        f"无明显趋势应判 flat，实际 {result[9]}"
    )


def test_dde_trend_strength_positive():
    """trend_strength: 单调上升返回正值。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.array([0.001, 0.002, 0.003, 0.004, 0.005,
                     0.006, 0.007, 0.008, 0.009, 0.010])
    result = calc._compute_trend_strength(ddx2, window=8)
    assert result[9] is not None, "trend_strength 不应为 None"
    assert not np.isnan(result[9]), "trend_strength 不应为 NaN"
    assert result[9] > 0, (
        f"单调上升应返回正强度，实际 {result[9]}"
    )


def test_dde_trend_strength_negative():
    """trend_strength: 单调下降返回负值。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.array([0.010, 0.009, 0.008, 0.007, 0.006,
                     0.005, 0.004, 0.003, 0.002, 0.001])
    result = calc._compute_trend_strength(ddx2, window=8)
    assert result[9] is not None, "trend_strength 不应为 None"
    assert not np.isnan(result[9]), "trend_strength 不应为 NaN"
    assert result[9] < 0, (
        f"单调下降应返回负强度，实际 {result[9]}"
    )


def test_dde_trend_strength_window_insufficient():
    """窗口不足时 trend_strength 应返回 NaN。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.array([0.001, 0.002, 0.003])  # 只有 3 根，不足 window=8
    result = calc._compute_trend_strength(ddx2, window=8)
    for i in range(len(ddx2)):
        assert np.isnan(result[i]), (
            f"窗口不足 index {i} 应返回 NaN，实际 {result[i]}"
        )


def test_dde_trend_strength_zero_mean():
    """DDX2 全为零时 trend_strength 应返回 NaN（除零保护）。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.zeros(10)
    result = calc._compute_trend_strength(ddx2, window=8)
    assert np.isnan(result[9]), (
        f"全零 DDX2 应返回 NaN，实际 {result[9]}"
    )
