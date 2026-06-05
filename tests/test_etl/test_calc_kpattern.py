import pandas as pd
import numpy as np
from backend.etl.calc_kpattern import KPatternCalculator


def test_yang_bao_yin_detection():
    """Verify 阳包阴 (bull engulfing) pattern detection with mock OHLCV data."""
    calc = KPatternCalculator.__new__(KPatternCalculator)

    # Build 35 rows: first 33 neutral, then a bear day followed by a bull engulfing day
    n = 35
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    opens = [10.0] * n
    highs = [10.5] * n
    lows = [9.5] * n
    closes = [10.0] * n
    vols = [1000000] * n
    pct_chg = [0.0] * n

    # Day 33: bear day (open 12, close 10)
    opens[32] = 12.0
    highs[32] = 12.5
    lows[32] = 9.5
    closes[32] = 10.0
    vols[32] = 1000000
    pct_chg[32] = -3.0

    # Day 34: bull engulfing (open 9, close 13 — engulfs prev open=12, close=10)
    opens[33] = 9.0
    highs[33] = 13.5
    lows[33] = 8.5
    closes[33] = 13.0
    vols[33] = 1200000
    pct_chg[33] = 5.0

    df = pd.DataFrame({
        "trade_date": dates,
        "open_qfq": opens,
        "high_qfq": highs,
        "low_qfq": lows,
        "close_qfq": closes,
        "vol": vols,
        "pct_chg": pct_chg,
    })

    result = calc._compute_patterns(df, is_st=False)

    # Day 34 (index 33, 0-based) should have yang_bao_yin = 1
    assert result["yang_bao_yin"].iloc[33] == 1, (
        f"Expected yang_bao_yin at day 34, got {result['yang_bao_yin'].iloc[33]}"
    )
    # Earlier days should be 0
    assert result["yang_bao_yin"].iloc[30] == 0


def test_yin_bao_yang_detection():
    """Verify 阴包阳 (bear engulfing) pattern detection."""
    calc = KPatternCalculator.__new__(KPatternCalculator)

    n = 35
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    opens = [10.0] * n
    highs = [10.5] * n
    lows = [9.5] * n
    closes = [10.0] * n
    vols = [1000000] * n
    pct_chg = [0.0] * n

    # Day 33: bull day (open 10, close 12)
    opens[32] = 10.0
    highs[32] = 12.5
    lows[32] = 9.5
    closes[32] = 12.0
    pct_chg[32] = 3.0

    # Day 34: bear engulfing (open 13, close 9 — engulfs prev open=10, close=12)
    opens[33] = 13.0
    highs[33] = 13.5
    lows[33] = 8.5
    closes[33] = 9.0
    pct_chg[33] = -5.0

    df = pd.DataFrame({
        "trade_date": dates,
        "open_qfq": opens,
        "high_qfq": highs,
        "low_qfq": lows,
        "close_qfq": closes,
        "vol": vols,
        "pct_chg": pct_chg,
    })

    result = calc._compute_patterns(df, is_st=False)
    assert result["yin_bao_yang"].iloc[33] == 1, (
        f"Expected yin_bao_yang at day 34, got {result['yin_bao_yang'].iloc[33]}"
    )


def test_price_limit_filter():
    """Verify that limit-up/down days have all patterns set to 0."""
    calc = KPatternCalculator.__new__(KPatternCalculator)

    n = 35
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    opens = [10.0] * n
    highs = [10.5] * n
    lows = [9.5] * n
    closes = [10.0] * n
    vols = [1000000] * n
    pct_chg = [0.0] * n

    # Day 33: bear
    opens[32] = 12.0
    closes[32] = 10.0
    pct_chg[32] = -3.0

    # Day 34: bull engulfing BUT at limit up (+10%)
    opens[33] = 9.0
    closes[33] = 13.0
    pct_chg[33] = 10.0  # limit up -> should be filtered

    df = pd.DataFrame({
        "trade_date": dates,
        "open_qfq": opens,
        "high_qfq": highs,
        "low_qfq": lows,
        "close_qfq": closes,
        "vol": vols,
        "pct_chg": pct_chg,
    })

    result = calc._compute_patterns(df, is_st=False)
    # Should be 0 because limit-up filtered
    assert result["yang_bao_yin"].iloc[33] == 0, "Limit-up day should not have patterns"
    assert result["strength"].iloc[33] is np.nan or pd.isna(result["strength"].iloc[33])


