"""Excel export layout: column order and 综合分析 signal-only columns."""

from backend.export_wide import (
    _BASIC_HEADER_FILL,
    _SIGNAL_ONLY,
    _reorder_vol_signal,
)


def test_reorder_vol_signal_after_vol_divergence():
    cols = [
        "kpattern", "price_position_60d", "vol_signal", "macd_zone",
        "ma_vol_5", "vol_zone", "vol_trend", "vol_divergence",
    ]
    out = _reorder_vol_signal(cols)
    assert out.index("vol_signal") == out.index("vol_divergence") + 1
    assert "vol_signal" not in out[: out.index("vol_divergence")]


def test_reorder_vol_signal_fallback_after_vol_trend():
    cols = ["kpattern", "vol_signal", "vol_zone", "vol_trend"]
    out = _reorder_vol_signal(cols)
    assert out.index("vol_signal") == out.index("vol_trend") + 1


def test_reorder_vol_signal_noop_without_column():
    cols = ["kpattern", "vol_zone", "vol_trend"]
    assert _reorder_vol_signal(cols) == cols


def test_signal_only_excludes_structure_and_reject_divergence():
    excluded = {
        "macd_divergence", "macd_divergence_reject",
        "dde_divergence", "dde_divergence_reject",
    }
    assert not excluded & set(_SIGNAL_ONLY)
    assert "macd_divergence_tradable" in _SIGNAL_ONLY
    assert "dde_divergence_tradable" in _SIGNAL_ONLY


def test_signal_only_volume_group_includes_vol_divergence_and_vol_signal():
    vol_start = _SIGNAL_ONLY.index("vol_zone")
    vol_block = _SIGNAL_ONLY[vol_start:]
    assert vol_block == ["vol_zone", "vol_trend", "vol_divergence", "vol_signal"]


def test_basic_header_fill_near_black():
    assert _BASIC_HEADER_FILL == "1A1A1A"
