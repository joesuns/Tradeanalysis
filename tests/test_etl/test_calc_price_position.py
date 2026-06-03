import pandas as pd
import numpy as np
from backend.etl.calc_price_position import PricePositionCalculator


def test_price_position_60d():
    """price_position_60d = (close - 60d_low) / (60d_high - 60d_low) * 100"""
    calc = PricePositionCalculator.__new__(PricePositionCalculator)
    calc.freq = "daily"

    n = 120
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    # Prices: 60 bars at 10, dip to 5, rise to 15
    closes = ([10.0] * 60 + list(np.linspace(10, 5, 10)) +
              [5.0] * 10 + list(np.linspace(5, 15, 40)))
    closes = closes[:n]
    df = pd.DataFrame({"trade_date": dates, "close_qfq": closes})

    result = calc._compute_positions(df)

    # At the dip (index 79, close=5): should be near 0 (bottom of 60d range)
    # Window starts at index 59, so index 79 is the last bar at price=5
    pp60 = result["price_position_60d"].iloc[79]
    assert pp60 is not None and pp60 < 10, f"At dip, price_position_60d should be < 10, got {pp60}"

    # At the peak (index 119, close=15): should be near 100
    pp60_end = result["price_position_60d"].iloc[119]
    assert pp60_end is not None and pp60_end > 90, f"At peak, price_position_60d should be > 90, got {pp60_end}"


def test_price_position_boundaries():
    """price_position is always in [0, 100]."""
    calc = PricePositionCalculator.__new__(PricePositionCalculator)
    calc.freq = "daily"

    n = 100
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    np.random.seed(42)
    closes = np.cumsum(np.random.randn(n)) + 100
    df = pd.DataFrame({"trade_date": dates, "close_qfq": closes})
    result = calc._compute_positions(df)

    for col in ["price_position_60d", "price_position_120d", "price_position_250d"]:
        valid = result[col].dropna()
        assert (valid >= 0).all(), f"{col} has values < 0"
        assert (valid <= 100).all(), f"{col} has values > 100"


def test_price_position_all_windows():
    """Each window column exists and has values after enough data."""
    calc = PricePositionCalculator.__new__(PricePositionCalculator)
    calc.freq = "daily"

    n = 300
    dates = [f"2026{i:03d}" for i in range(1, n + 1)]
    closes = [10.0 + i * 0.02 for i in range(n)]  # steadily rising
    df = pd.DataFrame({"trade_date": dates, "close_qfq": closes})
    result = calc._compute_positions(df)

    # After 60 rows: price_position_60d should start having values
    assert result["price_position_60d"].iloc[59] is not None, "60d column should have value at index 59"
    # 120d and 250d should be NaN until enough data
    assert pd.isna(result["price_position_120d"].iloc[59]), "120d should be NaN with only 60 rows"
    # After 120 rows: 120d column fills in
    assert result["price_position_120d"].iloc[119] is not None, "120d column should have value at index 119"
    # After 250 rows: 250d column fills in
    assert result["price_position_250d"].iloc[249] is not None, "250d column should have value at index 249"


def test_integration_price_position(db_with_schema):
    """Integration: full calculate + INSERT with real DuckDB."""
    con = db_with_schema
    con.execute(
        "INSERT INTO dim_stock (ts_code, stock_code, name) VALUES ('TEST.SZ','TEST','Test')"
    )

    n = 150
    for i in range(1, n + 1):
        price = 10.0 + i * 0.1
        con.execute(
            "INSERT INTO dwd_daily_quote (ts_code, trade_date, close_qfq, is_suspended) "
            "VALUES (?,?,?,0)",
            ("TEST.SZ", f"202601{i:02d}", price),
        )

    calc = PricePositionCalculator(con, "daily")
    calc.calculate(["TEST.SZ"], "20260201")

    rows = con.execute(
        "SELECT trade_date, price_position_60d, price_position_120d, price_position_250d "
        "FROM dws_price_position_daily WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    assert len(rows) > 0, "Should have rows inserted"
    # Last row (peak price) should have high position values
    last = rows[-1]
    pp60 = last[1]
    pp120 = last[2]
    assert pp60 is not None and pp60 > 90, f"Last row price_position_60d should be high, got {pp60}"
    if pp120 is not None:
        assert pp120 > 90, f"Last row price_position_120d should be high, got {pp120}"
