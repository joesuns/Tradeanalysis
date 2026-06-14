"""TDD: DDE trend content oracle audit script."""
import pandas as pd


def test_recompute_daily_trend_last_bar():
    from scripts.audit_dde_trend_oracle import recompute_daily_trend

    calc = __import__(
        "backend.etl.calc_dde", fromlist=["DDECalculator"]
    ).DDECalculator.__new__(
        __import__(
            "backend.etl.calc_dde", fromlist=["DDECalculator"]
        ).DDECalculator
    )
    calc.freq = "daily"
    n = 80
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    net = [-1000.0] * 40 + [1000.0] * 40  # 尾段上升
    mv = [1e9] * n
    df = pd.DataFrame({
        "trade_date": dates,
        "net_mf_amount": net,
        "net_amount_dc": net,
        "circ_mv": mv,
        "total_mv": mv,
        "buy_lg_vol": [100.0] * n,
        "sell_lg_vol": [50.0] * n,
        "buy_elg_vol": [30.0] * n,
        "sell_elg_vol": [20.0] * n,
        "total_vol": [200.0] * n,
        "close_qfq": [10.0] * n,
    })
    got = recompute_daily_trend(calc, df)
    assert got in ("up", "down", "flat")
    assert got == "up"
