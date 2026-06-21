"""Normalize 123 string labels to Tradeanalysis DWS enum values."""
from typing import Dict, Optional

_NULLISH_123 = frozenset({"无", "无数据", "数据不足", "未知", "-", ""})

MACD_TREND_123_TO_TA: Dict[str, str] = {
    "上升": "up",
    "下降": "down",
    "走平": "flat",
    "持平": "flat",
    "up": "up",
    "down": "down",
    "flat": "flat",
}

# 123 short_macd_signal = crossover_signal (maps to TA turning_point, not zone)
MACD_ZONE_123_TO_TA: Dict[str, str] = {
    "金叉": "golden_cross",
    "死叉": "dead_cross",
    "即将金叉": "near_golden",
    "即将死叉": "near_dead",
    "无交叉": None,
    "golden_cross": "golden_cross",
    "dead_cross": "dead_cross",
    "near_golden": "near_golden",
    "near_dead": "near_dead",
}

MA_ALIGNMENT_123_TO_TA: Dict[str, str] = {
    "多头扩散上行": "bull_strong",
    "多头上行": "bull_strong",
    "多头收敛": "bull_weakening",
    "多头乖离": "bull_strong",
    "空头扩散下行": "bear_strong",
    "空头下行": "bear_strong",
    "空头收敛": "bear_weakening",
    "空头乖离": "bear_strong",
    "偏多上行": "bull_building",
    "偏多整理": "bull_weakening",
    "偏空下行": "bear_building",
    "偏空整理": "bear_weakening",
    "均线粘合": "sideways",
    "交叉缠绕": "tangle",
}

DDE_TREND_123_TO_TA: Dict[str, str] = {
    "上升": "up",
    "下降": "down",
    "走平": "flat",
    "持平": "flat",
    "资金温和净流入趋势": "up",
    "资金强势净流入趋势": "up",
    "资金温和净流出趋势": "down",
    "资金强势净流出趋势": "down",
    "资金流向平衡": "flat",
    "up": "up",
    "down": "down",
    "flat": "flat",
}

VOL_TREND_123_TO_TA: Dict[str, str] = {
    "放量中": "expanding",
    "缩量中": "shrinking",
    "平量": "flat",
    "expanding": "expanding",
    "shrinking": "shrinking",
    "flat": "flat",
}

MACD_ALERT_123_TO_TA: Dict[str, str] = {
    "柱线拐上": "downturn_reverse",
    "柱线拐下": "upturn_reverse",
    "downturn_reverse": "downturn_reverse",
    "downturn_flat": "downturn_flat",
    "upturn_reverse": "upturn_reverse",
    "upturn_flat": "upturn_flat",
}

DDE_ALERT_123_TO_TA: Dict[str, str] = {
    "斜率拐头看多": "downturn_reverse",
    "斜率拐头看空": "upturn_reverse",
    "downturn_reverse": "downturn_reverse",
    "downturn_flat": "downturn_flat",
    "upturn_reverse": "upturn_reverse",
    "upturn_flat": "upturn_flat",
}

_FIELD_MAPS: Dict[str, Dict[str, str]] = {
    "macd_trend": MACD_TREND_123_TO_TA,
    "macd_zone": MACD_ZONE_123_TO_TA,
    "ma_alignment": MA_ALIGNMENT_123_TO_TA,
    "dde_trend": DDE_TREND_123_TO_TA,
    "vol_trend": VOL_TREND_123_TO_TA,
    "macd_alert": MACD_ALERT_123_TO_TA,
    "dde_alert": DDE_ALERT_123_TO_TA,
}


def b4_base_field(field: str) -> str:
    """Strip weekly ``w_`` prefix for enum lookup."""
    if field.startswith("w_"):
        return field[2:]
    return field


def _strip_nullish(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if s in _NULLISH_123:
        return None
    return s


def normalize_value(field: str, raw: Optional[str], source: str) -> Optional[str]:
    """Return TA-canonical value for comparison.

    source: 'ta' | '123'
    """
    base = b4_base_field(field)
    cleaned = _strip_nullish(raw)
    if cleaned is None:
        return None
    if source == "ta":
        return cleaned
    table = _FIELD_MAPS.get(base, {})
    # 123 volume_trend compound labels: zone·trend or 振幅不足·trend
    if base == "vol_trend" and "·" in cleaned:
        head, tail = cleaned.split("·", 1)
        tail = tail.strip()
        if head.strip() == "振幅不足":
            return table.get(tail, "flat")
        mapped = table.get(tail)
        if mapped:
            return mapped
    if cleaned in table:
        return table[cleaned]
    return cleaned
