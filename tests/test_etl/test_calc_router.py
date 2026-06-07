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
    from backend.etl.calc_router import state_signature
    fp = state_signature(df, "20260102", SIG_COLS)
    state = {"last_trade_date": "20260102", "history_fp": fp}
    mode, new_bars = classify_calc_mode(df, state=state, sig_cols=SIG_COLS)
    assert mode == "APPEND"
    assert new_bars == ["20260103"]


def test_state_signature_stable_across_sliding_load_window():
    """Models the real daily slide: load is a fixed 250-bar window ending at the
    new calc_date, so it advances by one bar each day. With last_td = newest-1,
    the fixed trailing signature window must be identical on both days, yielding
    an APPEND (not a spurious FULL)."""
    import numpy as np
    from backend.etl.calc_router import state_signature
    WIDTH = 250
    n = WIDTH + 1
    dates = [f"26{i:06d}" for i in range(n)]
    rng = np.random.default_rng(7)
    closes = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    df_day1 = _df(dates[:WIDTH], list(closes[:WIDTH]))        # load on day T
    df_day2 = _df(dates[1:], list(closes[1:])).reset_index(drop=True)  # day T+1
    last_td = dates[WIDTH - 1]  # newest bar of day1 = newest-1 on day2

    fp1 = state_signature(df_day1, last_td, SIG_COLS)
    assert state_signature(df_day2, last_td, SIG_COLS) == fp1

    state = {"last_trade_date": last_td, "history_fp": fp1}
    mode, new_bars = classify_calc_mode(df_day2, state=state, sig_cols=SIG_COLS)
    assert mode == "APPEND"
    assert new_bars == [dates[WIDTH]]
