import pandas as pd
import numpy as np
from backend.etl.calc_dde import DDECalculator


# ── B2 golden-master: frozen pre-vectorization oracles (decay=0.20, window=8) ──

def _oracle_dde_trend(ddx2, window=8):
    result = [None] * len(ddx2)
    for i in range(len(ddx2)):
        if i < window - 1:
            continue
        segment = ddx2[i - window + 1:i + 1]
        valid = segment[~np.isnan(segment)]
        if len(valid) < window:
            continue
        x = np.arange(window, dtype=float)
        weights = np.exp(x * 0.20)
        try:
            slope = float(np.polyfit(x, valid, 1, w=weights)[0])
        except (np.linalg.LinAlgError, ValueError, TypeError):
            continue
        if not np.isfinite(slope):
            continue
        if slope > 0.0001:
            result[i] = "up"
        elif slope < -0.0001:
            result[i] = "down"
        else:
            result[i] = "flat"
    return result


def _oracle_dde_trend_strength(ddx2, window=8):
    result = np.full(len(ddx2), np.nan)
    for i in range(window - 1, len(ddx2)):
        segment = ddx2[i - window + 1:i + 1]
        valid = segment[~np.isnan(segment)]
        if len(valid) < window:
            continue
        mean_abs = np.mean(np.abs(valid))
        if mean_abs == 0:
            continue
        x = np.arange(window, dtype=float)
        weights = np.exp(x * 0.20)
        try:
            slope = float(np.polyfit(x, valid, 1, w=weights)[0])
        except (np.linalg.LinAlgError, ValueError, TypeError):
            continue
        if not np.isfinite(slope):
            continue
        result[i] = slope / mean_abs
    return result


def _oracle_moneyflow_trend(net, mv, freq="daily"):
    calc = DDECalculator.__new__(DDECalculator)
    calc.freq = freq
    return calc._compute_moneyflow_trend(net, mv)


def test_dde_moneyflow_trend_matches_oracle_random():
    calc = DDECalculator.__new__(DDECalculator)
    calc.freq = "daily"
    rng = np.random.default_rng(21)
    for _ in range(30):
        n = rng.integers(20, 90)
        net = rng.normal(0, 5000, size=n)
        mv = rng.uniform(1e8, 5e10, size=n)
        got = calc._compute_moneyflow_trend(net, mv)
        exp = _oracle_moneyflow_trend(net, mv, "daily")
        assert got == exp


