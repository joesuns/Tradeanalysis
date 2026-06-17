"""B4 MACD trend / crossover aligned with 123 analysis_macd_daily/weekly.

Daily MACD params (10, 20, 7); weekly (12, 26, 9). EWM ``adjust=False`` matches
``utils.utils_macd``. Maps to DWS ``trend`` / ``turning_point`` for B4 diff.
"""
from typing import List, Optional, Set, Tuple

import numpy as np
import pandas as pd

# 123 config.config.Config
B4_MACD_PARAMS_DAILY = {"fast": 10, "slow": 20, "signal": 7}
B4_MACD_PARAMS_WEEKLY = {"fast": 12, "slow": 26, "signal": 9}

B4_HIST_BARS_DAILY = 5
B4_HIST_BARS_WEEKLY = 5
B4_MACD_HIST_EPS = 0.001
B4_TREND_DECAY = 0.15

B4_NEAR_DAILY = {"n_std": 20, "frac": 0.1}
B4_NEAR_WEEKLY = {"n_std": 10, "frac": 0.15}

_TA_CROSSOVER = {
    "golden_cross": "golden_cross",
    "dead_cross": "dead_cross",
    "near_golden": "near_golden",
    "near_dead": "near_dead",
}


def macd_ewm_columns(
    close: np.ndarray,
    fast: int,
    slow: int,
    signal: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """123 ``utils_macd.compute_macd`` — pandas ewm, hist = 2×(DIF−DEA)."""
    s = pd.Series(np.asarray(close, dtype=float))
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd = 2.0 * (dif - dea)
    return dif.values, dea.values, macd.values


def _macd_trend_label(macd_tail: np.ndarray, eps: float) -> Optional[str]:
    """123 ``get_macd_trend`` on a tail window."""
    vals = np.asarray(macd_tail, dtype=float)
    if len(vals) < 2 or not np.isfinite(vals).all():
        return None
    n = len(vals)
    x = np.arange(n, dtype=float)
    weights = np.exp(np.arange(n) * B4_TREND_DECAY)
    try:
        slope = float(np.polyfit(x, vals, 1, w=weights)[0])
    except (np.linalg.LinAlgError, ValueError, TypeError):
        return None
    if not np.isfinite(slope):
        return None
    if abs(slope) < eps:
        return "flat"
    return "up" if slope > 0 else "down"


def compute_macd_trend_123_series(
    macd: np.ndarray,
    hist_bars: int = B4_HIST_BARS_DAILY,
    eps: float = B4_MACD_HIST_EPS,
) -> List[Optional[str]]:
    """Per-bar trend using trailing ``hist_bars`` MACD histogram values."""
    macd = np.asarray(macd, dtype=float)
    n = len(macd)
    out: List[Optional[str]] = [None] * n
    for i in range(hist_bars - 1, n):
        tail = macd[i - hist_bars + 1: i + 1]
        if not np.isfinite(tail).all():
            continue
        out[i] = _macd_trend_label(tail, eps)
    return out


def _near_crossover_at(
    dif: np.ndarray,
    dea: np.ndarray,
    i: int,
    n_std: int,
    frac: float,
) -> Optional[str]:
    """123 ``identify_near_crossover`` at index ``i`` (needs ``i >= 2``)."""
    if i < 2:
        return None
    prev_dif, prev_dea = float(dif[i - 1]), float(dea[i - 1])
    curr_dif, curr_dea = float(dif[i]), float(dea[i])
    if not all(np.isfinite([prev_dif, prev_dea, curr_dif, curr_dea])):
        return None

    prev_diff = abs(prev_dif - prev_dea)
    curr_diff = abs(curr_dif - curr_dea)

    if i + 1 >= n_std:
        dif_std = float(np.nanstd(dif[i - n_std + 1: i + 1], ddof=0))
        dea_std = float(np.nanstd(dea[i - n_std + 1: i + 1], ddof=0))
        threshold = max(dif_std, dea_std) * frac
        if not np.isfinite(threshold):
            threshold = max(abs(curr_dif), abs(curr_dea)) * frac
    else:
        threshold = max(abs(curr_dif), abs(curr_dea)) * frac

    if (
        prev_dif < prev_dea and curr_dif < curr_dea
        and curr_diff < prev_diff and curr_diff < threshold
    ):
        return "near_golden"
    if (
        prev_dif > prev_dea and curr_dif > curr_dea
        and curr_diff < prev_diff and curr_diff < threshold
    ):
        return "near_dead"
    return None


def _crossover_at(
    dif: np.ndarray,
    dea: np.ndarray,
    i: int,
    n_std: int,
    frac: float,
) -> Optional[str]:
    """123 ``identify_macd_crossover`` at bar ``i``."""
    if i < 1:
        return None
    prev_dif, prev_dea = float(dif[i - 1]), float(dea[i - 1])
    curr_dif, curr_dea = float(dif[i]), float(dea[i])
    if not all(np.isfinite([prev_dif, prev_dea, curr_dif, curr_dea])):
        return None
    if prev_dif <= prev_dea and curr_dif > curr_dea:
        return "golden_cross"
    if prev_dif >= prev_dea and curr_dif < curr_dea:
        return "dead_cross"
    return _near_crossover_at(dif, dea, i, n_std, frac)


def compute_macd_crossover_123_series(
    dif: np.ndarray,
    dea: np.ndarray,
    n_std: int,
    frac: float,
) -> List[Optional[str]]:
    """Per-bar crossover / near-crossover (B4 ``macd_zone``)."""
    dif = np.asarray(dif, dtype=float)
    dea = np.asarray(dea, dtype=float)
    n = len(dif)
    out: List[Optional[str]] = [None] * n
    for i in range(1, n):
        out[i] = _crossover_at(dif, dea, i, n_std, frac)
    return out


def convert_daily_to_weekly_resample_w(daily_df: pd.DataFrame) -> pd.DataFrame:
    """123 ``utils_data.convert_to_weekly_data`` (``resample('W')``)."""
    if daily_df.empty:
        return pd.DataFrame()
    df = daily_df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").set_index("trade_date")
    if "close_qfq" in df.columns:
        close_col = "close_qfq"
    else:
        close_col = "close"
    weekly = df[[close_col]].resample("W").last().dropna(how="all")
    weekly = weekly.reset_index()
    weekly.columns = ["trade_date", "close_qfq"]
    return weekly


def b4_weekly_trend_and_crossover_at(
    daily_df: pd.DataFrame,
    week_end: str,
    hist_bars: int = B4_HIST_BARS_WEEKLY,
    near_cfg: dict = None,
) -> Tuple[Optional[str], Optional[str]]:
    """123 weekly MACD snapshot at ``week_end`` (daily history ≤ week_end)."""
    near_cfg = near_cfg or B4_NEAR_WEEKLY
    sub = daily_df.copy()
    sub["trade_date"] = sub["trade_date"].astype(str)
    sub = sub[sub["trade_date"] <= str(week_end)].copy()
    if sub.empty:
        return None, None
    weekly = convert_daily_to_weekly_resample_w(sub)
    if len(weekly) < 2:
        return None, None
    params = B4_MACD_PARAMS_WEEKLY
    dif, dea, macd = macd_ewm_columns(
        weekly["close_qfq"].values,
        params["fast"], params["slow"], params["signal"],
    )
    i = len(weekly) - 1
    trend = _macd_trend_label(
        macd[max(0, i - hist_bars + 1): i + 1],
        B4_MACD_HIST_EPS,
    )
    cross = _crossover_at(
        dif, dea, i,
        int(near_cfg["n_std"]), float(near_cfg["frac"]),
    )
    return trend, cross


def b4_weekly_series_from_daily(
    daily_df: pd.DataFrame,
    week_end_dates: List[str],
    target_indices: Optional[Set[int]] = None,
) -> Tuple[List[Optional[str]], List[Optional[str]]]:
    """Trend + crossover for each TA ``week_end`` trade_date.

    When ``target_indices`` is set (APPEND path), only those bar indices are
    computed; other positions remain None.
    """
    n = len(week_end_dates)
    trends: List[Optional[str]] = [None] * n
    crosses: List[Optional[str]] = [None] * n
    indices = range(n) if target_indices is None else sorted(target_indices)
    for i in indices:
        if i < 0 or i >= n:
            continue
        t, c = b4_weekly_trend_and_crossover_at(daily_df, week_end_dates[i])
        trends[i] = t
        crosses[i] = c
    return trends, crosses


def _weekly_last_daily_dates(daily_df: pd.DataFrame, weekly_df: pd.DataFrame) -> List[str]:
    """Max daily trade_date per pandas ``W`` bucket aligned to ``weekly_df`` rows."""
    if daily_df.empty or weekly_df.empty:
        return []
    d = daily_df.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    d = d.sort_values("trade_date")
    max_by_period = d.groupby(d["trade_date"].dt.to_period("W-SUN"))["trade_date"].max()
    out: List[str] = []
    for wdt in weekly_df["trade_date"]:
        ts = pd.to_datetime(wdt)
        mx = max_by_period.get(ts.to_period("W-SUN"))
        out.append(mx.strftime("%Y%m%d") if mx is not None and pd.notna(mx) else "")
    return out


def _weekly_prefix_index(last_daily_dates: List[str], cutoff: str) -> int:
    """Last weekly row whose bucket max daily date is <= cutoff (oracle prefix)."""
    cutoff = str(cutoff)
    j = -1
    for idx, ld in enumerate(last_daily_dates):
        if ld and ld <= cutoff:
            j = idx
        elif ld:
            break
    return j


def b4_weekly_series_from_daily_fast(
    daily_df: pd.DataFrame,
    week_end_dates: List[str],
    target_indices: Optional[Set[int]] = None,
) -> Tuple[List[Optional[str]], List[Optional[str]]]:
    """Single resample + EWM; map each week_end cutoff to weekly prefix index.

    Oracle-equivalent to ``b4_weekly_series_from_daily`` (expanding per cutoff).
    """
    n = len(week_end_dates)
    trends: List[Optional[str]] = [None] * n
    crosses: List[Optional[str]] = [None] * n
    if n == 0 or daily_df is None or daily_df.empty:
        return trends, crosses

    weekly = convert_daily_to_weekly_resample_w(daily_df)
    if len(weekly) < 2:
        return trends, crosses

    last_daily = _weekly_last_daily_dates(daily_df, weekly)
    params = B4_MACD_PARAMS_WEEKLY
    near_cfg = B4_NEAR_WEEKLY
    hist_bars = B4_HIST_BARS_WEEKLY
    dif, dea, macd = macd_ewm_columns(
        weekly["close_qfq"].values,
        params["fast"], params["slow"], params["signal"],
    )

    indices = range(n) if target_indices is None else sorted(target_indices)
    for i in indices:
        if i < 0 or i >= n:
            continue
        j = _weekly_prefix_index(last_daily, week_end_dates[i])
        if j < 0:
            continue
        trends[i] = _macd_trend_label(
            macd[max(0, j - hist_bars + 1): j + 1],
            B4_MACD_HIST_EPS,
        )
        crosses[i] = _crossover_at(
            dif, dea, j,
            int(near_cfg["n_std"]), float(near_cfg["frac"]),
        )
    return trends, crosses
