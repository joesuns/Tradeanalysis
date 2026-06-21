"""B4 alert signals aligned with 123 reversal modules.

- MACD: ``trend_reversal_signals_daily._hist_turn_up/down`` (3-bar inflection)
- DDE: ``trend_reversal_signals_daily._ddx2_slope_inflection_*`` (adjacent window slopes)
"""
from typing import List, Optional

import numpy as np


def _polyfit_slope(y: np.ndarray) -> Optional[float]:
    """123 ``utils_volume._safe_polyfit_slope`` on x=0..n-1."""
    y = np.asarray(y, dtype=float)
    if len(y) < 2 or not np.isfinite(y).all():
        return None
    x = np.arange(len(y), dtype=float)
    try:
        m = float(np.polyfit(x, y, 1)[0])
        return m if np.isfinite(m) else None
    except (np.linalg.LinAlgError, ValueError, TypeError):
        return None


def _ddx2_segment_slope(values: np.ndarray, window: int, end_shift: int = 0) -> float:
    """123 ``_ddx2_raw_segment_slope`` on a 1-D array ending at the last bar."""
    if window < 2 or end_shift < 0:
        return float("nan")
    need = window + end_shift
    if len(values) < need:
        return float("nan")
    if end_shift == 0:
        seg = values[-window:]
    else:
        seg = values[-(window + end_shift):-end_shift]
    if len(seg) != window or not np.isfinite(seg).all():
        return float("nan")
    slope = _polyfit_slope(seg)
    return float(slope) if slope is not None else float("nan")


def compute_macd_hist_turn_alerts(macd_bar: np.ndarray) -> List[Optional[str]]:
    """Per-bar MACD histogram turn alerts (123 ``_eval_macd_hist_turn``).

    Returns TA enums:
    - downturn_reverse: 下降趋势反弹（V 形，h[i-1] 为局部最小值，看多）
    - upturn_reverse: 上升趋势回落（Λ 形，h[i-1] 为局部最大值，看空）
    """
    bar = np.asarray(macd_bar, dtype=float)
    n = len(bar)
    result: List[Optional[str]] = [None] * n
    for i in range(2, n):
        h0, h1, h2 = bar[i - 2], bar[i - 1], bar[i]
        if not np.isfinite(h0) or not np.isfinite(h1) or not np.isfinite(h2):
            continue
        if h2 > h1 < h0:
            result[i] = "downturn_reverse"
        elif h2 < h1 > h0:
            result[i] = "upturn_reverse"
    return result


def compute_ddx2_slope_alerts(
    ddx2: np.ndarray,
    window: int = 2,
    eps: float = 0.0,
) -> List[Optional[str]]:
    """Per-bar DDX2 slope inflection alerts (123 ``_eval_ddx2_slope_reversal``).

    Adjacent-window slope comparison: s_prev = slope of window bars ending at i-1,
    s_now = slope of window bars ending at i.  Window=3 gives 67% segment overlap
    (33% new info per bar), balancing responsiveness against noise rejection.

    Returns TA enums:
    - downturn_reverse: 下降趋势反弹（斜率由负转正，看多）
    - upturn_reverse: 上升趋势回落（斜率由正转负，看空）
    """
    values = np.asarray(ddx2, dtype=float)
    n = len(values)
    result: List[Optional[str]] = [None] * n
    if window < 2:
        return result
    for i in range(window, n):
        seg = values[: i + 1]
        s_prev = _ddx2_segment_slope(seg, window, end_shift=1)
        s_now = _ddx2_segment_slope(seg, window, end_shift=0)
        if not (np.isfinite(s_prev) and np.isfinite(s_now)):
            continue
        if s_prev < -eps and s_now > eps:
            result[i] = "downturn_reverse"
        elif s_prev > eps and s_now < -eps:
            result[i] = "upturn_reverse"
    return result