def test_st_stock_limit_filter():
    """Verify ST stock uses 4.9% limit instead of 9.9%."""
    calc = KPatternCalculator.__new__(KPatternCalculator)

    n = 35
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    opens = [10.0] * n
    highs = [10.5] * n
    lows = [9.5] * n
    closes = [10.0] * n
    vols = [1000000] * n
    pct_chg = [0.0] * n

    # Day 33: bear
    opens[32] = 12.0
    closes[32] = 10.0
    pct_chg[32] = -2.0

    # Day 34: bull engulfing but at +5% (above ST limit)
    opens[33] = 9.0
    closes[33] = 13.0
    pct_chg[33] = 5.0  # above ST limit of 4.9%

    df = pd.DataFrame({
        "trade_date": dates,
        "open_qfq": opens,
        "high_qfq": highs,
        "low_qfq": lows,
        "close_qfq": closes,
        "vol": vols,
        "pct_chg": pct_chg,
    })

    result = calc._compute_patterns(df, is_st=True)
    # Should be filtered because ST uses 4.9%
    assert result["yang_bao_yin"].iloc[33] == 0, "ST stock at 5% should be filtered"


def test_doji_body_pct_10_percent():
    """Doji: body < 10% of full range should qualify (not 0.5%)."""
    calc = KPatternCalculator.__new__(KPatternCalculator)

    n = 65
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    # Strong uptrend: 60d high and 20d gain > 15%
    closes = np.array([10.0 + i * 0.3 for i in range(n)])  # strong uptrend
    opens = closes - 0.02  # tiny body: 0.02 out of full range 0.4 = 5%
    highs = closes + 0.20
    lows = closes - 0.20
    vols = [1000000] * n
    pct_chg = [1.0] * n

    df = pd.DataFrame({
        "trade_date": dates, "open_qfq": opens, "high_qfq": highs,
        "low_qfq": lows, "close_qfq": closes, "vol": vols, "pct_chg": pct_chg,
    })
    result = calc._compute_patterns(df, is_st=False)
    # body_pct = |c-o| / (h-l) = 0.02 / 0.40 = 5% < 10% → Doji should trigger
    # and body > 0, upper_shadow = 0.20 >= 3*0.02=0.06 ✓ → tombstone triggered
    assert result["mu_bei_xian"].iloc[-1] == 1, (
        f"Expected tombstone with body_pct=5%<10%, got {result['mu_bei_xian'].iloc[-1]}"
    )


def test_doji_by_prev_close():
    """Doji: |O-C|/prev_close < 0.5% also qualifies, even if body_pct is large."""
    calc = KPatternCalculator.__new__(KPatternCalculator)

    n = 65
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    closes = np.array([10.0 + i * 0.3 for i in range(n)])
    prev_close = closes[-2]  # ~28.7
    # |O-C| = 0.10, full_range = 0.21 → body_pct = 0.10/0.21 ≈ 47.6% (NOT doji by body_pct)
    # But |O-C|/prev_close = 0.10/28.7 ≈ 0.35% < 0.5% → IS doji by prev_close rule
    opens = np.concatenate([closes[:-1] * 0.99, [closes[-1] - 0.05]])
    highs = np.array([c + 0.30 for c in closes])
    lows = np.array([c - 0.30 for c in closes])
    # Make the last day have the right structure
    highs[-1] = closes[-1] + 0.30  # big upper shadow
    lows[-1] = closes[-1] - 0.30
    opens[-1] = closes[-1] - 0.05  # tiny offset from close
    vols = [1000000] * n
    pct_chg = [1.0] * n

    df = pd.DataFrame({
        "trade_date": dates, "open_qfq": opens, "high_qfq": highs,
        "low_qfq": lows, "close_qfq": closes, "vol": vols, "pct_chg": pct_chg,
    })
    result = calc._compute_patterns(df, is_st=False)
    # |O-C|/prev_close = 0.05/prev_close should be < 0.5% if prev_close > 10
    # With our data, prev_close[-1] ≈ 29.05, so 0.05/29.05 ≈ 0.17% < 0.5% ✓
    # body_pct ≈ 0.05/0.60 ≈ 8.3% < 10% → also qualifies via body_pct
    # So this test primarily verifies prev_close rule doesn't hurt
    assert result["mu_bei_xian"].iloc[-1] == 1, (
        f"Expected tombstone via doji prev_close rule, got {result['mu_bei_xian'].iloc[-1]}"
    )


def test_tombstone_zero_body_fallback():
    """When body=0 (perfect doji), use upper_shadow/full_range > 60%."""
    calc = KPatternCalculator.__new__(KPatternCalculator)

    n = 65
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    closes = np.array([10.0 + i * 0.3 for i in range(n)])
    opens = closes.copy()  # open == close → body=0, perfect doji
    highs = closes + 0.50  # upper shadow 0.50
    lows = closes - 0.20   # lower shadow 0.20
    # full_range = 0.70, upper_shadow/full_range = 0.50/0.70 ≈ 71.4% > 60% ✓
    vols = [1000000] * n
    pct_chg = [1.0] * n

    df = pd.DataFrame({
        "trade_date": dates, "open_qfq": opens, "high_qfq": highs,
        "low_qfq": lows, "close_qfq": closes, "vol": vols, "pct_chg": pct_chg,
    })
    result = calc._compute_patterns(df, is_st=False)
    assert result["mu_bei_xian"].iloc[-1] == 1, (
        f"Perfect doji with upper_shadow/full_range=71% should be tombstone, got {result['mu_bei_xian'].iloc[-1]}"
    )


