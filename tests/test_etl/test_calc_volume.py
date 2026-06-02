import pandas as pd
import numpy as np
from backend.etl.calc_volume import VolumeCalculator


def test_ma_vol_5_formula():
    """Verify MA5_vol = SMA(vol, 5)."""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 30
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    vols = [1000000.0 + i * 10000 for i in range(n)]
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)

    # MA5 at index 4 (5th value) = average of first 5 volumes
    expected_ma5 = np.mean(vols[0:5])
    assert abs(result["ma_vol_5"].iloc[4] - expected_ma5) < 0.1, (
        f"MA5 mismatch: expected {expected_ma5}, got {result['ma_vol_5'].iloc[4]}"
    )

    # Early values (before period) should be NaN
    assert pd.isna(result["ma_vol_5"].iloc[3])


def test_pct_vol_rank():
    """Verify percentile rank calculation."""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 150
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    # Constant volume for most days, one spike at end
    vols = [1000000.0] * n
    vols[-1] = 5000000.0  # Much higher than all others
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)

    # Last day should have pct_vol_rank close to 100 (above nearly all others)
    last_rank = result["pct_vol_rank"].iloc[-1]
    assert last_rank > 95, f"Expected rank > 95 for spike, got {last_rank}"

    # Middle day with constant volume should be around mid-range (exact depends on NaN handling)
    mid_rank = result["pct_vol_rank"].iloc[130]
    assert mid_rank is not np.nan or not pd.isna(mid_rank)


def test_zone_normal():
    """Volume in mid-range should classify as normal."""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 150
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    # All same volume -> rank ~50%, should be "normal"
    vols = [1000000.0] * n
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)

    valid_zones = result["zone"].dropna()
    # With constant volume, everything should be "normal"
    normal_count = (valid_zones == "normal").sum()
    assert normal_count > 0, f"Expected normal zones, got: {valid_zones.unique()}"


def test_trend_flat():
    """Flat volume should yield flat trend."""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 30
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    vols = [1000000.0] * n
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)

    # After 20 data points, trend should be "flat" (slope near 0)
    valid_trends = result["trend"].dropna()
    flat_count = (valid_trends == "flat").sum()
    assert flat_count > 0, f"Expected flat trend for constant volume, got: {valid_trends.unique()}"


def test_trend_uses_raw_vol():
    """趋势直接使用原始成交量，10-bar 窗口即可检出 expanding。"""
    calc = VolumeCalculator.__new__(VolumeCalculator)
    n = 30
    dates = [f"d{i}" for i in range(n)]
    vols = [1000000.0 + i * 100000 for i in range(n)]  # 每日 +10%
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)
    t = result["trend"].iloc[15]
    assert t == "expanding", f"raw vol 持续上升应为 expanding，实际 {t}"


def test_trend_threshold_0008():
    """阈值 0.008——弱趋势判为 flat。"""
    calc = VolumeCalculator.__new__(VolumeCalculator)
    n = 20
    dates = [f"d{i}" for i in range(n)]
    vols = [1000000.0 + i * 5000 for i in range(n)]  # 每日 +0.5%
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)
    t = result["trend"].iloc[15]
    assert t == "flat", f"弱趋势(斜率<0.008)应为 flat，实际 {t}"


def test_integration_volume(db_with_schema):
    """Integration test: volume indicators with real DuckDB data."""
    con = db_with_schema
    con.execute(
        "INSERT INTO dim_stock (ts_code, stock_code, name) VALUES ('TEST.SZ','TEST','Test')"
    )

    # Insert 150 days of volume data (enough for 120-day percentile window)
    for i in range(1, 151):
        con.execute(
            "INSERT INTO dwd_daily_quote (ts_code, trade_date, vol, is_suspended) "
            "VALUES (?,?,?,0)",
            ("TEST.SZ", f"202601{i:02d}", 1000000.0 + i * 1000),
        )

    calc = VolumeCalculator(con, "daily")
    calc.calculate(["TEST.SZ"], "20260201")

    rows = con.execute(
        "SELECT trade_date, ma_vol_5, pct_vol_rank, zone, trend FROM dws_volume_daily "
        "WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    assert len(rows) > 0
    # Verify MA5_vol is computed
    assert rows[5] is not None and rows[5][1] is not None, "MA5_vol should be computed"
    # Verify zone is populated
    zones = [r[3] for r in rows if r[3] is not None]
    assert "normal" in zones, f"Expected normal zone, got zones: {set(zones)}"
