"""Tests for scripts.audit_ma_alignment_fallback."""
import pandas as pd

from backend.etl.calc_ma import MACalculator, _layer3_fallback_alignment


def test_layer3_eight_cell_helper():
    assert _layer3_fallback_alignment(True, False, True, True, False, False, False) == "bull_building"
    assert _layer3_fallback_alignment(True, True, False, False, False, True, False) == "bull_building"
    assert _layer3_fallback_alignment(False, True, False, False, False, True, False) == "bear_building"


def test_recompute_single_row_matches_stored():
    calc = MACalculator.__new__(MACalculator)
    df = pd.DataFrame({
        "trade_date": ["d0"],
        "close_qfq": [10.7],
        "ma_5": [10.7],
        "ma_10": [10.9],
        "ma5_slope": [0.04],
        "ma10_slope": [0.25],
    })
    assert calc._compute_alignment(df)[0] == "bear_building"
