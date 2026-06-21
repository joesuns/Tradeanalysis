"""B4 column registry: extract 14 cols; hard gate 10 cols vs 123.

``ma_alignment`` / ``w_ma_alignment`` — TA-native MA5/MA10 regime (not 123).
``dde_alert`` / ``w_dde_alert`` — TA-native 2-bar DDX2 inflection (not 123 5-bar).
"""
from typing import Dict, List

# Tradeanalysis DWS raw enums (not Excel Chinese labels)
B4_DAILY_FIELDS: List[str] = [
    "macd_trend",
    "macd_zone",
    "macd_alert",
    "ma_alignment",
    "dde_trend",
    "dde_alert",
    "vol_trend",
]

B4_WEEKLY_FIELDS: List[str] = list(B4_DAILY_FIELDS)

B4_WEEKLY_PREFIX = "w_"

# TA-native with semantics incompatible with 123 — excluded from hard-gate diff
# (dde_alert: TA 2-bar ≠ 123 5-bar; ma_alignment: TA regime ≠ 123)
B4_SOFT_DAILY_FIELDS: List[str] = ["ma_alignment", "dde_alert"]

B4_HARD_DAILY_FIELDS: List[str] = [
    f for f in B4_DAILY_FIELDS if f not in B4_SOFT_DAILY_FIELDS
]

B4_HARD_WEEKLY_FIELDS: List[str] = list(B4_HARD_DAILY_FIELDS)

B4_ALL_FIELDS: List[str] = B4_DAILY_FIELDS + [
    f"{B4_WEEKLY_PREFIX}{f}" for f in B4_WEEKLY_FIELDS
]

B4_HARD_ALL_FIELDS: List[str] = B4_HARD_DAILY_FIELDS + [
    f"{B4_WEEKLY_PREFIX}{f}" for f in B4_HARD_WEEKLY_FIELDS
]

B4_SOFT_ALL_FIELDS: List[str] = B4_SOFT_DAILY_FIELDS + [
    f"{B4_WEEKLY_PREFIX}{f}" for f in B4_SOFT_DAILY_FIELDS
]

# 123 column names → Tradeanalysis field (daily)
MAP_123_DAILY: Dict[str, str] = {
    "short_macd_trend": "macd_trend",
    "short_macd_signal": "macd_zone",
    "daily_rev_macd_hist_turn": "macd_alert",
    "short_ma_regime": "ma_alignment",
    "short_dde_trend": "dde_trend",
    "daily_rev_ddx2_slope_reversal": "dde_alert",
    "short_volume_trend": "vol_trend",
}

# 123 weekly B4: medium_* + weekly_rev_* on same batch_trend_results row
MAP_123_WEEKLY: Dict[str, str] = {
    "medium_macd_trend": "macd_trend",
    "medium_macd_signal": "macd_zone",
    "weekly_rev_macd_hist_turn": "macd_alert",
    "medium_ma_regime": "ma_alignment",
    "medium_dde_trend": "dde_trend",
    "weekly_rev_ddx2_slope_reversal": "dde_alert",
    "medium_volume_trend": "vol_trend",
}


def map_123_daily_col(col_123: str) -> str:
    return MAP_123_DAILY[col_123]


def map_123_weekly_col(col_123: str) -> str:
    return MAP_123_WEEKLY[col_123]


def weekly_field_name(daily_field: str) -> str:
    return f"{B4_WEEKLY_PREFIX}{daily_field}"
