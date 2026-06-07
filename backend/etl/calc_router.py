"""Route each stock to SKIP / APPEND / FULL for append-only calc."""
from typing import Optional, List, Tuple
import pandas as pd
from backend.etl.base import compute_history_signature

# Fixed trailing-window width for the state signature. Kept strictly below the
# loaded history (load provides max_lookback=250 bars ending at calc_date, i.e.
# only ~249 bars <= last_td on the day after a baseline, fewer across a small
# gap). A window below that guaranteed coverage keeps tail(SIG_WINDOW) identical
# run-to-run, so the signature is stable and a sliding load_start does not cause
# spurious FULL routing. A too-small window only risks an extra (safe) FULL —
# never a wrong APPEND — because APPEND always recomputes the new bar over the
# full loaded window regardless of the signature decision.
SIG_WINDOW = 245


def state_signature(df: "pd.DataFrame", last_td: str, sig_cols: List[str],
                    sig_window: int = SIG_WINDOW) -> str:
    """Stable signature over the last ``sig_window`` bars with trade_date <= last_td.

    A fixed-width trailing window makes the signature independent of the variable
    load_start, so identical data yields an identical signature across runs while
    still catching ex-div rescaling (qfq shifts in-window closes) and in-window
    corrections. Changes older than the window cannot affect a new bar's value,
    so missing them is harmless.
    """
    hist = df[df["trade_date"] <= last_td].tail(sig_window)
    return compute_history_signature(hist, sig_cols)


def classify_calc_mode(df: "pd.DataFrame", state: Optional[dict],
                       sig_cols: List[str],
                       sig_window: int = SIG_WINDOW) -> Tuple[str, List[str]]:
    """Decide calc mode for one stock given its loaded tail-window df and state.

    Returns (mode, new_bars) where mode in {"SKIP","APPEND","FULL"} and
    new_bars is the list of trade_dates strictly after state.last_trade_date.
    Signature domain = the fixed trailing window ending at state.last_trade_date.
    """
    if state is None:
        return "FULL", []
    last_td = state["last_trade_date"]
    cur_fp = state_signature(df, last_td, sig_cols, sig_window)
    if cur_fp != state["history_fp"]:
        return "FULL", []
    new_bars = df[df["trade_date"] > last_td]["trade_date"].astype(str).tolist()
    if not new_bars:
        return "SKIP", []
    return "APPEND", new_bars
