"""Chunk-level preflight to fast-skip stocks that would all route to SKIP."""
import logging
from typing import Dict, List, Optional, Tuple

import pandas as pd

from backend.etl.calc_router import SIG_WINDOW, classify_calc_mode, classify_calc_mode_detail
from backend.etl.calc_indicators import CALC_ROUTE_SPECS
from backend.etl.calc_dde import DDECalculator

logger = logging.getLogger(__name__)


def _tail_frame(df: Optional[pd.DataFrame], window: int = SIG_WINDOW) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return df
    return df.tail(window).reset_index(drop=True)


def batch_load_quote_tails(con, ts_codes: List[str], freq: str,
                           columns: List[str], window: int = SIG_WINDOW) -> dict:
    """Load the latest ``window`` bars per stock (no calc_date upper bound)."""
    if not ts_codes:
        return {}
    groups = {}
    cols = ["trade_date"] + [c for c in columns if c != "trade_date"]
    cols_csv = ", ".join(cols)
    for i in range(0, len(ts_codes), 400):
        chunk = ts_codes[i:i + 400]
        ph = ",".join(["?"] * len(chunk))
        if freq == "weekly":
            d_cols = ", ".join("d." + c for c in columns)
            query = f"""
                WITH ranked AS (
                    SELECT d.ts_code, {d_cols},
                           ROW_NUMBER() OVER (
                               PARTITION BY d.ts_code ORDER BY d.trade_date DESC
                           ) AS rn
                    FROM dwd_weekly_quote d
                    JOIN dim_date dd ON d.trade_date = dd.trade_date
                    WHERE d.ts_code IN ({ph}) AND dd.is_week_end = 1
                )
                SELECT ts_code, {cols_csv}
                FROM ranked WHERE rn <= ?
                ORDER BY ts_code, trade_date
            """
            params = list(chunk) + [window]
        else:
            query = f"""
                WITH ranked AS (
                    SELECT ts_code, {cols_csv},
                           ROW_NUMBER() OVER (
                               PARTITION BY ts_code ORDER BY trade_date DESC
                           ) AS rn
                    FROM dwd_daily_quote
                    WHERE ts_code IN ({ph}) AND is_suspended = 0
                )
                SELECT ts_code, {cols_csv}
                FROM ranked WHERE rn <= ?
                ORDER BY ts_code, trade_date
            """
            params = list(chunk) + [window]
        big = con.execute(query, params).df()
        if big.empty:
            continue
        for ts_code, g in big.groupby("ts_code", sort=False):
            groups[ts_code] = g.drop(columns=["ts_code"]).reset_index(drop=True)
    return groups


def batch_load_dde_tails(con, ts_codes: List[str], freq: str,
                         window: int = SIG_WINDOW) -> dict:
    """Load DDE frames via existing batch loaders, then keep the latest window."""
    if not ts_codes:
        return {}
    calc = DDECalculator(con, freq)
    if freq == "daily":
        return calc._load_daily_batch(ts_codes, tail_window=window)
    return calc._load_weekly_batch(ts_codes, tail_window=window)


def _classify_indicator_preflight_with_fp(
    ts_code: str,
    indicator_name: str,
    freq: str,
    sig_cols: list,
    source: str,
    state: Optional[dict],
    daily_q: Optional[pd.DataFrame],
    weekly_q: Optional[pd.DataFrame],
    daily_dde: Optional[pd.DataFrame],
    weekly_dde: Optional[pd.DataFrame],
    expected_spec_version: str = "v1",
) -> Optional[Tuple[str, list, Optional[str]]]:
    """Return (mode, new_bars, cur_fp) or None when slow-path required."""
    if source == "quote":
        df = daily_q if freq == "daily" else weekly_q
        if df is None or len(df) == 0:
            return None
        return classify_calc_mode_detail(
            df, state, sig_cols, expected_spec_version=expected_spec_version)

    df = daily_dde if freq == "daily" else weekly_dde
    if df is None or len(df) == 0:
        if ts_code.endswith(".BJ"):
            return "SKIP", [], None
        return None
    return classify_calc_mode_detail(
        df, state, sig_cols, expected_spec_version=expected_spec_version)


def _classify_indicator_preflight(
    ts_code: str,
    indicator_name: str,
    freq: str,
    sig_cols: list,
    source: str,
    state: Optional[dict],
    daily_q: Optional[pd.DataFrame],
    weekly_q: Optional[pd.DataFrame],
    daily_dde: Optional[pd.DataFrame],
    weekly_dde: Optional[pd.DataFrame],
    expected_spec_version: str = "v1",
) -> Optional[Tuple[str, list]]:
    """Return (mode, new_bars) or None when slow-path required."""
    out = _classify_indicator_preflight_with_fp(
        ts_code, indicator_name, freq, sig_cols, source, state,
        daily_q, weekly_q, daily_dde, weekly_dde,
        expected_spec_version=expected_spec_version,
    )
    if out is None:
        return None
    mode, new_bars, _ = out
    return mode, new_bars


