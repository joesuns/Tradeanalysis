import logging

import numpy as np
import pandas as pd
from backend.etl.base import (
    ema, to_float_safe, linear_regression_slope,
    weighted_window_slopes, sliding_window_mean_abs,
    compute_price_signal_divergence,
    insert_dws_batch, compute_input_fingerprint, check_dwd_unchanged,
    load_latest_fingerprints, load_quote_groups, resolve_ema_seeds,
    compute_history_signature,
    SkipReason, CalcResult,
)
from backend.etl.recalc_spec import RecalcSpec

logger = logging.getLogger(__name__)


class MACDCalculator:
    RECALC_SPEC_DAILY = RecalcSpec(lookback=60, seed=26, event_tail=5, min_rows=27)
    RECALC_SPEC_WEEKLY = RecalcSpec(lookback=60, seed=26, event_tail=5, min_rows=27)
    """MACD indicator calculator. Works for both daily and weekly frequencies."""

    SIGNATURE_COLS = ["close_qfq"]

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws_table = f"dws_macd_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str,
                  recalc_start: str = None,
                  quote_groups: dict = None) -> CalcResult:
        """Calculate MACD for a batch of stocks. Returns CalcResult with stats."""
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
                logger.debug("MACD %s skip %s: no DWD data", self.freq, ts_code)
                result.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
                continue
            if len(df) < 27:
                logger.debug("MACD %s skip %s: %d rows < 27",
                             self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=27")
                continue

            if check_dwd_unchanged(self.con, self.dws_table, ts_code, df,
                                   latest_fps=latest_fps, recalc_start=recalc_start):
                result.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                                "DWD fingerprint match")
                continue

            fp = compute_input_fingerprint(df, recalc_start=recalc_start)
            ema_seeds = resolve_ema_seeds(
                self.con, self.dws_table, ts_code, df, self.freq,
                ("ema_12", "ema_26", "dea"), recalc_start,
            )
            df = self._compute_indicators(df, ema_seeds=ema_seeds)
            if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                            write_start=recalc_start,
                            write_end=calc_date if recalc_start else None):
                result.calculated += 1
        return result

    def _compute_indicators(self, df: pd.DataFrame,
                            ema_seeds: dict = None) -> pd.DataFrame:
        c = df["close_qfq"].values.astype(float)
        s12 = ema_seeds.get("ema_12") if ema_seeds else None
        s26 = ema_seeds.get("ema_26") if ema_seeds else None
        df["ema_12"] = ema(c, 12, seed=s12)
        df["ema_26"] = ema(c, 26, seed=s26)
        df["dif"] = df["ema_12"] - df["ema_26"]
        sdea = ema_seeds.get("dea") if ema_seeds else None
        df["dea"] = ema(df["dif"].values.astype(float), 9, seed=sdea)
        df["macd_bar"] = 2.0 * (df["dif"] - df["dea"])
        df["zone"] = df["macd_bar"].apply(
            lambda x: "bull" if x > 0 else ("bear" if x < 0 else None)
        )
        window = 5  # 5-bar weighted regression for both daily and weekly
        df["trend"] = self._compute_trend(df["macd_bar"].values, window=window)
        df["trend_strength"] = self._compute_trend_strength(
            df["macd_bar"].values, window=window
        )
        df["divergence"] = self._compute_divergence(df)
        df["turning_point"] = self._compute_turning_points(df)
        df["alert"] = self._compute_alerts(df)
        return df

    def _compute_trend(self, bar: np.ndarray, window: int = 5) -> list:
        """MACD bar trend via exponentially weighted linear regression.
        Same method as 123 project: weighted slope with threshold 0.001.
        - up: weighted_slope > 0.001
        - down: weighted_slope < -0.001
        - flat: otherwise
        """
        # Vectorized fixed-window weighted regression (decay=0.15), equivalent
        # to the legacy per-bar np.polyfit loop. See base.weighted_window_slopes.
        slopes = weighted_window_slopes(bar, window, 0.15)
        result = [None] * len(bar)
        for i in np.nonzero(np.isfinite(slopes))[0]:
            s = slopes[i]
            if s > 0.001:
                result[i] = "up"
            elif s < -0.001:
                result[i] = "down"
            else:
                result[i] = "flat"
        return result

    def _compute_trend_strength(self, bar: np.ndarray, window: int = 5) -> np.ndarray:
        """MACD bar trend strength via exponentially weighted linear regression.

        Formula: slope / mean(|bar|), unitless signed value.
        Positive = bullish strength, negative = bearish strength.
        Weighted regression (decay=0.15) makes recent bars ~3x more influential.
        """
        # Vectorized: weighted slope / mean(|bar|) over each full window.
        slopes = weighted_window_slopes(bar, window, 0.15)
        scale = sliding_window_mean_abs(bar, window)
        result = np.full(len(bar), np.nan)
        full = ~np.isnan(scale)  # full non-NaN window
        result[full & (scale < 1e-6)] = 0.0
        mask = full & (scale >= 1e-6) & np.isfinite(slopes)
        result[mask] = slopes[mask] / scale[mask]
        return result

    def _compute_divergence(self, df: pd.DataFrame) -> list:
        """Top/bottom divergence using 60-day window (vectorized rolling + dedup)."""
        return compute_price_signal_divergence(
            df["close_qfq"].values, df["dif"].values, window=60, dedup=5,
        )

    def _compute_turning_points(self, df: pd.DataFrame) -> list:
        """Golden cross / Dead cross / Near golden / Near dead.

        Golden/dead cross = MACD bar sign flip.
        Near = estimated days to cross < 3 (small-gap direct or speed-based).
        Small gap: |DIF-DEA| < 0.005 → direct near.
        Speed: 3-day gap regression slope < 0 AND gap/|slope| < 3.
        Zero-axis fallback (|DEA| < close * 0.1%): absolute threshold.
        """
        result = [None] * len(df)
        bar = df["macd_bar"].values
        dif = df["dif"].values
        dea = df["dea"].values
        close = df["close_qfq"].values

        for i in range(1, len(df)):
            if pd.isna(bar[i - 1]) or pd.isna(bar[i]):
                continue

            # Golden / dead cross: MACD bar sign flip
            if bar[i - 1] <= 0 and bar[i] > 0:
                result[i] = "golden_cross"
                continue
            elif bar[i - 1] >= 0 and bar[i] < 0:
                result[i] = "dead_cross"
                continue

            # Near golden / near dead: 预估交叉天数 < 3
            if pd.isna(dif[i]) or pd.isna(dea[i]) or dea[i] == 0:
                continue
            if pd.isna(dif[i - 1]) or pd.isna(dea[i - 1]):
                continue

            gap = abs(dif[i] - dea[i])

            # 小间距直通: DIF-DEA 几乎合并
            if gap < 0.005:
                if dif[i] < dea[i]:
                    result[i] = "near_golden"
                else:
                    result[i] = "near_dead"
                continue

            # 速度判定: 3 日回归 est_days = gap / convergence_speed
            if i >= 2:
                if not pd.isna(dif[i - 2]) and not pd.isna(dea[i - 2]):
                    gap_seq = np.array([
                        abs(dif[i - 2] - dea[i - 2]),
                        abs(dif[i - 1] - dea[i - 1]),
                        gap,
                    ])
                    gap_slope = linear_regression_slope(gap_seq, use_log=False)
                    if gap_slope < 0:
                        conv_speed = -gap_slope
                        if conv_speed > 1e-9 and gap / conv_speed < 3:
                            # 零轴兜底（保留不变）
                            if abs(dea[i]) < close[i] * 0.001:
                                near = gap < close[i] * 0.0001
                            else:
                                near = gap / abs(dea[i]) < 0.15
                            if near:
                                if dif[i] < dea[i]:
                                    result[i] = "near_golden"
                                else:
                                    result[i] = "near_dead"

        return result

    def _compute_alerts(self, df: pd.DataFrame) -> list:
        """Upturn/downturn reverse + flat alerts.

        - reverse: prev 2 consecutive rises/falls, then direction flips
        - flat: prev 2 consecutive rises/falls, then |change|/|prev| <= 2%
        Reverse takes priority over flat when bar[i] < bar[i-1] (or > for downtrend).
        """
        result = [None] * len(df)
        bar = df["macd_bar"].values
        for i in range(3, len(df)):
            prev = bar[i - 3:i + 1]
            if any(pd.isna(x) for x in prev):
                continue

            prev_up = bar[i - 1] > bar[i - 2] and bar[i - 2] > bar[i - 3]
            prev_down = bar[i - 1] < bar[i - 2] and bar[i - 2] < bar[i - 3]

            if prev_up:
                if bar[i] < bar[i - 1]:
                    result[i] = "upturn_reverse"
                elif bar[i - 1] != 0 and abs(bar[i] - bar[i - 1]) / abs(bar[i - 1]) <= 0.02:
                    result[i] = "upturn_flat"
            elif prev_down:
                if bar[i] > bar[i - 1]:
                    result[i] = "downturn_reverse"
                elif bar[i - 1] != 0 and abs(bar[i] - bar[i - 1]) / abs(bar[i - 1]) <= 0.02:
                    result[i] = "downturn_flat"

        return result

    def append_calculate(self, ts_code: str, df: pd.DataFrame, new_bars: list,
                         calc_date: str, state: dict) -> CalcResult:
        """APPEND mode: compute over full tail-window df, write only new_bars.

        EMA (ema_12, ema_26, dea) seeds are loaded from DWS at the bar
        immediately before df[0], ensuring the seeded recursion is equivalent
        to full-history computation on the new bars (atol=1e-9).
        Falls back to SMA warm-up when seeds are unavailable.
        """
        result = CalcResult()
        seeds = resolve_ema_seeds(
            self.con, self.dws_table, ts_code, df, self.freq,
            ("ema_12", "ema_26", "dea"), recalc_start=new_bars[0],
        )
        df = self._compute_indicators(df, ema_seeds=seeds)
        fp = compute_history_signature(df, self.SIGNATURE_COLS)
        if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                        write_start=new_bars[0], write_end=new_bars[-1]):
            result.calculated += 1
        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str,
                input_fingerprint: str = None,
                write_start: str = None, write_end: str = None):
        dws_cols = ["ts_code", "trade_date", "ema_12", "ema_26", "dif", "dea",
                    "macd_bar", "divergence", "zone", "turning_point", "alert",
                    "trend", "trend_strength", "calc_date",
                    "input_fingerprint", "spec_version"]
        float_cols = ["ema_12", "ema_26", "dif", "dea", "macd_bar", "trend_strength"]
        return insert_dws_batch(self.con, self.dws_table, df, ts_code, calc_date,
                                dws_cols, float_cols,
                                input_fingerprint=input_fingerprint,
                                write_start=write_start, write_end=write_end)
