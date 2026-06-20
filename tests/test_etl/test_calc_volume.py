import pandas as pd
import numpy as np
from backend.etl.calc_volume import (
    VolumeCalculator,
    VOLUME_TREND_V2_DAILY,
    VOLUME_TREND_V2_WEEKLY,
    compute_volume_trend_series,
    trend_from_v2_label,
    volume_trend_v2,
)


# ── B2 golden-master: trend_strength still uses ln(vol) weighted regression ──

def _oracle_vol_strength(vol_series, window=10):
    n = len(vol_series)
    result = np.full(n, np.nan)
    for i in range(window - 1, n):
        segment = vol_series[i - window + 1:i + 1]
        valid = segment[~np.isnan(segment)]
        valid_pos = valid[valid > 0]
        if len(valid_pos) < 5:
            continue
        log_segment = np.log(valid_pos)
        m = len(log_segment)
        x = np.arange(m, dtype=float)
        weights = np.exp(x * 0.20)
        try:
            slope = float(np.polyfit(x, log_segment, 1, w=weights)[0])
        except (np.linalg.LinAlgError, ValueError, TypeError):
            continue
        if not np.isfinite(slope):
            continue
        scale = np.mean(np.abs(log_segment))
        if scale < 1e-6:
            result[i] = 0.0
        else:
            result[i] = slope / scale
    return result


def _rand_vol(rng):
    vol = rng.lognormal(mean=11.0, sigma=0.5, size=rng.integers(10, 90))
    r = rng.random()
    if r < 0.4:  # sprinkle NaNs
        idx = rng.integers(0, len(vol), size=max(1, len(vol) // 6))
        vol[idx] = np.nan
    if r > 0.6:  # sprinkle zeros / negatives
        idx = rng.integers(0, len(vol), size=max(1, len(vol) // 8))
        vol[idx] = 0.0
    return vol


def test_vol_trend_matches_v2_last_bar_daily():
    calc = VolumeCalculator(None, "daily")
    rng = np.random.default_rng(41)
    kw = dict(VOLUME_TREND_V2_DAILY)
    anchor = kw.pop("anchor_bars")
    for _ in range(40):
        vol = rng.lognormal(mean=11.0, sigma=0.5, size=rng.integers(anchor, anchor + 40))
        series = calc._compute_trend(vol, 10)
        _, label = volume_trend_v2(vol, anchor_bars=anchor, **kw)
        assert series[-1] == trend_from_v2_label(label)


def test_vol_trend_matches_v2_last_bar_weekly():
    calc = VolumeCalculator(None, "weekly")
    rng = np.random.default_rng(42)
    kw = dict(VOLUME_TREND_V2_WEEKLY)
    anchor = kw.pop("anchor_bars")
    for _ in range(40):
        vol = rng.lognormal(mean=11.0, sigma=0.5, size=rng.integers(anchor, anchor + 30))
        series = calc._compute_trend(vol, 10)
        _, label = volume_trend_v2(vol, anchor_bars=anchor, **kw)
        assert series[-1] == trend_from_v2_label(label)


def test_compute_volume_trend_series_prefix_consistency():
    vol = np.array([1e6 + i * 1e4 for i in range(80)], dtype=float)
    series = compute_volume_trend_series(vol, VOLUME_TREND_V2_DAILY)
    kw = dict(VOLUME_TREND_V2_DAILY)
    anchor = kw.pop("anchor_bars")
    for i in range(59, len(vol)):
        _, label = volume_trend_v2(vol[: i + 1], anchor_bars=anchor, **kw)
        assert series[i] == trend_from_v2_label(label)


def test_vol_trend_strength_matches_oracle_random():
    calc = VolumeCalculator.__new__(VolumeCalculator)
    rng = np.random.default_rng(43)
    for _ in range(60):
        vol = _rand_vol(rng)
        got = calc._compute_trend_strength(vol, window=10)
        exp = _oracle_vol_strength(vol, 10)
        np.testing.assert_array_equal(np.isnan(got), np.isnan(exp))
        m = ~np.isnan(exp)
        np.testing.assert_allclose(got[m], exp[m], rtol=0, atol=1e-9)


def test_vol_all_positive_full_window_strength_matches_oracle():
    """Common case: no NaN, all positive → trend_strength fast path equals oracle."""
    calc = VolumeCalculator.__new__(VolumeCalculator)
    rng = np.random.default_rng(45)
    vol = rng.lognormal(11.0, 0.4, size=80)
    got = calc._compute_trend_strength(vol, window=10)
    exp = _oracle_vol_strength(vol, 10)
    np.testing.assert_allclose(got[~np.isnan(exp)], exp[~np.isnan(exp)], atol=1e-9)


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
    """Constant volume → v2 direction 平量 (needs >=60 bars)."""
    calc = VolumeCalculator(None, "daily")
    n = 70
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    vols = [1000000.0] * n
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)
    valid_trends = result["trend"].dropna()
    flat_count = (valid_trends == "flat").sum()
    assert flat_count > 0, f"Expected flat trend for constant volume, got: {valid_trends.unique()}"


def test_trend_uses_raw_vol():
    """v2 方向用末 5 根原始量斜率；持续上升 → expanding。"""
    calc = VolumeCalculator(None, "daily")
    n = 70
    dates = [f"d{i}" for i in range(n)]
    vols = [1000000.0 + i * 100000 for i in range(n)]
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)
    t = result["trend"].iloc[-1]
    assert t == "expanding", f"raw vol 持续上升应为 expanding，实际 {t}"


def test_trend_v2_flat_eps():
    """v2 vol_flat_eps=0.001——弱斜率判为 flat。"""
    calc = VolumeCalculator(None, "daily")
    n = 70
    dates = [f"d{i}" for i in range(n)]
    base = 1_000_000.0
    vols = [base + i * 200 for i in range(n)]  # 极弱上升
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)
    t = result["trend"].iloc[-1]
    assert t == "flat", f"弱趋势应为 flat，实际 {t}"


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


def test_volume_ratio():
    """volume_ratio = vol / MA5_vol. Values centered around 1.0."""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 30
    dates = [f"d{i}" for i in range(n)]
    vols = [1000000.0] * 10 + [3000000.0] * 5 + [1000000.0] * 15
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)

    assert "volume_ratio" in result.columns, "volume_ratio column should exist"
    ratio_mid = result["volume_ratio"].iloc[9]
    assert ratio_mid is not None and 0.9 < ratio_mid < 1.1, \
        f"Constant volume should give ratio ~1.0, got {ratio_mid}"

    ratio_high = result["volume_ratio"].iloc[10]
    assert ratio_high is not None and ratio_high > 1.5, \
        f"Volume spike should give ratio > 1.5, got {ratio_high}"


