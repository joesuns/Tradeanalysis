import logging

import numpy as np
import pandas as pd
from backend.etl.base import (
    sma, to_float_safe, linear_regression_slope, weighted_window_slopes,
    insert_dws_batch, compute_input_fingerprint, check_dwd_unchanged,
    load_latest_fingerprints, load_latest_spec_versions, load_quote_groups, compute_history_signature,
    SkipReason, CalcResult,
)
from backend.etl.recalc_spec import RecalcSpec

logger = logging.getLogger(__name__)


def _compute_slope_pct(series: np.ndarray, window: int = 5) -> np.ndarray:
    """5-bar linear regression slope, normalized as %/day of current value.

    Replaces the old diff(3)/shift(3)*100 formula. The normalization by
    current MA value makes slopes comparable across stocks of different prices.
    """
    # Vectorized fixed-window OLS slope (decay=0), equivalent to the legacy
    # per-bar linear_regression_slope loop. See base.weighted_window_slopes.
    series = np.asarray(series, dtype=float)
    slopes = weighted_window_slopes(series, window, 0.0)
    result = np.full(len(series), np.nan)
    mask = np.isfinite(slopes) & (series != 0)
    result[mask] = slopes[mask] / series[mask] * 100.0
    return result


def _layer3_fallback_alignment(
    above: bool,
    s5_flat: bool,
    s10_flat: bool,
    s5_up: bool,
    s5_dn: bool,
    s10_up: bool,
    s10_dn: bool,
):
    """Layer 3 eight-cell map (MACalculator.SPEC_VERSION=v2, data-model §6.3)."""
    if above:
        if s5_up and s10_flat:
            return "bull_building"
        if s5_flat and s10_up:
            return "bull_building"
        if s5_dn and s10_flat:
            return "bull_weakening"
        if s5_flat and s10_dn:
            return "bull_weakening"
    else:
        if s5_dn and s10_flat:
            return "bear_building"
        if s5_flat and s10_up:
            return "bear_building"
        if s5_up and s10_flat:
            return "bear_weakening"
        if s5_flat and s10_dn:
            return "bear_strong"
    return None


