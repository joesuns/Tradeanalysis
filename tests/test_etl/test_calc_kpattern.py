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
