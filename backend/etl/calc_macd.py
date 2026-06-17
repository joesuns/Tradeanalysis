import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from backend.etl.base import (
    ema, to_float_safe,
    weighted_window_slopes, sliding_window_mean_abs,
    insert_dws_batch, compute_input_fingerprint, check_dwd_unchanged,
    load_latest_fingerprints, load_latest_spec_versions, load_quote_groups,
    resolve_ema_seeds,
    compute_history_signature,
    SkipReason, CalcResult,
)
from backend.etl.b4_alerts import compute_macd_hist_turn_alerts
from backend.etl.b4_macd import (
    B4_HIST_BARS_DAILY,
    B4_MACD_HIST_EPS,
    B4_MACD_PARAMS_DAILY,
    B4_MACD_PARAMS_WEEKLY,
    B4_NEAR_DAILY,
    B4_NEAR_WEEKLY,
    b4_weekly_series_from_daily,
    b4_weekly_series_from_daily_fast,
    compute_macd_crossover_123_series,
    compute_macd_trend_123_series,
    macd_ewm_columns,
)
from backend.config import CALC_B4_WEEKLY_FAST
from backend.etl.divergence_structure import compute_macd_structure_divergence
from backend.etl.recalc_spec import RecalcSpec

MACD_B4_WEEKLY_DAILY_HISTORY_DAYS = 900

logger = logging.getLogger(__name__)


def resolve_b4_weekly_target_indices(df: pd.DataFrame, new_bars: list) -> list:
    """Map APPEND ``new_bars`` trade dates to row indices in weekly tail ``df``."""
    td_set = {str(d) for d in new_bars}
    return [i for i, d in enumerate(df["trade_date"].astype(str)) if d in td_set]


def require_b4_weekly_target_indices(
    df: pd.DataFrame,
    new_bars: Optional[list],
    *,
    ts_code: str = "",
) -> list:
    """APPEND gate: weekly B4 ``new_bars`` must 1:1 map to tail df row indices."""
    prefix = f"ts_code={ts_code} " if ts_code else ""
    if new_bars is None:
        raise ValueError(f"{prefix}APPEND MACD weekly B4 requires new_bars")
    if len(new_bars) == 0:
        raise ValueError(f"{prefix}APPEND MACD weekly B4 new_bars must be non-empty")
    indices = resolve_b4_weekly_target_indices(df, new_bars)
    if len(indices) != len(new_bars):
        str_bars = [str(d) for d in new_bars]
        if len(set(str_bars)) != len(str_bars):
            raise ValueError(f"{prefix}duplicate dates in new_bars: {new_bars}")
        td_in_df = set(df["trade_date"].astype(str))
        missing = [s for s in str_bars if s not in td_in_df]
        raise ValueError(
            f"{prefix}new_bars not in tail df: missing={missing} new_bars={new_bars}"
        )
    return indices


