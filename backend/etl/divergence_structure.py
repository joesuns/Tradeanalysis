"""Tongdaxin-style MACD/DDE structure divergence (Level 2: direct + skip-peak + bar peak)."""
from typing import List, Optional, Set

import numpy as np


def mdif_part(value: float, ref_peak: float) -> int:
    """INTPART(value / 10^PDIFH); PDIFH=INTPART(LOG(|ref|))-1 (Tongdaxin INTPART truncates toward zero)."""
    if not np.isfinite(value) or not np.isfinite(ref_peak) or ref_peak == 0:
        return 0
    abs_ref = abs(float(ref_peak))
    pdifh = int(np.trunc(np.log10(abs_ref))) - 1
    scale = 10.0 ** pdifh
    return int(float(value) / scale)


def cross_up(fast: np.ndarray, slow: np.ndarray, i: int) -> bool:
    """Golden cross: fast crosses above slow at index i."""
    if i <= 0:
        return False
    a, b = float(fast[i - 1]), float(fast[i])
    c, d = float(slow[i - 1]), float(slow[i])
    if not all(np.isfinite(x) for x in (a, b, c, d)):
        return False
    return a <= c and b > d


def cross_down(fast: np.ndarray, slow: np.ndarray, i: int) -> bool:
    """Death cross: fast crosses below slow at index i."""
    if i <= 0:
        return False
    a, b = float(fast[i - 1]), float(fast[i])
    c, d = float(slow[i - 1]), float(slow[i])
    if not all(np.isfinite(x) for x in (a, b, c, d)):
        return False
    return a >= c and b < d


def _last_cross_index(
    fast: np.ndarray, slow: np.ndarray, i: int, golden: bool
) -> Optional[int]:
    """Return index of most recent golden (or death) cross at or before bar i."""
    for j in range(i, -1, -1):
        if golden and cross_up(fast, slow, j):
            return j
        if not golden and cross_down(fast, slow, j):
            return j
    return None


def _cross_index_arrays(
    fast: np.ndarray, slow: np.ndarray,
) -> tuple:
    """O(n) last golden/death cross index at or before each bar (-1 = none)."""
    n = len(fast)
    last_gc = np.full(n, -1, dtype=int)
    last_dc = np.full(n, -1, dtype=int)
    gc = -1
    dc = -1
    for i in range(n):
        if cross_up(fast, slow, i):
            gc = i
        if cross_down(fast, slow, i):
            dc = i
        last_gc[i] = gc
        last_dc[i] = dc
    return last_gc, last_dc


def _cross_at(arr: np.ndarray, i: int) -> Optional[int]:
    v = int(arr[i])
    return v if v >= 0 else None


def _write_divergence(
    result: List[Optional[str]],
    i: int,
    label: str,
    target_indices: Optional[Set[int]],
) -> None:
    if target_indices is not None and i not in target_indices:
        return
    result[i] = label


def _recent(result: List[Optional[str]], i: int, label: str, dedup: int) -> bool:
    """True if label appeared within dedup bars before i."""
    return any(result[j] == label for j in range(max(0, i - dedup), i))


