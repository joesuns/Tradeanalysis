"""Route each stock to SKIP / APPEND / FULL for append-only calc."""
from typing import Optional, List, Tuple
import pandas as pd
from backend.etl.base import compute_history_signature


def classify_calc_mode(df: "pd.DataFrame", state: Optional[dict],
                       sig_cols: List[str]) -> Tuple[str, List[str]]:
    """Decide calc mode for one stock given its loaded tail-window df and state.

    Returns (mode, new_bars) where mode in {"SKIP","APPEND","FULL"} and
    new_bars is the list of trade_dates strictly after state.last_trade_date.
    Signature domain = bars up to and including state.last_trade_date.
    """
    if state is None:
        return "FULL", []
    last_td = state["last_trade_date"]
    hist = df[df["trade_date"] <= last_td]
    cur_fp = compute_history_signature(hist, sig_cols)
    if cur_fp != state["history_fp"]:
        return "FULL", []
    new_bars = df[df["trade_date"] > last_td]["trade_date"].astype(str).tolist()
    if not new_bars:
        return "SKIP", []
    return "APPEND", new_bars
