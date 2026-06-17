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


def test_recompute_daily_trend_full_history_not_tail255():
    """EMA(60) trend must use full history — tail255 can diverge from FULL."""
    import numpy as np
    from scripts.audit_dde_trend_oracle import recompute_daily_trend

    DDECalculator = __import__(
        "backend.etl.calc_dde", fromlist=["DDECalculator"]
    ).DDECalculator
    calc = DDECalculator.__new__(DDECalculator)
    calc.freq = "daily"
    n = 400
    dates = [f"2024{(i // 28 + 1):02d}{(i % 28 + 1):02d}" for i in range(n)]
    # long flat negative, short recent flip — EMA state depends on full prefix
    net = np.concatenate([
        np.full(350, -500.0),
        np.linspace(-500.0, 800.0, 50),
    ])
    mv = np.full(n, 1e9)
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
    full = recompute_daily_trend(calc, df)
    tail255 = recompute_daily_trend(calc, df, tail_bars=255)
    full_direct = calc._compute_indicators(df.copy())["trend"].iloc[-1]
    assert full == full_direct
    assert full in ("up", "down", "flat")
    # tail255 is diagnostic-only; oracle default must not use it
    assert recompute_daily_trend(calc, df, tail_bars=None) == full_direct
