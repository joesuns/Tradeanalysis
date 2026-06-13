"""B4 column registry and 123 mapping."""
from backend.b4_gate.columns import (
    B4_ALL_FIELDS,
    B4_DAILY_FIELDS,
    B4_HARD_ALL_FIELDS,
    B4_HARD_DAILY_FIELDS,
    B4_SOFT_ALL_FIELDS,
    B4_SOFT_DAILY_FIELDS,
    B4_WEEKLY_FIELDS,
    MAP_123_DAILY,
    MAP_123_WEEKLY,
    map_123_daily_col,
    map_123_weekly_col,
)


def test_b4_field_count():
    assert len(B4_DAILY_FIELDS) == 7
    assert len(B4_WEEKLY_FIELDS) == 7
    assert len(B4_ALL_FIELDS) == 14


def test_b4_hard_gate_excludes_ma_alignment():
    assert B4_SOFT_DAILY_FIELDS == ["ma_alignment"]
    assert len(B4_HARD_DAILY_FIELDS) == 6
    assert len(B4_HARD_ALL_FIELDS) == 12
    assert "ma_alignment" not in B4_HARD_ALL_FIELDS
    assert "w_ma_alignment" in B4_SOFT_ALL_FIELDS
    assert "w_ma_alignment" not in B4_HARD_ALL_FIELDS


def test_123_daily_macd_trend_maps_to_macd_trend():
    assert map_123_daily_col("short_macd_trend") == "macd_trend"


def test_123_weekly_uses_medium_columns():
    assert map_123_weekly_col("medium_macd_trend") == "macd_trend"
    assert MAP_123_WEEKLY["weekly_rev_macd_hist_turn"] == "macd_alert"
    assert len(MAP_123_DAILY) == 7
    assert len(MAP_123_WEEKLY) == 7