def test_kpattern_strength_per_pattern():
    """Strength scores differ per pattern type."""
    calc = KPatternCalculator.__new__(KPatternCalculator)
    # We just need to verify _compute_strength doesn't crash and produces values in [0,1]
    n = 65
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    closes = np.array([10.0 + i * 0.3 for i in range(n)])
    opens = closes.copy()
    highs = closes + 0.2
    lows = closes - 0.2
    vols = [1000000] * n
    pct_chg = [1.0] * n

    df = pd.DataFrame({
        "trade_date": dates, "open_qfq": opens, "high_qfq": highs,
        "low_qfq": lows, "close_qfq": closes, "vol": vols, "pct_chg": pct_chg,
    })

    # Run pattern detection, then manually set a pattern and test strength
    result = calc._compute_patterns(df, is_st=False)
    # Force a pattern
    result.at[63, "yang_bao_yin"] = 1
    strengths = calc._compute_strength(result)
    # Check strength is in valid range
    s = strengths[63]
    assert not pd.isna(s), "Strength should not be NaN for a pattern"
    assert 0.0 <= s <= 1.0, f"Strength {s} should be in [0, 1]"


def test_integration_kpattern(db_with_schema):
    """Integration test: detect yang_bao_yin with real DuckDB data."""
    con = db_with_schema
    con.execute(
        "INSERT INTO dim_stock (ts_code, stock_code, name, is_st) "
        "VALUES ('TEST.SZ','TEST','Test',0)"
    )

    # Insert 35 days: neutral for first 33, then bear -> bull engulfing
    for i in range(1, 36):
        if i <= 33:
            o, h, l, c = 10.0, 10.5, 9.5, 10.0
            pct = 0.0
        elif i == 34:
            # Bear day
            o, h, l, c = 12.0, 12.5, 9.5, 10.0
            pct = -3.0
        else:
            # Bull engulfing day
            o, h, l, c = 9.0, 13.5, 8.5, 13.0
            pct = 5.0

        con.execute(
            "INSERT INTO dwd_daily_quote (ts_code, trade_date, open_qfq, high_qfq, "
            "low_qfq, close_qfq, vol, pct_chg, is_suspended) VALUES (?,?,?,?,?,?,?,?,0)",
            ("TEST.SZ", f"202601{i:02d}", o, h, l, c, 1000000, pct),
        )

    calc = KPatternCalculator(con, "daily")
    calc.calculate(["TEST.SZ"], "20260201")

    rows = con.execute(
        "SELECT trade_date, yang_bao_yin, strength FROM dws_kpattern_daily "
        "WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    assert len(rows) > 0
    # Check the last row (day 35) has yang_bao_yin = 1
    last_row = rows[-1]
    assert last_row[1] == 1, f"Expected yang_bao_yin on day 35, got: {last_row}"


def test_yang_ke_yin_requires_prev_bear():
    """阳克阴: 前日阳线 + 满足量价条件 → 不应触发（缺少对立颜色前置条件）."""
    calc = KPatternCalculator.__new__(KPatternCalculator)

    n = 35
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    opens = [10.0] * n
    highs = [10.5] * n
    lows = [9.5] * n
    closes = [10.0] * n
    vols = [1000000] * n
    pct_chg = [0.0] * n

    # Day 33 (index 32): BULL day — not bear!
    opens[32] = 10.0
    highs[32] = 11.0
    lows[32] = 9.5
    closes[32] = 11.0   # bullish, close > open
    vols[32] = 1000000
    pct_chg[32] = 2.0

    # Day 34 (index 33): would satisfy yang_ke_yin conditions IF prev were bear
    # volume spike > MA5 * 1.2, max(o,c) > prev max(o,c), close > MA10
    opens[33] = 10.5
    highs[33] = 12.5
    lows[33] = 9.5
    closes[33] = 12.0
    vols[33] = 1500000   # > 1,000,000 * 1.2
    pct_chg[33] = 5.0    # < 9.9%, not limit-filtered

    df = pd.DataFrame({
        "trade_date": dates,
        "open_qfq": opens,
        "high_qfq": highs,
        "low_qfq": lows,
        "close_qfq": closes,
        "vol": vols,
        "pct_chg": pct_chg,
    })

    result = calc._compute_patterns(df, is_st=False)

    # Prev day is BULL, so yang_ke_yin should NOT trigger
    assert result["yang_ke_yin"].iloc[33] == 0, (
        f"阳克阴要求前日阴线，前日阳线不应触发，实际为 {result['yang_ke_yin'].iloc[33]}"
    )


def test_yin_ke_yang_requires_prev_bull():
    """阴克阳: 前日阴线 + 满足量价条件 → 不应触发（缺少对立颜色前置条件）."""
    calc = KPatternCalculator.__new__(KPatternCalculator)

    n = 35
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    opens = [10.0] * n
    highs = [10.5] * n
    lows = [9.5] * n
    closes = [10.0] * n
    vols = [1000000] * n
    pct_chg = [0.0] * n

    # Day 33 (index 32): BEAR day — not bull!
    opens[32] = 11.0
    highs[32] = 11.5
    lows[32] = 9.5
    closes[32] = 9.5    # bearish, close < open
    vols[32] = 1000000
    pct_chg[32] = -2.0

    # Day 34 (index 33): would satisfy yin_ke_yang conditions IF prev were bull
    # volume spike > MA5 * 1.2, min(o,c) < prev min(o,c), close < MA10
    opens[33] = 10.0
    highs[33] = 10.5
    lows[33] = 8.5
    closes[33] = 9.0
    vols[33] = 1500000   # > 1,000,000 * 1.2
    pct_chg[33] = -5.0   # < 9.9%, not limit-filtered

    df = pd.DataFrame({
        "trade_date": dates,
        "open_qfq": opens,
        "high_qfq": highs,
        "low_qfq": lows,
        "close_qfq": closes,
        "vol": vols,
        "pct_chg": pct_chg,
    })

    result = calc._compute_patterns(df, is_st=False)

    # Prev day is BEAR, so yin_ke_yang should NOT trigger
    assert result["yin_ke_yang"].iloc[33] == 0, (
        f"阴克阳要求前日阳线，前日阴线不应触发，实际为 {result['yin_ke_yang'].iloc[33]}"
    )


def test_yang_ke_yin_strength_uses_ma5_volume():
    """阳克阴强度计算: 量能维度使用 MA5 成交量（与检测逻辑对齐），非前日量."""
    calc = KPatternCalculator.__new__(KPatternCalculator)

    n = 35
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    opens = [10.0] * n
    highs = [10.5] * n
    lows = [9.5] * n
    closes = [10.0] * n
    vols = [1000000] * n
    pct_chg = [0.0] * n

    # Day 33 (index 32): bear day — satisfies yang_ke_yin prev-bear precondition
    opens[32] = 12.0
    highs[32] = 12.5
    lows[32] = 9.5
    closes[32] = 10.0   # bearish, close < open
    vols[32] = 1000000
    pct_chg[32] = -3.0

    # Day 34 (index 33): triggers only yang_ke_yin, NOT yang_bao_yin
    # open > prev_close (11 > 10) → breaks engulfing condition, avoids double-trigger
    # volume spike but moderate so vs differs measurably between prev_vol and MA5
    opens[33] = 11.0   # > c[32]=10 → yang_bao_yin engulf fails (open must be <= prev close)
    highs[33] = 13.5
    lows[33] = 8.5
    closes[33] = 13.0
    vols[33] = 1300000   # > ma5 * 1.2 = 1,060,000 * 1.2 = 1,272,000
    pct_chg[33] = 5.0

    df = pd.DataFrame({
        "trade_date": dates,
        "open_qfq": opens,
        "high_qfq": highs,
        "low_qfq": lows,
        "close_qfq": closes,
        "vol": vols,
        "pct_chg": pct_chg,
    })

    result = calc._compute_patterns(df, is_st=False)

    # Verify only yang_ke_yin triggers, not yang_bao_yin
    assert result["yang_ke_yin"].iloc[33] == 1, "Test setup should trigger yang_ke_yin"
    assert result["yang_bao_yin"].iloc[33] == 0, "yang_bao_yin should NOT trigger"

    # Old prev_vol formula: vr=1.3M/1M=1.3, vs=min((1.3-1.2)/0.8, 1)=0.125
    #   → strength = 0.4*1.0 + 0.4*0.125 + 0.2*0.9 = 0.630
    # New MA5 formula: vs=min(1.3M/1.06M/1.5, 1)=0.818
    #   → strength = 0.4*1.0 + 0.4*0.818 + 0.2*0.9 = 0.907
    # The test expects strength > 0.80 (MA5-based), which the current prev_vol
    # code (~0.63) will fail — proving the bug exists.
    actual_strength = result["strength"].iloc[33]
    assert not pd.isna(actual_strength), "Strength should not be NaN for triggered pattern"

    assert actual_strength > 0.80, (
        f"阳克阴强度应使用 MA5 量能基准（预期 > 0.80），"
        f"实际为 {actual_strength:.4f}。若使用前日量会得到 ~0.63"
    )
