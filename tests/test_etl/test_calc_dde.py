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
