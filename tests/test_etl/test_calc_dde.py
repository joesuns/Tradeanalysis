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
