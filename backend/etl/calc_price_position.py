import logging

import numpy as np
import pandas as pd
from backend.etl.base import (
    to_float_safe, insert_dws_batch, compute_input_fingerprint, check_dwd_unchanged,
    load_latest_fingerprints, load_quote_groups, rolling_window_minmax_deque,
    compute_history_signature, SkipReason, CalcResult,
)
from backend.etl.recalc_spec import RecalcSpec

logger = logging.getLogger(__name__)


class PricePositionCalculator:
    """Price position (relative strength) calculator.

    Computes price_position for 3 window sizes (60, 120, 250).
    price_position_N = (close - N_day_low) / (N_day_high - N_day_low) * 100

    This is a PURE PRICE feature — no dependency on any other DWS table.
    It serves as infrastructure for MACD divergence high-position checks,
    K-pattern trend context, and volume zone price-aware interpretation.
    Works for both daily and weekly frequencies.
    """

    RECALC_SPEC_DAILY = RecalcSpec(lookback=250, seed=0, event_tail=0, min_rows=2)
    RECALC_SPEC_WEEKLY = RecalcSpec(lookback=250, seed=0, event_tail=0, min_rows=2)

    WINDOWS = [60, 120, 250]

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws_table = f"dws_price_position_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str,
                  recalc_start: str = None,
                  quote_groups: dict = None) -> CalcResult:
        result = CalcResult()
        latest_fps = load_latest_fingerprints(self.con, self.dws_table, ts_codes)
        if quote_groups is None:
            load_start = None
            if recalc_start:
                from backend.etl.recalc_spec import resolve_load_start
                load_start = resolve_load_start(self.con, recalc_start, self.freq)
            groups = load_quote_groups(self.con, self.src_table, self.freq,
                                       ["trade_date", "close_qfq"], ts_codes,
                                       start_date=load_start)
        else:
            groups = quote_groups
        for ts_code in ts_codes:
            df = groups.get(ts_code)

            if df is None or df.empty:
                logger.debug("PricePosition %s skip %s: no DWD data", self.freq, ts_code)
                result.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
                continue
            if len(df) < 2:
                logger.debug("PricePosition %s skip %s: %d rows < 2",
                             self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=2")
                continue

            if check_dwd_unchanged(self.con, self.dws_table, ts_code, df,
                                   latest_fps=latest_fps, recalc_start=recalc_start):
                result.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                                "DWD fingerprint match")
                continue

            fp = compute_input_fingerprint(df, recalc_start=recalc_start)
            df = self._compute_positions(df)
            self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                         write_start=recalc_start,
                         write_end=calc_date if recalc_start else None)
            result.calculated += 1
        return result

    def _compute_positions(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute price_position for all window sizes (deque rolling min/max)."""
        c = df["close_qfq"].values.astype(float)

        for window in self.WINDOWS:
            col = f"price_position_{window}d"
            roll_min, roll_max = rolling_window_minmax_deque(c, window, min_periods=2)
            denom = roll_max - roll_min
            with np.errstate(divide='ignore', invalid='ignore'):
                df[col] = np.where(
                    denom > 0,
                    (c - roll_min) / denom * 100.0,
                    np.nan,
                )

        return df

    def _compute_positions_append(self, df: pd.DataFrame, new_bars: list) -> pd.DataFrame:
        """Compute positions over the full tail-window df; caller writes only new_bars.

        PP at any bar depends only on its trailing N-bar window (causal rolling
        min/max).  Given a df whose tail covers the full 250-bar lookback, the
        value at each new bar is identical to FULL by construction.
        """
        return self._compute_positions(df)

    def append_calculate(self, ts_code: str, df: pd.DataFrame, new_bars: list,
                         calc_date: str, state: dict) -> "CalcResult":
        """APPEND mode: compute over full tail window, write only new_bars."""
        result = CalcResult()
        df = self._compute_positions_append(df, new_bars)
        fp = compute_history_signature(df, ["close_qfq"])
        self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                     write_start=new_bars[0], write_end=new_bars[-1])
        result.calculated += 1
        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str,
                input_fingerprint: str = None,
                write_start: str = None, write_end: str = None):
        pos_cols = [f"price_position_{w}d" for w in self.WINDOWS]
        dws_cols = ["ts_code", "trade_date"] + pos_cols + [
            "calc_date", "input_fingerprint", "spec_version"]
        float_cols = pos_cols
        insert_dws_batch(self.con, self.dws_table, df, ts_code, calc_date,
                         dws_cols, float_cols,
                         input_fingerprint=input_fingerprint,
                         write_start=write_start, write_end=write_end)