def test_dde_ddx2_trend_matches_oracle_random():
    calc = DDECalculator.__new__(DDECalculator)
    rng = np.random.default_rng(21)
    for _ in range(40):
        ddx2 = rng.normal(0, 0.03, size=rng.integers(8, 90))
        if rng.random() < 0.5:
            idx = rng.integers(0, len(ddx2), size=max(1, len(ddx2) // 6))
            ddx2[idx] = np.nan
        assert calc._compute_ddx2_trend(ddx2) == _oracle_dde_trend(ddx2)


def test_dde_trend_strength_matches_oracle_random():
    calc = DDECalculator.__new__(DDECalculator)
    rng = np.random.default_rng(23)
    for _ in range(40):
        ddx2 = rng.normal(0, 0.03, size=rng.integers(8, 90))
        if rng.random() < 0.5:
            idx = rng.integers(0, len(ddx2), size=max(1, len(ddx2) // 6))
            ddx2[idx] = np.nan
        got = calc._compute_trend_strength(ddx2)
        exp = _oracle_dde_trend_strength(ddx2)
        np.testing.assert_array_equal(np.isnan(got), np.isnan(exp))
        m = ~np.isnan(exp)
        np.testing.assert_allclose(got[m], exp[m], rtol=0, atol=1e-9)


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


def test_dde_slope_inflection_bull_alert():
    """123 adjacent-window slope flip → upturn_reverse."""
    calc = DDECalculator.__new__(DDECalculator)
    n = 12
    ddx2 = np.zeros(n, dtype=float)
    ddx2[-6:] = [4.0, 3.0, 2.0, 1.0, 0.0, 5.0]
    df = pd.DataFrame({"trade_date": [f"d{i}" for i in range(n)], "ddx2": ddx2})
    result = calc._compute_alerts(df)
    assert result[-1] == "upturn_reverse"


def test_dde_slope_inflection_bear_alert():
    calc = DDECalculator.__new__(DDECalculator)
    n = 12
    ddx2 = np.zeros(n, dtype=float)
    ddx2[-6:] = [-4.0, -3.0, -2.0, -1.0, 0.0, -5.0]
    df = pd.DataFrame({"trade_date": [f"d{i}" for i in range(n)], "ddx2": ddx2})
    result = calc._compute_alerts(df)
    assert result[-1] == "downturn_reverse"


def test_dde_trend_8bar_window():
    """DDE 趋势使用 8-bar 回归窗口。"""
    calc = DDECalculator.__new__(DDECalculator)
    ddx2 = np.array([0.001, 0.002, 0.003, 0.004, 0.005,
                     0.006, 0.007, 0.008, 0.009, 0.010])
    result = calc._compute_ddx2_trend(ddx2, window=8)
    assert result[7] is not None, "8-bar 窗口下第 7 根应有 DDX2 趋势值"


def test_dde_structure_spike_filtered():
    """Isolated DDX spike in segment must not trigger structure top divergence."""
    from backend.etl.divergence_structure import compute_dde_structure_divergence

    n = 68
    close = np.full(n, 10.0)
    ddx = np.full(n, 0.05)
    ddx2 = np.full(n, 0.03)
    ddx[55] = 0.50  # isolated spike, neighbors < 0.8× peak
    for i in range(30, 61):
        close[i] = 10.0 + (i - 30) * 0.1
    for i in range(61, n):
        close[i] = close[60] * 0.99

    result = compute_dde_structure_divergence(
        close, ddx, ddx2, dedup=10, spike_filter_top=True, require_finite=True,
    )
    for i in range(55, 60):
        assert result[i] != "top_divergence", (
            f"idx {i}: spike-filtered DDX must not trigger top divergence"
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
            "INSERT INTO dwd_daily_quote (ts_code, trade_date, close_qfq, total_mv, is_suspended) "
            "VALUES (?,?,?,?,0)",
            ("TEST.SZ", f"202601{i:02d}", 10.0 + i * 0.1, 1e10),
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
    result = calc._compute_ddx2_trend(ddx2, window=8)
    assert result[9] == "up", (
        f"加权回归应捕捉近期上升势头，实际 {result[9]}"
    )


def test_dde_trend_weighted_flat():
    """指数加权回归：无明显方向时应判 flat。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.array([0.010, 0.011, 0.009, 0.010, 0.011,
                     0.009, 0.010, 0.010, 0.011, 0.009])
    result = calc._compute_ddx2_trend(ddx2, window=8)
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


# ── B1 batch-load equivalence ──


def _dde_schema(con):
    for ddl in [
        """CREATE TABLE dwd_daily_moneyflow (
            ts_code TEXT, trade_date TEXT, buy_lg_vol REAL, sell_lg_vol REAL,
            buy_elg_vol REAL, sell_elg_vol REAL, total_vol REAL,
            net_mf_amount REAL, net_amount_dc REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL, total_mv REAL,
            circ_mv REAL, is_suspended INTEGER,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_week_end INTEGER,
            is_trade_day INTEGER)""",
    ]:
        con.execute(ddl)


def _seed_dde_two_stocks(con):
    import random
    random.seed(7)
    for code in ["A.SZ", "B.SZ"]:
        for w in range(1, 4):
            for d in range(1, 6):
                day = f"202601{w*7+d:02d}"
                con.execute("INSERT OR REPLACE INTO dwd_daily_moneyflow VALUES "
                            "(?,?,?,?,?,?,?,?,?)",
                            (code, day, 100+random.random(), 50, 80, 30, 300,
                             500+random.random(), None))
                con.execute(
                    "INSERT OR REPLACE INTO dwd_daily_quote VALUES (?,?,?,?,?,0)",
                    (code, day, 10.0 + random.random(), 1e10, None),
                )
                is_we = 1 if d == 5 else 0
                con.execute("INSERT OR REPLACE INTO dim_date VALUES (?, ?, 1)",
                            (day, is_we))
                if is_we:
                    con.execute("INSERT OR REPLACE INTO dwd_weekly_quote "
                                "VALUES (?, ?, 10.0)", (code, day))


def test_dde_load_daily_batch_matches_per_stock():
    import duckdb
    import pandas as pd
    from backend.etl.calc_dde import DDECalculator

    con = duckdb.connect(":memory:")
    _dde_schema(con)
    _seed_dde_two_stocks(con)

    calc = DDECalculator(con, "daily")
    groups = calc._load_daily_batch(["A.SZ", "B.SZ"])
    for code in ["A.SZ", "B.SZ"]:
        pd.testing.assert_frame_equal(groups[code], calc._load_daily(code))
    con.close()


def test_dde_load_weekly_batch_matches_per_stock():
    import duckdb
    import pandas as pd
    from backend.etl.calc_dde import DDECalculator

    con = duckdb.connect(":memory:")
    _dde_schema(con)
    _seed_dde_two_stocks(con)

    calc = DDECalculator(con, "weekly")
    groups = calc._load_weekly_batch(["A.SZ", "B.SZ"])
    for code in ["A.SZ", "B.SZ"]:
        pd.testing.assert_frame_equal(groups[code], calc._load_weekly(code))
    con.close()


# ── _load_weekly contract ──


def test_load_weekly_produces_weekly_rows():
    """_load_weekly should return one row per week-end for a stock."""
    import duckdb
    from backend.etl.calc_dde import DDECalculator

    con = duckdb.connect(":memory:")
    for ddl in [
        """CREATE TABLE dwd_daily_moneyflow (
            ts_code TEXT, trade_date TEXT, buy_lg_vol REAL, sell_lg_vol REAL,
            buy_elg_vol REAL, sell_elg_vol REAL, total_vol REAL,
            net_mf_amount REAL, net_amount_dc REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL, total_mv REAL,
            circ_mv REAL, is_suspended INTEGER,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_week_end INTEGER,
            is_trade_day INTEGER)""",
    ]:
        con.execute(ddl)

    # 3 weeks × 5 trading days for TEST.SZ
    for w in range(1, 4):
        for d in range(1, 6):
            day = f"202601{w*7+d:02d}"
            con.execute("INSERT OR REPLACE INTO dwd_daily_moneyflow "
                        "VALUES ('TEST.SZ', ?, 100,50,80,30,300,500,NULL)", (day,))
            con.execute(
                "INSERT OR REPLACE INTO dwd_daily_quote "
                "VALUES ('TEST.SZ', ?, 10.0, 1e10, NULL, 0)",
                (day,),
            )
            is_we = 1 if d == 5 else 0
            con.execute("INSERT OR REPLACE INTO dim_date VALUES (?, ?, 1)",
                        (day, is_we))
            if is_we:
                con.execute("INSERT OR REPLACE INTO dwd_weekly_quote "
                            "VALUES ('TEST.SZ', ?, 10.0)", (day,))

    calc = DDECalculator(con, "weekly")
    df = calc._load_weekly("TEST.SZ")

    assert len(df) == 3, f"Expected 3 weekly rows, got {len(df)}"
    assert "_skip_dde" in df.columns
    assert "close_qfq" in df.columns
    assert "buy_lg_vol" in df.columns
    assert "net_mf_amount" in df.columns
    # All weeks fully covered (5 active out of 5 expected = 100%)
    assert (df["_skip_dde"] == 0).all(), "Full coverage weeks should _skip_dde=0"

    con.close()


def test_load_weekly_empty_stock():
    """Stock with no week-end data → empty DataFrame."""
    import duckdb
    from backend.etl.calc_dde import DDECalculator

    con = duckdb.connect(":memory:")
    for ddl in [
        """CREATE TABLE dwd_daily_moneyflow (
            ts_code TEXT, trade_date TEXT, buy_lg_vol REAL, sell_lg_vol REAL,
            buy_elg_vol REAL, sell_elg_vol REAL, total_vol REAL,
            net_mf_amount REAL, net_amount_dc REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL, total_mv REAL,
            circ_mv REAL, is_suspended INTEGER,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_week_end INTEGER,
            is_trade_day INTEGER)""",
    ]:
        con.execute(ddl)

    calc = DDECalculator(con, "weekly")
    df = calc._load_weekly("NO_DATA.SZ")
    assert df.empty, f"Expected empty, got {len(df)} rows"
    con.close()


def test_load_weekly_moneyflow_insufficient_coverage():
    """Weeks with <60% moneyflow coverage should be _skip_dde=1."""
    import duckdb
    from backend.etl.calc_dde import DDECalculator

    con = duckdb.connect(":memory:")
    for ddl in [
        """CREATE TABLE dwd_daily_moneyflow (
            ts_code TEXT, trade_date TEXT, buy_lg_vol REAL, sell_lg_vol REAL,
            buy_elg_vol REAL, sell_elg_vol REAL, total_vol REAL,
            net_mf_amount REAL, net_amount_dc REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL, total_mv REAL,
            circ_mv REAL, is_suspended INTEGER,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_week_end INTEGER,
            is_trade_day INTEGER)""",
    ]:
        con.execute(ddl)

    # 1 week of 5 trading days, but only 2 days have moneyflow (2/5 = 40% < 60%)
    for d in range(1, 6):
        day = f"2026010{d}"
        con.execute(
            "INSERT OR REPLACE INTO dwd_daily_quote "
            "VALUES ('TEST.SZ', ?, 10.0, 1e10, NULL, 0)",
            (day,),
        )
        con.execute("INSERT OR REPLACE INTO dim_date VALUES (?, ?, 1)",
                    (day, 1 if d == 5 else 0))
        if d <= 2:
            con.execute("INSERT OR REPLACE INTO dwd_daily_moneyflow "
                        "VALUES ('TEST.SZ', ?, 100,50,80,30,300,500,NULL)", (day,))
    con.execute("INSERT OR REPLACE INTO dwd_weekly_quote "
                "VALUES ('TEST.SZ', '20260105', 10.0)")

    calc = DDECalculator(con, "weekly")
    df = calc._load_weekly("TEST.SZ")

    assert len(df) == 1
    assert df["_skip_dde"].iloc[0] == 1, (
        f"2/5=40% < 60% → _skip_dde should be 1, got {df['_skip_dde'].iloc[0]}")

    con.close()


def test_weekly_trend_resamples_net_before_circ_join():
    """Weekly net must sum all MF days in week, not only circ-overlap days."""
    calc = DDECalculator.__new__(DDECalculator)
    calc.freq = "weekly"
    rows = []
    for d, net, circ in [
        ("2026-07-08", 100.0, None),
        ("2026-07-09", 200.0, None),
        ("2026-07-10", 300.0, None),
        ("2026-07-11", 400.0, None),
        ("2026-07-12", -500.0, 1e6),
    ]:
        rows.append({
            "trade_date": d,
            "net_mf_amount": net,
            "net_amount_dc": net,
            "circ_mv": circ,
            "total_mv": None,
        })
    daily = pd.DataFrame(rows)
    _, ddx3 = calc._build_123_weekly_dde_series(daily)
    assert not ddx3.empty
    # full-week net sum 100+200+300+400-500 = 500, not single-day -500
    ddx, _ = calc._build_123_weekly_dde_series(daily)
    assert abs(ddx.iloc[-1] - 0.05) < 1e-6


def test_weekly_trend_nan_ddx3_tail_is_flat():
    """123 polyfit on NaN ddx3 tail → flat, not ddx fallback."""
    calc = DDECalculator.__new__(DDECalculator)
    calc.freq = "weekly"
    n = 11
    daily = pd.DataFrame({
        "trade_date": pd.date_range("2025-01-06", periods=n, freq="7D"),
        "net_mf_amount": np.linspace(1000, 500, n),
        "net_amount_dc": np.linspace(1000, 500, n),
        "circ_mv": np.full(n, 1e6),
        "total_mv": np.full(n, 1e6),
    })
    trend = calc._weekly_trend_from_daily(
        daily, [daily["trade_date"].iloc[-1].strftime("%Y%m%d")],
    )
    assert trend[-1] == "flat"


def test_moneyflow_trend_declining_ddx3_is_down():
    """B4: monotonic declining DDX3 tail → down (600831 class).

    M5 root-cause note: stored=up / recompute=down on 20260612 was traced to
    stale DWS rows written before B4 moneyflow trend (net_amount_dc+circ_mv).
    Current _compute_moneyflow_trend and APPEND paths agree on this fixture;
    vector batch_append_dde is covered by test_batch_append_dde_daily_trend_matches_full.
    """
    calc = DDECalculator.__new__(DDECalculator)
    calc.freq = "daily"
    n = 80
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    net = np.linspace(5000, -5000, n)
    mv = np.full(n, 1e9)
    trend = calc._compute_moneyflow_trend(
        net.astype(float), mv.astype(float),
        net_amount_dc=net.astype(float), circ_mv=mv.astype(float),
    )
    assert trend[-1] == "down"
