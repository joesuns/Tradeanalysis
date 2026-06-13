import pandas as pd

from backend.b4_gate.diff import diff_b4_frames


def test_diff_b4_reports_mismatch():
    ta = pd.DataFrame({
        "ts_code": ["A.SZ"],
        "macd_trend": ["up"],
        "w_macd_trend": ["down"],
    })
    ref = ta.copy()
    ref.loc[0, "macd_trend"] = "down"
    mismatches = diff_b4_frames(ta, ref, skip_dde_ts=set())
    assert len(mismatches) == 1
    assert mismatches[0]["field"] == "macd_trend"


def test_diff_b4_skips_ma_alignment():
    ta = pd.DataFrame({
        "ts_code": ["A.SZ"],
        "ma_alignment": ["bull_strong"],
        "w_ma_alignment": ["bear_strong"],
    })
    ref = ta.copy()
    ref.loc[0, "ma_alignment"] = "sideways"
    ref.loc[0, "w_ma_alignment"] = "tangle"
    mismatches = diff_b4_frames(ta, ref, skip_dde_ts=set())
    assert mismatches == []
