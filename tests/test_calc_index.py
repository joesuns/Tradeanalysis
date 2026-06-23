"""Tests for index calc pipeline."""
import pytest


@pytest.fixture
def con(db_with_schema):
    """DuckDB connection with full DDL schema applied."""
    return db_with_schema


def test_index_macd_calculator_table_names():
    """IndexMACDCalculator uses index-specific tables."""
    from backend.etl.calc_index import IndexMACDCalculator
    calc = IndexMACDCalculator(None, freq="daily")
    assert calc.src_table == "dwd_index_daily"
    assert calc.dws_table == "dws_index_macd_daily"
    calc_w = IndexMACDCalculator(None, freq="weekly")
    assert calc_w.src_table == "dwd_index_weekly"
    assert calc_w.dws_table == "dws_index_macd_weekly"


def test_index_ma_calculator_table_names():
    """IndexMACalculator uses index-specific tables."""
    from backend.etl.calc_index import IndexMACalculator
    calc = IndexMACalculator(None, freq="daily")
    assert calc.src_table == "dwd_index_daily"
    assert calc.dws_table == "dws_index_ma_daily"


def test_index_volume_calculator_table_names():
    """IndexVolumeCalculator uses index-specific tables + SIGNATURE_COLS."""
    from backend.etl.calc_index import IndexVolumeCalculator
    calc = IndexVolumeCalculator(None, freq="daily")
    assert calc.src_table == "dwd_index_daily"
    assert calc.dws_table == "dws_index_volume_daily"
    assert calc.SIGNATURE_COLS == ["close_qfq", "vol"]

    calc_w = IndexVolumeCalculator(None, freq="weekly")
    assert calc_w.src_table == "dwd_index_weekly"
    assert calc_w.dws_table == "dws_index_volume_weekly"
    assert calc_w.SIGNATURE_COLS == ["close_qfq", "vol", "active_days"]


def test_calc_index_pipeline_no_tracked_indices(con):
    """calc_index_pipeline returns empty dict when dim_index is empty."""
    from backend.etl.calc_index import calc_index_pipeline
    # dim_index should be empty in fresh test DB
    stats = calc_index_pipeline(con, "20260601")
    assert stats == {}


def test_calc_index_pipeline_with_data(con):
    """calc_index_pipeline computes MACD/MA/Volume for seeded index data."""
    import pandas as pd
    from backend.etl.calc_index import calc_index_pipeline

    # Seed dim_index
    con.execute("""
        INSERT OR REPLACE INTO dim_index (ts_code, name, is_active)
        VALUES ('000001.SH', '上证综指', 1)
    """)

    # Seed enough bars of dwd_index_daily for MACD min 27
    base = pd.Timestamp("2026-04-01")
    for i in range(40):
        dt = base + pd.DateOffset(days=i)
        # Only insert weekdays (approximate trading days)
        if dt.dayofweek >= 5:
            continue
        close = 3300.0 + i * 2.0
        date_str = dt.strftime("%Y%m%d")
        con.execute("""
            INSERT OR REPLACE INTO dwd_index_daily
                (ts_code, trade_date, close, open, high, low, close_qfq, vol, amount, is_suspended)
            VALUES ('000001.SH', ?, ?, ?, ?, ?, ?, 100000, 500000, 0)
        """, [date_str, close, close - 5, close + 10, close - 10, close])

    stats = calc_index_pipeline(con, "20260520")
    assert stats["index_macd_daily"]["calculated"] > 0
    assert stats["index_ma_daily"]["calculated"] > 0
    assert stats["index_volume_daily"]["calculated"] > 0

    # Verify MACD DWS output
    rows = con.execute("""
        SELECT COUNT(*) FROM dws_index_macd_daily
        WHERE ts_code = '000001.SH'
    """).fetchone()[0]
    assert rows > 0

    # Verify key columns populated via latest view
    last = con.execute("""
        SELECT dif, dea, macd_bar, zone FROM v_dws_index_macd_daily_latest
        WHERE ts_code='000001.SH' ORDER BY trade_date DESC LIMIT 1
    """).fetchone()
    assert last is not None
    assert last[3] in ("bull", "bear")  # zone