def compute_macd_structure_divergence(
    close,
    dif,
    dea,
    macd_bar,
    dedup: int = 10,
    target_indices: Optional[Set[int]] = None,
) -> List[Optional[str]]:
    """Tongdaxin Level 2 MACD structure divergence; annotate on TG day only."""
    close = np.asarray(close, dtype=float)
    dif = np.asarray(dif, dtype=float)
    dea = np.asarray(dea, dtype=float)
    macd_bar = np.asarray(macd_bar, dtype=float)
    n = len(close)
    result: List[Optional[str]] = [None] * n
    if n < 3:
        return result

    CH1 = np.full(n, np.nan)
    DIFH1 = np.full(n, np.nan)
    MACDH1 = np.full(n, np.nan)
    CL1 = np.full(n, np.nan)
    DIFL1 = np.full(n, np.nan)
    MACDL1 = np.full(n, np.nan)

    m1_arr = np.full(n, -1, dtype=int)

    last_gc_arr, last_dc_arr = _cross_index_arrays(dif, dea)

    for i in range(n):
        gc = _cross_at(last_gc_arr, i)
        if gc is not None:
            m1 = i - gc
            m1_arr[i] = m1
            seg = slice(gc, i + 1)
            c_slice = close[seg]
            d_slice = dif[seg]
            m_slice = macd_bar[seg]
            if np.all(np.isfinite(c_slice)):
                CH1[i] = np.max(c_slice)
                DIFH1[i] = np.max(d_slice)
                pos = m_slice > 0
                MACDH1[i] = np.max(m_slice[pos]) if pos.any() else np.nan

        dc = _cross_at(last_dc_arr, i)
        if dc is not None:
            seg = slice(dc, i + 1)
            c_slice = close[seg]
            d_slice = dif[seg]
            m_slice = macd_bar[seg]
            if np.all(np.isfinite(c_slice)):
                CL1[i] = np.min(c_slice)
                DIFL1[i] = np.min(d_slice)
                neg = m_slice < 0
                MACDL1[i] = np.min(m_slice[neg]) if neg.any() else np.nan

    CH2 = np.full(n, np.nan)
    DIFH2 = np.full(n, np.nan)
    MACDH2 = np.full(n, np.nan)
    CH3 = np.full(n, np.nan)
    DIFH3 = np.full(n, np.nan)
    MACDH3 = np.full(n, np.nan)
    CL2 = np.full(n, np.nan)
    DIFL2 = np.full(n, np.nan)
    MACDL2 = np.full(n, np.nan)
    CL3 = np.full(n, np.nan)
    DIFL3 = np.full(n, np.nan)
    MACDL3 = np.full(n, np.nan)

    for i in range(n):
        m1 = m1_arr[i]
        if m1 >= 0:
            ref = i - (m1 + 1)
            if ref >= 0:
                CH2[i] = CH1[ref]
                DIFH2[i] = DIFH1[ref]
                MACDH2[i] = MACDH1[ref]
                if ref >= 0 and np.isfinite(CH2[ref]):
                    CH3[i] = CH2[ref]
                    DIFH3[i] = DIFH2[ref]
                    MACDH3[i] = MACDH2[ref]

        dc = _cross_at(last_dc_arr, i)
        if dc is not None:
            n1 = i - dc
            ref = i - (n1 + 1)
            if ref >= 0:
                CL2[i] = CL1[ref]
                DIFL2[i] = DIFL1[ref]
                MACDL2[i] = MACDL1[ref]
                if ref >= 0 and np.isfinite(CL2[ref]):
                    CL3[i] = CL2[ref]
                    DIFL3[i] = DIFL2[ref]
                    MACDL3[i] = MACDL2[ref]

    T1 = np.zeros(n, dtype=bool)
    T2 = np.zeros(n, dtype=bool)
    mdift2 = np.full(n, np.nan)
    mdift3 = np.full(n, np.nan)
    B1 = np.zeros(n, dtype=bool)
    B2 = np.zeros(n, dtype=bool)
    mdifb2 = np.full(n, np.nan)
    mdifb3 = np.full(n, np.nan)

    for i in range(n):
        if not np.isfinite(CH1[i]) or not np.isfinite(DIFH2[i]):
            pass
        else:
            ch2 = CH2[i]
            difh2 = DIFH2[i]
            macdh2 = MACDH2[i]
            ch3 = CH3[i]
            difh3 = DIFH3[i]
            macdh3 = MACDH3[i]

            m2_val = mdif_part(dif[i], difh2)
            mh2 = mdif_part(difh2, difh2)
            mdift2[i] = m2_val

            m3_val = mdif_part(dif[i], difh3) if np.isfinite(difh3) else 0
            mh3 = mdif_part(difh3, difh3) if np.isfinite(difh3) else 0
            mdift3[i] = m3_val

            red = macd_bar[i] > 0 and (i == 0 or macd_bar[i - 1] > 0)
            bar_ok = np.isfinite(macdh2) and np.isfinite(MACDH1[i]) and MACDH1[i] < macdh2

            t1_line = (
                CH1[i] > ch2
                and m2_val < mh2
                and red
                and (i == 0 or mdift2[i] >= mdift2[i - 1])
            )
            t2_line = (
                np.isfinite(ch3)
                and CH1[i] > ch3 > ch2
                and m3_val < mh3
                and red
                and (i == 0 or mdift3[i] >= mdift3[i - 1])
            )
            bar2 = np.isfinite(macdh3) and np.isfinite(MACDH1[i]) and MACDH1[i] < macdh3
            T1[i] = t1_line and bar_ok
            T2[i] = t2_line and bar2

            if i > 0 and (T1[i - 1] or T2[i - 1]):
                tg = False
                if T1[i - 1] and np.isfinite(mdift2[i]) and np.isfinite(mdift2[i - 1]):
                    if mdift2[i] < mdift2[i - 1]:
                        tg = True
                elif T2[i - 1] and np.isfinite(mdift3[i]) and np.isfinite(mdift3[i - 1]):
                    if mdift3[i] < mdift3[i - 1]:
                        tg = True

                if tg:
                    invalidated = False
                    if T1[i - 1] and np.isfinite(DIFH1[i]) and np.isfinite(difh2):
                        if DIFH1[i] >= difh2:
                            invalidated = True
                    if T2[i - 1] and np.isfinite(difh3) and np.isfinite(DIFH1[i]):
                        if DIFH1[i] >= difh3:
                            invalidated = True
                    if not invalidated and not _recent(result, i, "top_divergence", dedup):
                        _write_divergence(result, i, "top_divergence", target_indices)

        if not np.isfinite(CL1[i]) or not np.isfinite(DIFL2[i]):
            continue

        cl2 = CL2[i]
        difl2 = DIFL2[i]
        macdl2 = MACDL2[i]
        cl3 = CL3[i]
        difl3 = DIFL3[i]
        macdl3 = MACDL3[i]

        mb2_val = mdif_part(dif[i], difl2)
        ml2 = mdif_part(difl2, difl2)
        mdifb2[i] = mb2_val

        mb3_val = mdif_part(dif[i], difl3) if np.isfinite(difl3) else 0
        ml3 = mdif_part(difl3, difl3) if np.isfinite(difl3) else 0
        mdifb3[i] = mb3_val

        green = macd_bar[i] < 0 and (i == 0 or macd_bar[i - 1] < 0)
        bar_ok_b = np.isfinite(macdl2) and np.isfinite(MACDL1[i]) and MACDL1[i] > macdl2

        b1_line = (
            CL1[i] < cl2
            and mb2_val > ml2
            and green
            and (i == 0 or mdifb2[i] <= mdifb2[i - 1])
        )
        b2_line = (
            np.isfinite(cl3)
            and CL1[i] < cl3 < cl2
            and mb3_val > ml3
            and green
            and (i == 0 or mdifb3[i] <= mdifb3[i - 1])
        )
        bar2_b = np.isfinite(macdl3) and np.isfinite(MACDL1[i]) and MACDL1[i] > macdl3
        B1[i] = b1_line and bar_ok_b
        B2[i] = b2_line and bar2_b

        if i > 0 and (B1[i - 1] or B2[i - 1]):
            tg_b = False
            if B1[i - 1] and np.isfinite(mdifb2[i]) and np.isfinite(mdifb2[i - 1]):
                if mdifb2[i] > mdifb2[i - 1]:
                    tg_b = True
            elif B2[i - 1] and np.isfinite(mdifb3[i]) and np.isfinite(mdifb3[i - 1]):
                if mdifb3[i] > mdifb3[i - 1]:
                    tg_b = True

            if tg_b:
                invalidated_b = False
                if B1[i - 1] and np.isfinite(DIFL1[i]) and np.isfinite(difl2):
                    if DIFL1[i] <= difl2:
                        invalidated_b = True
                if B2[i - 1] and np.isfinite(difl3) and np.isfinite(DIFL1[i]):
                    if DIFL1[i] <= difl3:
                        invalidated_b = True
                if not invalidated_b and not _recent(result, i, "bottom_divergence", dedup):
                    _write_divergence(result, i, "bottom_divergence", target_indices)

    return result


