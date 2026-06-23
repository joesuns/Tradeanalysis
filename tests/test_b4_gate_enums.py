"""B4 enum normalization for 123 ↔ TA diff."""
from backend.b4_gate.enums import normalize_value


def test_macd_zone_crossover_maps():
    assert normalize_value("macd_zone", "金叉", "123") == "golden_cross"
    assert normalize_value("macd_zone", "无交叉", "123") is None


def test_dde_trend_moneyflow_labels():
    assert normalize_value("dde_trend", "资金温和净流入趋势", "123") == "up"
    assert normalize_value("dde_trend", "资金流向平衡", "123") == "flat"


def test_vol_trend_compound_label():
    assert normalize_value("vol_trend", "正常区·放量中", "123") == "expanding"
    assert normalize_value("vol_trend", "振幅不足·缩量中", "123") == "shrinking"


def test_macd_alert_hist_turn():
    assert normalize_value("macd_alert", "柱线拐上", "123") == "downturn_reverse"


def test_dde_alert_hist_turn():
    assert normalize_value("dde_alert", "斜率拐头看多", "123") == "downturn_reverse"
    assert normalize_value("dde_alert", "斜率拐头看空", "123") == "upturn_reverse"