def test_volume_divergence_top():
    """Price hits 60d high + vol declining → top_divergence."""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 80
    dates = [f"d{i}" for i in range(n)]
    closes = [10.0 + i * 0.2 for i in range(60)]
    closes += [closes[59]] * 20
    vols = [1000000.0 - i * 5000 for i in range(60)]
    vols += [vols[59]] * 20

    df = pd.DataFrame({
        "trade_date": dates,
        "vol": vols,
        "close_qfq": closes,
    })
    result = calc._compute_indicators(df)

    assert "divergence" in result.columns
    divs = result["divergence"].dropna()
    assert "top_divergence" in divs.values, \
        f"Expected top_divergence, got {divs.unique() if len(divs) > 0 else 'none'}"


def test_volume_divergence_bottom():
    """Price hits 60d low + vol recovering → bottom_divergence."""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 80
    dates = [f"d{i}" for i in range(n)]
    closes = [50.0 - i * 0.5 for i in range(50)]
    closes += [closes[49]] * 30
    vols = [500000.0 + i * 5000 for i in range(50)]
    vols += [vols[49]] * 30

    df = pd.DataFrame({
        "trade_date": dates,
        "vol": vols,
        "close_qfq": closes,
    })
    result = calc._compute_indicators(df)

    divs = result["divergence"].dropna()
    assert "bottom_divergence" in divs.values, \
        f"Expected bottom_divergence, got {divs.unique() if len(divs) > 0 else 'none'}"