class MACDCalculator:
    # v2: B4 macd_alert; v3: B4 macd_trend/macd_zone (10,20,7 daily + resample-W weekly).
    SPEC_VERSION = "v3"

    RECALC_SPEC_DAILY = RecalcSpec(lookback=250, seed=26, event_tail=10, min_rows=27)
    RECALC_SPEC_WEEKLY = RecalcSpec(lookback=250, seed=26, event_tail=10, min_rows=27)
    """MACD indicator calculator. Works for both daily and weekly frequencies."""

    SIGNATURE_COLS = ["close_qfq"]

    DWS_COLS = [
        "ts_code", "trade_date", "ema_12", "ema_26", "dif", "dea",
        "macd_bar", "divergence", "zone", "turning_point", "alert",
        "trend", "trend_strength", "calc_date",
        "input_fingerprint", "spec_version",
    ]
    FLOAT_COLS = ["ema_12", "ema_26", "dif", "dea", "macd_bar", "trend_strength"]

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
        daily_b4_groups: dict = {}
        if self.freq == "weekly":
            b4_start = self._weekly_b4_daily_start(calc_date)
            daily_b4_groups = self._load_daily_for_b4_batch(
                ts_codes, start_date=b4_start, end_date=calc_date,
            )
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

            if check_dwd_unchanged(
                self.con, self.dws_table, ts_code, df,
                latest_fps=latest_fps, recalc_start=recalc_start,
                expected_spec_version=self.SPEC_VERSION,
                latest_specs=latest_specs,
            ):
                result.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                                "DWD fingerprint match")
                continue

            fp = compute_input_fingerprint(df, recalc_start=recalc_start)
            ema_seeds = resolve_ema_seeds(
                self.con, self.dws_table, ts_code, df, self.freq,
                ("ema_12", "ema_26", "dea"), recalc_start,
            )
            daily_b4 = daily_b4_groups.get(ts_code) if self.freq == "weekly" else None
            df = self._compute_indicators(
                df, ema_seeds=ema_seeds, daily_for_b4=daily_b4,
            )
            if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                            write_start=recalc_start,
                            write_end=calc_date if recalc_start else None):
                result.calculated += 1
        return result

    @staticmethod
    def _weekly_b4_daily_start(calc_date: str) -> str:
        end = datetime.strptime(calc_date, "%Y%m%d")
        start = end - timedelta(days=MACD_B4_WEEKLY_DAILY_HISTORY_DAYS)
        return start.strftime("%Y%m%d")

    def _load_daily_for_b4(
        self,
        ts_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        clauses = ["ts_code = ?", "is_suspended = 0"]
        params: list = [ts_code]
        if start_date:
            clauses.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("trade_date <= ?")
            params.append(end_date)
        where = " AND ".join(clauses)
        return self.con.execute(f"""
            SELECT trade_date, close_qfq
            FROM dwd_daily_quote
            WHERE {where}
            ORDER BY trade_date
        """, params).df()

    def _load_daily_for_b4_batch(
        self,
        ts_codes: list[str],
        chunk_size: int = 400,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> dict:
        groups: dict = {}
        for i in range(0, len(ts_codes), chunk_size):
            chunk = ts_codes[i:i + chunk_size]
            ph = ",".join(["?"] * len(chunk))
            clauses = [f"ts_code IN ({ph})", "is_suspended = 0"]
            params: list = list(chunk)
            if start_date:
                clauses.append("trade_date >= ?")
                params.append(start_date)
            if end_date:
                clauses.append("trade_date <= ?")
                params.append(end_date)
            where = " AND ".join(clauses)
            big = self.con.execute(f"""
                SELECT ts_code, trade_date, close_qfq
                FROM dwd_daily_quote
                WHERE {where}
                ORDER BY ts_code, trade_date
            """, params).df()
            if big.empty:
                continue
            for ts_code, g in big.groupby("ts_code", sort=False):
                groups[ts_code] = g.drop(columns=["ts_code"]).reset_index(drop=True)
        return groups

    def _apply_b4_trend_and_zone(
        self,
        df: pd.DataFrame,
        daily_for_b4: Optional[pd.DataFrame] = None,
        b4_target_indices: Optional[set] = None,
    ) -> pd.DataFrame:
        """B4 ``macd_trend`` / ``macd_zone`` → DWS ``trend`` / ``turning_point``."""
        freq = getattr(self, "freq", "daily")
        if freq == "daily":
            c = df["close_qfq"].values.astype(float)
            p = B4_MACD_PARAMS_DAILY
            b4_dif, b4_dea, b4_macd = macd_ewm_columns(
                c, p["fast"], p["slow"], p["signal"],
            )
            df["trend"] = compute_macd_trend_123_series(
                b4_macd, B4_HIST_BARS_DAILY, B4_MACD_HIST_EPS,
            )
            df["turning_point"] = compute_macd_crossover_123_series(
                b4_dif, b4_dea, B4_NEAR_DAILY["n_std"], B4_NEAR_DAILY["frac"],
            )
        elif daily_for_b4 is not None and not daily_for_b4.empty:
            week_ends = df["trade_date"].astype(str).tolist()
            b4_fn = (
                b4_weekly_series_from_daily_fast
                if CALC_B4_WEEKLY_FAST
                else b4_weekly_series_from_daily
            )
            trends, crosses = b4_fn(
                daily_for_b4, week_ends, target_indices=b4_target_indices,
            )
            df["trend"] = trends
            df["turning_point"] = crosses
        else:
            c = df["close_qfq"].values.astype(float)
            p = B4_MACD_PARAMS_WEEKLY
            b4_dif, b4_dea, b4_macd = macd_ewm_columns(
                c, p["fast"], p["slow"], p["signal"],
            )
            df["trend"] = compute_macd_trend_123_series(b4_macd)
            df["turning_point"] = compute_macd_crossover_123_series(
                b4_dif, b4_dea, B4_NEAR_WEEKLY["n_std"], B4_NEAR_WEEKLY["frac"],
            )
        return df

    def _compute_indicators(
        self,
        df: pd.DataFrame,
        ema_seeds: dict = None,
        daily_for_b4: Optional[pd.DataFrame] = None,
        target_indices: Optional[set] = None,
        b4_target_indices: Optional[set] = None,
    ) -> pd.DataFrame:
        df = self._compute_macd_core(df, ema_seeds=ema_seeds)
        return self._compute_macd_derived(
            df,
            daily_for_b4=daily_for_b4,
            target_indices=target_indices,
            b4_target_indices=b4_target_indices,
        )

    def _compute_macd_core(
        self,
        df: pd.DataFrame,
        ema_seeds: dict = None,
    ) -> pd.DataFrame:
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
        return df

    def _compute_macd_derived(
        self,
        df: pd.DataFrame,
        daily_for_b4: Optional[pd.DataFrame] = None,
        target_indices: Optional[set] = None,
        b4_target_indices: Optional[set] = None,
    ) -> pd.DataFrame:
        window = 5  # 5-bar weighted regression for both daily and weekly
        df["trend_strength"] = self._compute_trend_strength(
            df["macd_bar"].values, window=window
        )
        df["divergence"] = self._compute_divergence(df, target_indices=target_indices)
        df = self._apply_b4_trend_and_zone(
            df, daily_for_b4=daily_for_b4, b4_target_indices=b4_target_indices,
        )
        df["alert"] = self._compute_alerts(df)
        return df

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

    def _compute_divergence(
        self, df: pd.DataFrame, target_indices: Optional[set] = None,
    ) -> list:
        """Top/bottom divergence via Tongdaxin Level 2 structure (TG day annotation)."""
        return compute_macd_structure_divergence(
            df["close_qfq"].values,
            df["dif"].values,
            df["dea"].values,
            df["macd_bar"].values,
            dedup=10,
            target_indices=target_indices,
        )

    def _compute_alerts(self, df: pd.DataFrame) -> list:
        """123 ``_eval_macd_hist_turn``: 3-bar histogram inflection only."""
        return compute_macd_hist_turn_alerts(df["macd_bar"].values)

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
        daily_b4 = None
        b4_target = None
        if self.freq == "weekly":
            b4_start = self._weekly_b4_daily_start(calc_date)
            daily_b4 = self._load_daily_for_b4(
                ts_code, start_date=b4_start, end_date=calc_date,
            )
            b4_target = set(require_b4_weekly_target_indices(
                df, new_bars, ts_code=ts_code,
            ))
        df = self._compute_macd_core(df, ema_seeds=seeds)
        target_idx = b4_target if b4_target is not None else None
        df = self._compute_macd_derived(
            df,
            daily_for_b4=daily_b4,
            target_indices=target_idx,
            b4_target_indices=target_idx,
        )
        fp = compute_history_signature(df, self.SIGNATURE_COLS)
        if self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                        write_start=new_bars[0], write_end=new_bars[-1]):
            result.calculated += 1
        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str,
                input_fingerprint: str = None,
                write_start: str = None, write_end: str = None):
        return insert_dws_batch(
            self.con, self.dws_table, df, ts_code, calc_date,
            self.DWS_COLS, self.FLOAT_COLS,
            spec_version=self.SPEC_VERSION,
            input_fingerprint=input_fingerprint,
            write_start=write_start, write_end=write_end,
        )
