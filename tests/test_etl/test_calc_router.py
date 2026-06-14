"""Tests for calc mode routing."""
import pandas as pd

from backend.etl.calc_router import classify_calc_mode, classify_calc_mode_detail


def test_classify_calc_mode_detail_returns_same_mode_and_fp():
    df = pd.DataFrame({
        "trade_date": ["20260101", "20260102"],
        "close_qfq": [10.0, 10.1],
    })
    state = {
        "last_trade_date": "20260102",
        "history_fp": "deadbeef00000000",
        "spec_version": "v1",
        "updated_calc_date": "20260609",
    }
    mode_plain, _ = classify_calc_mode(df, state, ["close_qfq"])
    mode_det, _, fp = classify_calc_mode_detail(df, state, ["close_qfq"])
    assert mode_plain == mode_det
    assert len(fp) == 16