def preflight_stock_modes(
    ts_code: str,
    state_map: Dict[Tuple[str, str, str], dict],
    daily_q: Optional[pd.DataFrame],
    weekly_q: Optional[pd.DataFrame],
    daily_dde: Optional[pd.DataFrame],
    weekly_dde: Optional[pd.DataFrame],
    specs=CALC_ROUTE_SPECS,
) -> Optional[Dict[Tuple[str, str], Tuple[str, list]]]:
    """Classify all indicators for one stock. None → fallthrough (missing/empty frame)."""
    modes = {}
    for indicator_name, freq, CalcCls, sig_cols, source in specs:
        if source == "quote":
            df = daily_q if freq == "daily" else weekly_q
        else:
            df = daily_dde if freq == "daily" else weekly_dde
        if df is None or len(df) == 0:
            return None
        state = state_map.get((ts_code, freq, indicator_name))
        spec_ver = getattr(CalcCls, "SPEC_VERSION", "v1")
        mode, new_bars = classify_calc_mode(
            df, state, sig_cols, expected_spec_version=spec_ver)
        modes[(indicator_name, freq)] = (mode, new_bars)
    return modes


def preflight_stock_modes_with_fps(
    ts_code: str,
    state_map: Dict[Tuple[str, str, str], dict],
    daily_q: Optional[pd.DataFrame],
    weekly_q: Optional[pd.DataFrame],
    daily_dde: Optional[pd.DataFrame],
    weekly_dde: Optional[pd.DataFrame],
    specs=CALC_ROUTE_SPECS,
) -> Tuple[Optional[Dict[Tuple[str, str], Tuple[str, list]]], Dict[Tuple[str, str], str]]:
    """v2 preflight with per-indicator fingerprint cache for skip_refresh reuse."""
    modes: Dict[Tuple[str, str], Tuple[str, list]] = {}
    fps: Dict[Tuple[str, str], str] = {}
    for indicator_name, freq, CalcCls, sig_cols, source in specs:
        state = state_map.get((ts_code, freq, indicator_name))
        spec_ver = getattr(CalcCls, "SPEC_VERSION", "v1")
        out = _classify_indicator_preflight_with_fp(
            ts_code, indicator_name, freq, sig_cols, source, state,
            daily_q, weekly_q, daily_dde, weekly_dde,
            expected_spec_version=spec_ver,
        )
        if out is None:
            return None, {}
        mode, new_bars, cur_fp = out
        modes[(indicator_name, freq)] = (mode, new_bars)
        if cur_fp is not None:
            fps[(indicator_name, freq)] = cur_fp
    return modes, fps


def preflight_stock_modes_v2(
    ts_code: str,
    state_map: Dict[Tuple[str, str, str], dict],
    daily_q: Optional[pd.DataFrame],
    weekly_q: Optional[pd.DataFrame],
    daily_dde: Optional[pd.DataFrame],
    weekly_dde: Optional[pd.DataFrame],
    specs=CALC_ROUTE_SPECS,
) -> Optional[Dict[Tuple[str, str], Tuple[str, list]]]:
    """v2 preflight: BSE empty DDE → per-indicator SKIP instead of whole-stock None."""
    modes, _ = preflight_stock_modes_with_fps(
        ts_code, state_map, daily_q, weekly_q, daily_dde, weekly_dde, specs=specs,
    )
    return modes


def build_skip_state_records(
    stock_modes: Dict[str, Dict[Tuple[str, str], Tuple[str, list]]],
    fp_cache_by_stock: Dict[str, Dict[Tuple[str, str], str]],
    state_map: Dict[Tuple[str, str, str], dict],
    calc_date: str,
    daily_tails: dict,
    weekly_tails: dict,
    dde_daily: dict,
    dde_weekly: dict,
) -> list:
    """Build UPSERT rows for SKIP indicators using preflight fp cache."""
    from backend.config import CALC_SKIP_STATE_REFRESH
    from backend.etl.calc_state import should_refresh_calc_state

    records = []
    total = len(stock_modes)
    for i, (ts_code, modes) in enumerate(stock_modes.items()):
        if total > 1000 and i > 0 and i % 500 == 0:
            logger.info(
                "progress calc.batch_state: building records %d/%d", i, total,
            )
        fps = fp_cache_by_stock.get(ts_code, {})
        for (indicator_name, freq), (mode, _) in modes.items():
            if mode != "SKIP":
                continue
            fp = fps.get((indicator_name, freq))
            if fp is None:
                continue
            st = state_map.get((ts_code, freq, indicator_name))
            if st is None:
                continue
            spec = next(
                s for s in CALC_ROUTE_SPECS
                if s[0] == indicator_name and s[1] == freq
            )
            _, _, CalcCls, _, _ = spec
            spec_ver = getattr(CalcCls, "SPEC_VERSION", "v1")
            if not CALC_SKIP_STATE_REFRESH or should_refresh_calc_state(st, calc_date, fp):
                records.append((
                    ts_code, freq, indicator_name, st["last_trade_date"], fp,
                    calc_date, None, spec_ver,
                ))
    return records


def partition_preflight_modes(
    modes: Dict[Tuple[str, str], Tuple[str, list]],
) -> Tuple[set, set]:
    """Return (skip_keys, run_keys) where run_keys need APPEND/FULL."""
    skip_keys = set()
    run_keys = set()
    for key, (mode, _) in modes.items():
        if mode == "SKIP":
            skip_keys.add(key)
        else:
            run_keys.add(key)
    return skip_keys, run_keys


def stock_can_fast_skip(modes: Dict[Tuple[str, str], Tuple[str, list]]) -> bool:
    return all(m == "SKIP" for m, _ in modes.values())