class MACalculator:
    """Moving Average indicator calculator. Computes MA5, MA10, bias, slope,
    alignment (10 DWS enums + single-slope fallback), and turning points (golden/dead cross).
    Works for both daily and weekly frequencies."""

    SPEC_VERSION = "v2"

    RECALC_SPEC_DAILY = RecalcSpec(lookback=10, seed=10, event_tail=5, min_rows=11)
    RECALC_SPEC_WEEKLY = RecalcSpec(lookback=10, seed=10, event_tail=5, min_rows=11)

    SIGNATURE_COLS = ["close_qfq"]

    DWS_COLS = [
        "ts_code", "trade_date", "ma_5", "ma_10",
        "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope",
        "alignment", "turning_point", "calc_date",
        "input_fingerprint", "spec_version",
    ]
    FLOAT_COLS = ["ma_5", "ma_10", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope"]

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws_table = f"dws_ma_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str,
                  recalc_start: str = None,
                  quote_groups: dict = None) -> CalcResult:
        result = CalcResult()
        latest_fps = load_latest_fingerprints(self.con, self.dws_table, ts_codes)
        latest_specs = load_latest_spec_versions(self.con, self.dws_table, ts_codes)
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
                logger.debug("MA %s skip %s: no DWD data", self.freq, ts_code)
                result.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
                continue
            if len(df) < 11:
                logger.debug("MA %s skip %s: %d rows < 11", self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=11")
                continue

            if check_dwd_unchanged(self.con, self.dws_table, ts_code, df,
                                   latest_fps=latest_fps, recalc_start=recalc_start,
                                   expected_spec_version=self.SPEC_VERSION,
                                   latest_specs=latest_specs):
                result.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                                "DWD fingerprint match")
                continue

            fp = compute_input_fingerprint(df, recalc_start=recalc_start)
            df = self._compute_indicators(df)
            if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                            write_start=recalc_start,
                            write_end=calc_date if recalc_start else None):
                result.calculated += 1
        return result

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["close_qfq"].values.astype(float)
        df["ma_5"] = sma(c, 5)
        df["ma_10"] = sma(c, 10)
        # Bias (乖离率): (close - MA) / MA * 100
        df["bias_ma5"] = (c - df["ma_5"].values) / df["ma_5"].values * 100.0
        df["bias_ma10"] = (c - df["ma_10"].values) / df["ma_10"].values * 100.0
        # Slope: 5-bar linear regression normalized as %/day
        df["ma5_slope"] = _compute_slope_pct(df["ma_5"].values)
        df["ma10_slope"] = _compute_slope_pct(df["ma_10"].values)
        df["alignment"] = self._compute_alignment(df)
        df["turning_point"] = self._compute_turning_points(df)
        return df

    def _compute_alignment(self, df: pd.DataFrame) -> list:
        """11-value alignment: 8 directional + tangle + sideways + single-slope fallback.

        Layer 1 tangle: gap < 3% of MA10 AND >= 2 crosses in last 10 days.
        Layer 2 sideways: both |slope| < 0.08%/day.
        Layer 3 (fallback): exactly one slope flat, map to nearest of 8 directional codes.
        """
        result = [None] * len(df)
        ma5 = df["ma_5"].values
        ma10 = df["ma_10"].values
        s5 = df["ma5_slope"].values
        s10 = df["ma10_slope"].values

        # Pre-compute crosses for tangle cross-count
        cross_marker = np.zeros(len(df), dtype=int)
        for i in range(1, len(df)):
            if pd.isna(ma5[i]) or pd.isna(ma10[i]) or pd.isna(ma5[i - 1]) or pd.isna(ma10[i - 1]):
                continue
            if (ma5[i - 1] <= ma10[i - 1] and ma5[i] > ma10[i]) or \
               (ma5[i - 1] >= ma10[i - 1] and ma5[i] < ma10[i]):
                cross_marker[i] = 1

        for i in range(len(df)):
            if pd.isna(ma5[i]) or pd.isna(ma10[i]):
                continue
            if pd.isna(s5[i]) or pd.isna(s10[i]):
                continue

            # Tangle: gap < 3% AND >= 2 crosses in last 10 days
            gap = abs(ma5[i] - ma10[i]) / ma10[i] if ma10[i] != 0 else 0
            recent_crosses = cross_marker[max(0, i - 9):i + 1].sum()
            if gap < 0.0299 and recent_crosses >= 2:
                result[i] = "tangle"
                continue

            # Sideways: 双斜率均在平区（|s| < 0.08%）且非 tangle
            s5_flat = s5[i] > -0.08 and s5[i] < 0.08
            s10_flat = s10[i] > -0.08 and s10[i] < 0.08
            if s5_flat and s10_flat:
                result[i] = "sideways"
                continue

            above = ma5[i] > ma10[i]
            s5_up = s5[i] > 0.08
            s5_dn = s5[i] < -0.08
            s10_up = s10[i] > 0.08
            s10_dn = s10[i] < -0.08

            if above and s5_up and s10_up:
                result[i] = "bull_strong"
            elif above and s5_up and s10_dn:
                result[i] = "bull_building"
            elif above and s5_dn and s10_up:
                result[i] = "bull_weakening"
            elif above and s5_dn and s10_dn:
                result[i] = "bull_rolling"
            elif not above and s5_dn and s10_dn:
                result[i] = "bear_strong"
            elif not above and s5_dn and s10_up:
                result[i] = "bear_building"
            elif not above and s5_up and s10_dn:
                result[i] = "bear_weakening"
            elif not above and s5_up and s10_up:
                result[i] = "bear_rolling"
            # Layer 3: single-slope transitional — v2 eight-cell lookup (spec §6.3)
            elif s5_flat != s10_flat:
                fb = _layer3_fallback_alignment(
                    above, s5_flat, s10_flat, s5_up, s5_dn, s10_up, s10_dn,
                )
                if fb is not None:
                    result[i] = fb

        return result

    def _compute_turning_points(self, df: pd.DataFrame) -> list:
        """Golden/dead cross + near_golden/near_dead on MA5/MA10 crossover.

        Near = estimated days to cross < 3 (small-gap direct or speed-based).
        Small gap: |MA5-MA10|/MA10 < 0.5% → direct near.
        Speed: 3-day gap regression slope < 0 AND gap/|slope| < 3.
        """
        result = [None] * len(df)
        ma5 = df["ma_5"].values
        ma10 = df["ma_10"].values

        for i in range(1, len(df)):
            if pd.isna(ma5[i - 1]) or pd.isna(ma10[i - 1]):
                continue
            if pd.isna(ma5[i]) or pd.isna(ma10[i]):
                continue
            if ma10[i] == 0:
                continue

            # Golden / dead cross
            if ma5[i - 1] <= ma10[i - 1] and ma5[i] > ma10[i]:
                result[i] = "golden_cross"
                continue
            elif ma5[i - 1] >= ma10[i - 1] and ma5[i] < ma10[i]:
                result[i] = "dead_cross"
                continue

            # Near golden / near dead: 预估交叉天数 < 3
            gap = abs(ma5[i] - ma10[i])
            gap_pct = gap / ma10[i]

            # 小间距直通: gap < 0.5% of MA10
            if gap_pct < 0.005:
                if ma5[i] < ma10[i]:
                    result[i] = "near_golden"
                else:
                    result[i] = "near_dead"
                continue

            # 速度判定: 3 日回归 est_days = gap / convergence_speed
            if i >= 2:
                gap_seq = np.array([
                    abs(ma5[i - 2] - ma10[i - 2]),
                    abs(ma5[i - 1] - ma10[i - 1]),
                    gap,
                ])
                gap_slope = linear_regression_slope(gap_seq, use_log=False)
                if gap_slope < 0:
                    conv_speed = -gap_slope
                    if conv_speed > 1e-9 and gap / conv_speed < 3:
                        if ma5[i] < ma10[i]:
                            result[i] = "near_golden"
                        else:
                            result[i] = "near_dead"

        return result

    def _compute_indicators_append(self, df: pd.DataFrame, new_bars: list) -> pd.DataFrame:
        """Compute MA indicators over the full tail-window df; caller writes only new_bars.

        MA5/MA10 and all derived indicators (bias, slope, alignment, turning point)
        are causal rolling: a bar's value depends only on its preceding N bars.
        With a tail window >= 10 bars of warmup, append values equal FULL by
        construction.
        """
        return self._compute_indicators(df)

    def append_calculate(self, ts_code: str, df: pd.DataFrame, new_bars: list,
                         calc_date: str, state: dict) -> "CalcResult":
        """APPEND mode: compute over full tail window, write only new_bars."""
        result = CalcResult()
        df = self._compute_indicators_append(df, new_bars)
        fp = compute_history_signature(df, self.SIGNATURE_COLS)
        if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                        write_start=new_bars[0], write_end=new_bars[-1]):
            result.calculated += 1
        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str,
                input_fingerprint: str = None,
                write_start: str = None, write_end: str = None):
        return insert_dws_batch(self.con, self.dws_table, df, ts_code, calc_date,
                                self.DWS_COLS, self.FLOAT_COLS,
                                input_fingerprint=input_fingerprint,
                                write_start=write_start, write_end=write_end)