def _is_ddx_spike(seg_ddx: np.ndarray, peak_idx: int, peak_val: float) -> bool:
    """True when DDX peak is an isolated spike (neighbors < 0.8× peak)."""
    lo = max(0, peak_idx - 2)
    hi = min(len(seg_ddx), peak_idx + 3)
    neighbors = seg_ddx[lo:hi]
    return (neighbors >= peak_val * 0.8).sum() < 2


def _segment_ddx_peak(
    d_slice: np.ndarray, spike_filter_top: bool
) -> float:
    """Max DDX in segment; returns NaN if peak is spike-filtered."""
    if not np.any(np.isfinite(d_slice)):
        return np.nan
    peak_rel = int(np.nanargmax(d_slice))
    peak_val = float(d_slice[peak_rel])
    if spike_filter_top and _is_ddx_spike(d_slice, peak_rel, peak_val):
        return np.nan
    return peak_val


def compute_dde_structure_divergence(
    close,
    ddx,
    ddx2,
    dedup: int = 10,
    spike_filter_top: bool = True,
    require_finite: bool = True,
    target_indices: Optional[Set[int]] = None,
) -> List[Optional[str]]:
    """Tongdaxin Level 2 DDE structure divergence; annotate on TG day only.

    fast=DDX, slow=DDX2, bar=DDX for segment bar-peak comparisons.
    Top zone: ddx>0 and ref(ddx,1)>0; bottom zone: ddx<0 and ref<0.
    """
    close = np.asarray(close, dtype=float)
    ddx = np.asarray(ddx, dtype=float)
    ddx2 = np.asarray(ddx2, dtype=float)
    n = len(close)
    result: List[Optional[str]] = [None] * n
    if n < 3:
        return result

    CH1 = np.full(n, np.nan)
    DDXH1 = np.full(n, np.nan)
    BARDH1 = np.full(n, np.nan)
    CL1 = np.full(n, np.nan)
    DDXL1 = np.full(n, np.nan)
    BARDL1 = np.full(n, np.nan)

    m1_arr = np.full(n, -1, dtype=int)

    last_gc_arr, last_dc_arr = _cross_index_arrays(ddx, ddx2)

    for i in range(n):
        gc = _cross_at(last_gc_arr, i)
        if gc is not None:
            m1 = i - gc
            m1_arr[i] = m1
            seg = slice(gc, i + 1)
            c_slice = close[seg]
            d_slice = ddx[seg]
            if require_finite and not np.all(np.isfinite(d_slice)):
                pass
            elif np.all(np.isfinite(c_slice)):
                CH1[i] = np.max(c_slice)
                DDXH1[i] = _segment_ddx_peak(d_slice, spike_filter_top)
                pos = d_slice > 0
                if pos.any():
                    pos_vals = d_slice[pos]
                    peak_rel = int(np.argmax(pos_vals))
                    abs_idx = int(np.where(pos)[0][peak_rel])
                    peak_val = float(pos_vals[peak_rel])
                    if spike_filter_top and _is_ddx_spike(d_slice, abs_idx, peak_val):
                        BARDH1[i] = np.nan
                    else:
                        BARDH1[i] = peak_val

        dc = _cross_at(last_dc_arr, i)
        if dc is not None:
            seg = slice(dc, i + 1)
            c_slice = close[seg]
            d_slice = ddx[seg]
            if require_finite and not np.all(np.isfinite(d_slice)):
                pass
            elif np.all(np.isfinite(c_slice)):
                CL1[i] = np.min(c_slice)
                neg = d_slice < 0
                DDXL1[i] = np.min(d_slice) if neg.any() else np.nan
                if neg.any():
                    BARDL1[i] = float(np.min(d_slice[neg]))

    CH2 = np.full(n, np.nan)
    DDXH2 = np.full(n, np.nan)
    BARDH2 = np.full(n, np.nan)
    CH3 = np.full(n, np.nan)
    DDXH3 = np.full(n, np.nan)
    BARDH3 = np.full(n, np.nan)
    CL2 = np.full(n, np.nan)
    DDXL2 = np.full(n, np.nan)
    BARDL2 = np.full(n, np.nan)
    CL3 = np.full(n, np.nan)
    DDXL3 = np.full(n, np.nan)
    BARDL3 = np.full(n, np.nan)

    for i in range(n):
        m1 = m1_arr[i]
        if m1 >= 0:
            ref = i - (m1 + 1)
            if ref >= 0:
                CH2[i] = CH1[ref]
                DDXH2[i] = DDXH1[ref]
                BARDH2[i] = BARDH1[ref]
                if ref >= 0 and np.isfinite(CH2[ref]):
                    CH3[i] = CH2[ref]
                    DDXH3[i] = DDXH2[ref]
                    BARDH3[i] = BARDH2[ref]

        dc = _cross_at(last_dc_arr, i)
        if dc is not None:
            n1 = i - dc
            ref = i - (n1 + 1)
            if ref >= 0:
                CL2[i] = CL1[ref]
                DDXL2[i] = DDXL1[ref]
                BARDL2[i] = BARDL1[ref]
                if ref >= 0 and np.isfinite(CL2[ref]):
                    CL3[i] = CL2[ref]
                    DDXL3[i] = DDXL2[ref]
                    BARDL3[i] = BARDL2[ref]

    T1 = np.zeros(n, dtype=bool)
    T2 = np.zeros(n, dtype=bool)
    mdift2 = np.full(n, np.nan)
    mdift3 = np.full(n, np.nan)
    B1 = np.zeros(n, dtype=bool)
    B2 = np.zeros(n, dtype=bool)
    mdifb2 = np.full(n, np.nan)
    mdifb3 = np.full(n, np.nan)

    for i in range(n):
        if np.isfinite(CH1[i]) and np.isfinite(DDXH2[i]):
            ch2 = CH2[i]
            ddxh2 = DDXH2[i]
            bardh2 = BARDH2[i]
            ch3 = CH3[i]
            ddxh3 = DDXH3[i]
            bardh3 = BARDH3[i]

            m2_val = mdif_part(ddx[i], ddxh2)
            mh2 = mdif_part(ddxh2, ddxh2)
            mdift2[i] = m2_val

            m3_val = mdif_part(ddx[i], ddxh3) if np.isfinite(ddxh3) else 0
            mh3 = mdif_part(ddxh3, ddxh3) if np.isfinite(ddxh3) else 0
            mdift3[i] = m3_val

            red = ddx[i] > 0 and (i == 0 or ddx[i - 1] > 0)
            bar_ok = (
                np.isfinite(bardh2) and np.isfinite(BARDH1[i])
                and BARDH1[i] < bardh2
            )

            t1_line = (
                CH1[i] > ch2
                and m2_val < mh2
                and red
                and (i == 0 or mdift2[i] >= mdift2[i - 1])
            )
            t2_line = (
                np.isfinite(ch3)
                and CH1[i] > ch3 > ch2
                and m3_val < mh3
                and red
                and (i == 0 or mdift3[i] >= mdift3[i - 1])
            )
            bar2 = (
                np.isfinite(bardh3) and np.isfinite(BARDH1[i])
                and BARDH1[i] < bardh3
            )
            T1[i] = t1_line and bar_ok
            T2[i] = t2_line and bar2

            if i > 0 and (T1[i - 1] or T2[i - 1]):
                tg = False
                if T1[i - 1] and np.isfinite(mdift2[i]) and np.isfinite(mdift2[i - 1]):
                    if mdift2[i] < mdift2[i - 1]:
                        tg = True
                elif T2[i - 1] and np.isfinite(mdift3[i]) and np.isfinite(mdift3[i - 1]):
                    if mdift3[i] < mdift3[i - 1]:
                        tg = True

                if tg:
                    invalidated = False
                    if T1[i - 1] and np.isfinite(DDXH1[i]) and np.isfinite(ddxh2):
                        if DDXH1[i] >= ddxh2:
                            invalidated = True
                    if T2[i - 1] and np.isfinite(ddxh3) and np.isfinite(DDXH1[i]):
                        if DDXH1[i] >= ddxh3:
                            invalidated = True
                    if not invalidated and not _recent(result, i, "top_divergence", dedup):
                        _write_divergence(result, i, "top_divergence", target_indices)

        if not np.isfinite(CL1[i]) or not np.isfinite(DDXL2[i]):
            continue

        cl2 = CL2[i]
        ddxl2 = DDXL2[i]
        bardl2 = BARDL2[i]
        cl3 = CL3[i]
        ddxl3 = DDXL3[i]
        bardl3 = BARDL3[i]

        mb2_val = mdif_part(ddx[i], ddxl2)
        ml2 = mdif_part(ddxl2, ddxl2)
        mdifb2[i] = mb2_val

        mb3_val = mdif_part(ddx[i], ddxl3) if np.isfinite(ddxl3) else 0
        ml3 = mdif_part(ddxl3, ddxl3) if np.isfinite(ddxl3) else 0
        mdifb3[i] = mb3_val

        green = ddx[i] < 0 and (i == 0 or ddx[i - 1] < 0)
        bar_ok_b = (
            np.isfinite(bardl2) and np.isfinite(BARDL1[i])
            and BARDL1[i] > bardl2
        )

        b1_line = (
            CL1[i] < cl2
            and mb2_val > ml2
            and green
            and (i == 0 or mdifb2[i] <= mdifb2[i - 1])
        )
        b2_line = (
            np.isfinite(cl3)
            and CL1[i] < cl3 < cl2
            and mb3_val > ml3
            and green
            and (i == 0 or mdifb3[i] <= mdifb3[i - 1])
        )
        bar2_b = (
            np.isfinite(bardl3) and np.isfinite(BARDL1[i])
            and BARDL1[i] > bardl3
        )
        B1[i] = b1_line and bar_ok_b
        B2[i] = b2_line and bar2_b

        if i > 0 and (B1[i - 1] or B2[i - 1]):
            tg_b = False
            if B1[i - 1] and np.isfinite(mdifb2[i]) and np.isfinite(mdifb2[i - 1]):
                if mdifb2[i] > mdifb2[i - 1]:
                    tg_b = True
            elif B2[i - 1] and np.isfinite(mdifb3[i]) and np.isfinite(mdifb3[i - 1]):
                if mdifb3[i] > mdifb3[i - 1]:
                    tg_b = True

            if tg_b:
                invalidated_b = False
                if B1[i - 1] and np.isfinite(DDXL1[i]) and np.isfinite(ddxl2):
                    if DDXL1[i] <= ddxl2:
                        invalidated_b = True
                if B2[i - 1] and np.isfinite(ddxl3) and np.isfinite(DDXL1[i]):
                    if DDXL1[i] <= ddxl3:
                        invalidated_b = True
                if not invalidated_b and not _recent(result, i, "bottom_divergence", dedup):
                    _write_divergence(result, i, "bottom_divergence", target_indices)

    return result