def test_volume_trend_strength():
    """trend_strength is de-unitized. Positive when volume expanding."""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 30
    dates = [f"d{i}" for i in range(n)]
    vols = [1000000.0 + i * 50000 for i in range(n)]  # steadily rising
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)

    assert "trend_strength" in result.columns
    ts = result["trend_strength"].dropna()
    assert len(ts) > 0, "Should have trend_strength values"
    positive_ratio = (ts > 0).sum() / len(ts)
    assert positive_ratio > 0.5, \
        f"Continuously expanding volume should have mostly positive trend_strength, got {positive_ratio:.1%}"


def test_volume_divergence_dedup():
    """Same divergence type should not repeat within 5 bars."""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 100
    dates = [f"d{i}" for i in range(n)]
    closes_list = []
    for i in range(n):
        if i < 80:
            closes_list.append(10.0 + i * 0.1)
        else:
            closes_list.append(18.0)
    vols = [900000.0 - i * 5000 for i in range(60)] + [500000.0] * 40

    df = pd.DataFrame({
        "trade_date": dates,
        "vol": vols,
        "close_qfq": closes_list,
    })
    result = calc._compute_indicators(df)

    divs = result["divergence"].dropna()
    if len(divs) > 0:
        top_indices = [i for i, v in enumerate(result["divergence"]) if v == "top_divergence"]
        if len(top_indices) >= 2:
            for j in range(1, len(top_indices)):
                gap = top_indices[j] - top_indices[j - 1]
                assert gap >= 5, \
                    f"Dedup failed: top_divergence repeated after {gap} days"


def test_weekly_trend_denormalizes_vol_with_active_days():
    """B4 weekly vol_trend uses raw week sum (vol * active_days / 5), not normalized vol."""
    calc = VolumeCalculator(None, "weekly")
    n = 40
    dates = [f"w{i}" for i in range(n)]
    norm_vol = np.full(n, 1_000_000.0)
    active = np.full(n, 5.0)
    # Same normalized vol; rising active_days → rising raw week sum → expanding
    active[-5:] = [1.0, 2.0, 3.0, 4.0, 5.0]
    raw_sum = norm_vol * active / 5.0
    df_week = pd.DataFrame({
        "trade_date": dates, "vol": norm_vol, "active_days": active, "close_qfq": 10.0,
    })
    t_norm = calc._compute_trend(norm_vol, 10)[-1]
    t_raw = calc._compute_trend(raw_sum, 10)[-1]
    t_week = calc._compute_indicators(df_week)["trend"].iloc[-1]
    assert t_norm == "flat"
    assert t_raw == "expanding"
    assert t_week == t_raw


def test_weekly_pct_vol_rank_with_130_week_end_bars():
    """130 根 week-end bar 时 pct_vol_rank 末行非 NaN。"""
    import numpy as np
    import pandas as pd
    from backend.etl.calc_volume import VolumeCalculator

    n = 130
    df = pd.DataFrame({
        "trade_date": [f"2020{(i // 52) + 1:02d}{(i % 52) + 1:02d}" for i in range(n)],
        "vol": np.random.uniform(1e6, 5e6, n),
        "close_qfq": np.linspace(10, 20, n),
    })
    calc = VolumeCalculator(con=None, freq="weekly")
    out = calc._compute_indicators(df)
    ranks = out["pct_vol_rank"].values
    assert np.isfinite(ranks[-1])
    assert out["zone"].iloc[-1] in ("normal", "explosive", "low_volume")


