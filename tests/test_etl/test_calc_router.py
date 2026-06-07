import pandas as pd
from backend.etl.calc_router import classify_calc_mode

SIG_COLS = ["close_qfq", "vol"]


def _df(dates, closes):
    return pd.DataFrame({"trade_date": dates, "close_qfq": closes,
                         "vol": [100.0] * len(dates)})


def test_no_state_returns_full():
    df = _df(["20260101", "20260102"], [10.0, 11.0])
    mode, new_bars = classify_calc_mode(df, state=None, sig_cols=SIG_COLS)
    assert mode == "FULL"


def test_signature_changed_returns_full():
    df = _df(["20260101", "20260102"], [10.0, 11.0])
    state = {"last_trade_date": "20260102", "history_fp": "STALE"}
    mode, _ = classify_calc_mode(df, state=state, sig_cols=SIG_COLS)
    assert mode == "FULL"


def test_no_new_bars_same_sig_returns_skip():
    df = _df(["20260101", "20260102"], [10.0, 11.0])
    from backend.etl.base import compute_history_signature
    fp = compute_history_signature(df, SIG_COLS)
    state = {"last_trade_date": "20260102", "history_fp": fp}
    mode, new_bars = classify_calc_mode(df, state=state, sig_cols=SIG_COLS)
    assert mode == "SKIP"
    assert new_bars == []


def test_new_bars_same_history_returns_append():
    df = _df(["20260101", "20260102", "20260103"], [10.0, 11.0, 12.0])
    from backend.etl.base import compute_history_signature
    hist = df[df["trade_date"] <= "20260102"]
    fp = compute_history_signature(hist, SIG_COLS)
    state = {"last_trade_date": "20260102", "history_fp": fp}
    mode, new_bars = classify_calc_mode(df, state=state, sig_cols=SIG_COLS)
    assert mode == "APPEND"
    assert new_bars == ["20260103"]