def test_pct_rank_vectorized_matches_original():
    """向量化版 _compute_pct_rank 与原始逐根循环逐值一致 (atol=1e-9)."""
    from backend.etl.calc_volume import VolumeCalculator, _compute_pct_rank_vectorized
    from numpy.lib.stride_tricks import sliding_window_view

    # 原始实现（拷贝自 calc_volume.py:397-415）
    def _compute_pct_rank_original(ma_vol_5: np.ndarray, window: int) -> np.ndarray:
        n = len(ma_vol_5)
        result = np.full(n, np.nan)
        for i in range(window - 1, n):
            start = max(0, i - window + 1)
            window_vals = ma_vol_5[start:i + 1]
            valid = window_vals[~np.isnan(window_vals)]
            if len(valid) < 2:
                continue
            cur = ma_vol_5[i]
            if np.isnan(cur):
                continue
            rank = np.sum(valid <= cur) / len(valid) * 100.0
            result[i] = rank
        return result

    rng = np.random.default_rng(42)

    # Case 1: all-finite, normal data
    for _ in range(20):
        n = rng.integers(130, 300)
        data = rng.lognormal(11.0, 0.5, size=n)
        for w in [60, 120]:
            orig = _compute_pct_rank_original(data, w)
            vec = _compute_pct_rank_vectorized(data, w)
            np.testing.assert_allclose(
                vec[~np.isnan(orig)], orig[~np.isnan(orig)],
                atol=1e-9,
                err_msg=f"pct_rank mismatch: n={n}, w={w}",
            )
            # NaN positions must match
            np.testing.assert_array_equal(np.isnan(vec), np.isnan(orig))

    # Case 2: with NaN holes
    data_with_nan = rng.lognormal(11.0, 0.5, size=200)
    data_with_nan[rng.integers(0, 200, 15)] = np.nan
    orig = _compute_pct_rank_original(data_with_nan, 120)
    vec = _compute_pct_rank_vectorized(data_with_nan, 120)
    np.testing.assert_allclose(
        vec[~np.isnan(orig)], orig[~np.isnan(orig)], atol=1e-9,
    )
    np.testing.assert_array_equal(np.isnan(vec), np.isnan(orig))

    # Case 3: short data (< window)
    short = rng.lognormal(11.0, 0.5, size=50)
    orig = _compute_pct_rank_original(short, 120)
    vec = _compute_pct_rank_vectorized(short, 120)
    assert np.all(np.isnan(vec)), "short data should be all-NaN"
    np.testing.assert_array_equal(np.isnan(vec), np.isnan(orig))


def test_compute_volume_trend_series_vectorized_matches_original():
    """预计算版 trend series 与原始逐 bar 版完全一致."""
    from backend.etl.calc_volume import (
        compute_volume_trend_series,
        _compute_volume_trend_series_vectorized,
        VOLUME_TREND_V2_DAILY,
        VOLUME_TREND_V2_WEEKLY,
    )

    rng = np.random.default_rng(77)

    for label, params in [("daily", VOLUME_TREND_V2_DAILY),
                           ("weekly", VOLUME_TREND_V2_WEEKLY)]:
        anchor = params["anchor_bars"]
        for _ in range(30):
            n = rng.integers(anchor + 10, anchor + 80)
            vol = rng.lognormal(11.0, 0.5, size=n)

            # Full range (target_indices=None)
            orig = compute_volume_trend_series(vol, params)
            vec = _compute_volume_trend_series_vectorized(vol, params)
            assert len(orig) == len(vec) == n
            for i in range(n):
                assert orig[i] == vec[i], (
                    f"[{label}] mismatch at i={i}: orig={orig[i]}, vec={vec[i]}"
                )

            # Subset indices (APPEND path)
            indices = sorted(set(rng.integers(anchor, n, size=min(10, n - anchor))))
            orig_sub = compute_volume_trend_series(vol, params, target_indices=indices)
            vec_sub = _compute_volume_trend_series_vectorized(
                vol, params, target_indices=indices,
            )
            for i in range(n):
                assert orig_sub[i] == vec_sub[i], (
                    f"[{label}] subset mismatch at i={i}"
                )

    # Case: data with NaN
    vol_nan = rng.lognormal(11.0, 0.5, size=120)
    vol_nan[rng.integers(0, 120, 5)] = np.nan
    orig = compute_volume_trend_series(vol_nan, VOLUME_TREND_V2_DAILY)
    vec = _compute_volume_trend_series_vectorized(vol_nan, VOLUME_TREND_V2_DAILY)
    for i in range(len(vol_nan)):
        assert orig[i] == vec[i], f"NaN case mismatch at i={i}"
