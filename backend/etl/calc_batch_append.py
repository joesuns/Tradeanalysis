"""Cross-stock batch APPEND orchestration."""
import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from backend.etl.base import CalcResult, SkipReason, insert_dws_batch, insert_dws_batch_multi
from backend.etl.calc_batch_seeds import load_ema_seeds_batch, load_zone_seeds_batch

logger = logging.getLogger(__name__)

ModeMap = Dict[str, Dict[Tuple[str, str], str]]


def partition_stocks_by_mode(
    stock_modes: ModeMap,
    indicator: str,
    freq: str,
) -> Tuple[List[str], List[str], List[str]]:
    """Split ts_codes into (append_list, full_list, skip_list) for one indicator×freq."""
    append, full, skip = [], [], []
    for ts_code, modes in stock_modes.items():
        m = modes.get((indicator, freq), "FULL")
        if m == "APPEND":
            append.append(ts_code)
        elif m == "SKIP":
            skip.append(ts_code)
        else:
            full.append(ts_code)
    return append, full, skip


def _batch_append_loop(
    calc,
    ts_codes: List[str],
    calc_date: str,
    data_groups: dict,
    new_bars_map: dict,
    compute_fn,
    dws_cols: Optional[List[str]] = None,
    float_cols: Optional[List[str]] = None,
):
    """Shared batch APPEND: compute per stock, INSERT once via insert_dws_batch_multi."""
    from backend.etl.base import compute_history_signature

    if dws_cols is None or float_cols is None:
        raise ValueError("dws_cols and float_cols required for batch append loop")

    stock_rows = []
    for ts_code in ts_codes:
        df = data_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            continue
        out = compute_fn(calc, ts_code, df, new_bars)
        fp = compute_history_signature(out, calc.SIGNATURE_COLS)
        stock_rows.append((ts_code, out, fp, new_bars[0], new_bars[-1]))

    agg = CalcResult()
    if not stock_rows:
        return agg

    n = insert_dws_batch_multi(
        calc.con, calc.dws_table, stock_rows, calc_date, dws_cols, float_cols,
    )
    agg.calculated = n
    return agg


def batch_append_macd(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    quote_groups: dict,
    new_bars_map: dict,
    state_map: Optional[dict] = None,
):
    """Cross-stock MACD APPEND for one freq.

    quote_groups must be pre-loaded (e.g. via load_quote_groups). Seeds are
    batch-loaded at the bar before each tail window's first trade_date — same
    anchor as resolve_ema_seeds / append_calculate.
    """
    from backend.etl.calc_macd import MACDCalculator

    if state_map is None:
        state_map = {}

    from backend.etl.base import compute_history_signature

    calc = MACDCalculator(con, freq)
    seed_cols = ("ema_12", "ema_26", "dea")
    stock_rows = []

    anchor_groups = {}
    for ts_code in ts_codes:
        df = quote_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            continue
        first_td = str(df.iloc[0]["trade_date"])
        anchor_groups.setdefault(first_td, []).append(ts_code)

    seeds_by_code = {}
    for first_td, codes_at_anchor in anchor_groups.items():
        batch_seeds = load_ema_seeds_batch(
            con, calc.dws_table, codes_at_anchor, first_td, seed_cols,
        )
        seeds_by_code.update(batch_seeds)

    for ts_code in ts_codes:
        df = quote_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            continue

        seeds = seeds_by_code.get(ts_code)
        out = calc._compute_indicators(df, ema_seeds=seeds)
        fp = compute_history_signature(out, calc.SIGNATURE_COLS)
        stock_rows.append((ts_code, out, fp, new_bars[0], new_bars[-1]))

    agg = CalcResult()
    if stock_rows:
        n = insert_dws_batch_multi(
            con, calc.dws_table, stock_rows, calc_date,
            MACDCalculator.DWS_COLS, MACDCalculator.FLOAT_COLS,
        )
        agg.calculated = n
    return agg


def batch_append_ma(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    quote_groups: dict,
    new_bars_map: dict,
    state_map: Optional[dict] = None,
):
    from backend.etl.calc_ma import MACalculator

    calc = MACalculator(con, freq)
    return _batch_append_loop(
        calc, ts_codes, calc_date, quote_groups, new_bars_map,
        lambda c, _code, df, new_bars: c._compute_indicators_append(df, new_bars),
        dws_cols=MACalculator.DWS_COLS, float_cols=MACalculator.FLOAT_COLS,
    )


def batch_append_kpattern(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    quote_groups: dict,
    new_bars_map: dict,
    state_map: Optional[dict] = None,
):
    from backend.etl.calc_kpattern import KPatternCalculator

    calc = KPatternCalculator(con, freq)

    def _compute(c, ts_code, df, new_bars):
        is_st = c._is_st_stock(ts_code) if c.con is not None else False
        return c._compute_patterns_append(df, new_bars, is_st)

    return _batch_append_loop(
        calc, ts_codes, calc_date, quote_groups, new_bars_map, _compute,
        dws_cols=KPatternCalculator.DWS_COLS, float_cols=KPatternCalculator.FLOAT_COLS,
    )


def batch_append_volume(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    quote_groups: dict,
    new_bars_map: dict,
    state_map: Optional[dict] = None,
):
    from backend.etl.calc_volume import VolumeCalculator

    calc = VolumeCalculator(con, freq)

    anchor_groups = {}
    for ts_code in ts_codes:
        df = quote_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            continue
        first_td = str(df.iloc[0]["trade_date"])
        anchor_groups.setdefault(first_td, []).append(ts_code)

    zone_seeds_by_code = {}
    for first_td, codes_at_anchor in anchor_groups.items():
        batch_seeds = load_zone_seeds_batch(
            con, calc.dws_table, codes_at_anchor, first_td,
        )
        zone_seeds_by_code.update(batch_seeds)

    def _compute(c, ts_code, df, _new_bars):
        zone_seed = zone_seeds_by_code.get(ts_code)
        return c._compute_indicators_append(df, zone_seed=zone_seed)

    return _batch_append_loop(
        calc, ts_codes, calc_date, quote_groups, new_bars_map, _compute,
        dws_cols=VolumeCalculator.DWS_COLS, float_cols=VolumeCalculator.FLOAT_COLS,
    )


def batch_append_priceposition(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    quote_groups: dict,
    new_bars_map: dict,
    state_map: Optional[dict] = None,
):
    from backend.etl.calc_price_position import PricePositionCalculator

    calc = PricePositionCalculator(con, freq)
    return _batch_append_loop(
        calc, ts_codes, calc_date, quote_groups, new_bars_map,
        lambda c, _code, df, new_bars: c._compute_positions_append(df, new_bars),
        dws_cols=PricePositionCalculator.DWS_COLS,
        float_cols=PricePositionCalculator.FLOAT_COLS,
    )


def batch_append_dde(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    dde_groups: dict,
    new_bars_map: dict,
    state_map: Optional[dict] = None,
):
    """Cross-stock DDE APPEND — dde_groups from _load_daily/_weekly_batch."""
    from backend.etl.base import compute_history_signature
    from backend.etl.calc_dde import DDECalculator

    calc = DDECalculator(con, freq)
    seed_cols = ("ddx2",)
    stock_rows = []

    anchor_groups = {}
    for ts_code in ts_codes:
        df = dde_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            continue
        first_td = str(df.iloc[0]["trade_date"])
        anchor_groups.setdefault(first_td, []).append(ts_code)

    seeds_by_code = {}
    for first_td, codes_at_anchor in anchor_groups.items():
        batch_seeds = load_ema_seeds_batch(
            con, calc.dws_table, codes_at_anchor, first_td, seed_cols,
        )
        seeds_by_code.update(batch_seeds)

    for ts_code in ts_codes:
        df = dde_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            continue

        seeds = seeds_by_code.get(ts_code)
        out = calc._compute_indicators(df, ema_seeds=seeds)
        fp = compute_history_signature(out, calc.SIGNATURE_COLS)
        stock_rows.append((ts_code, out, fp, new_bars[0], new_bars[-1]))

    agg = CalcResult()
    if stock_rows:
        n = insert_dws_batch_multi(
            con, calc.dws_table, stock_rows, calc_date,
            DDECalculator.DWS_COLS, DDECalculator.FLOAT_COLS,
        )
        agg.calculated = n
    return agg


BATCH_APPEND_FNS = {
    "macd": batch_append_macd,
    "ma": batch_append_ma,
    "kpattern": batch_append_kpattern,
    "volume": batch_append_volume,
    "priceposition": batch_append_priceposition,
    "dde": batch_append_dde,
}


def run_batch_append_phase(con, codes: List[str], calc_date: str) -> Optional[dict]:
    """Batch APPEND + SKIP for all codes before per-stock chunk workers.

    Returns context dict with chunk_codes, completed_keys, agg_by_key, and
    preloaded tail frames; None when batch path is inactive.
    """
    from backend.config import CALC_APPEND, CALC_BATCH_APPEND, CALC_SKIP_STATE_REFRESH
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS, quote_tail_columns
    from backend.etl.calc_fast_skip import (
        batch_load_dde_tails,
        batch_load_quote_tails,
        partition_preflight_modes,
        preflight_stock_modes_v2,
    )
    from backend.etl.calc_router import state_signature
    from backend.etl.calc_state import (
        load_calc_state_batch,
        should_refresh_calc_state,
        upsert_calc_state_batch,
        write_calc_state_from_df,
    )

    if not (CALC_APPEND and CALC_BATCH_APPEND):
        return None

    if not codes:
        return {
            "chunk_codes": [],
            "completed_keys": set(),
            "agg_by_key": {},
            "stock_modes": {},
            "state_map": {},
            "daily_tails": {},
            "weekly_tails": {},
            "dde_daily": {},
            "dde_weekly": {},
        }

    logger.info("progress calc.batch_append: started | stocks=%d", len(codes))
    t0 = time.monotonic()

    state_map = load_calc_state_batch(con, codes)
    tail_cols = quote_tail_columns()
    daily_tails = batch_load_quote_tails(con, codes, "daily", tail_cols)
    weekly_tails = batch_load_quote_tails(con, codes, "weekly", tail_cols)
    dde_daily = batch_load_dde_tails(con, codes, "daily")
    dde_weekly = batch_load_dde_tails(con, codes, "weekly")

    stock_modes: Dict[str, Dict[Tuple[str, str], Tuple[str, list]]] = {}
    chunk_codes: Set[str] = set()
    completed_keys: Set[Tuple[str, str, str]] = set()
    agg_by_key = defaultdict(CalcResult)

    from backend.etl.progress import stock_progress
    preflight = stock_progress("calc.batch_preflight", len(codes))
    preflight.log_start()
    for ts_code in codes:
        modes = preflight_stock_modes_v2(
            ts_code, state_map,
            daily_tails.get(ts_code), weekly_tails.get(ts_code),
            dde_daily.get(ts_code), dde_weekly.get(ts_code),
        )
        if modes is None:
            chunk_codes.add(ts_code)
            preflight.tick()
            continue
        stock_modes[ts_code] = modes
        if any(mode == "FULL" for mode, _ in modes.values()):
            chunk_codes.add(ts_code)
        preflight.tick()
    preflight.log_done()

    state_records = []
    for ts_code, modes in stock_modes.items():
        for (indicator_name, freq), (mode, _) in modes.items():
            if mode != "SKIP":
                continue
            completed_keys.add((ts_code, indicator_name, freq))
            agg_by_key[(indicator_name, freq)].add_skip(
                SkipReason.FINGERPRINT_MATCH, ts_code, "batch_append: preflight skip",
            )
            spec = next(
                s for s in CALC_ROUTE_SPECS
                if s[0] == indicator_name and s[1] == freq
            )
            _, _, _, sig_cols, source = spec
            st = state_map.get((ts_code, freq, indicator_name))
            if st is None:
                continue
            if source == "quote":
                tdf = daily_tails.get(ts_code) if freq == "daily" else weekly_tails.get(ts_code)
            else:
                tdf = dde_daily.get(ts_code) if freq == "daily" else dde_weekly.get(ts_code)
            if tdf is None or tdf.empty:
                continue
            fp = state_signature(tdf, st["last_trade_date"], sig_cols)
            if not CALC_SKIP_STATE_REFRESH or should_refresh_calc_state(st, calc_date, fp):
                state_records.append((
                    ts_code, freq, indicator_name, st["last_trade_date"], fp, calc_date, None,
                ))
    upsert_calc_state_batch(con, state_records)

    for indicator_name, freq, CalcCls, sig_cols, source in CALC_ROUTE_SPECS:
        append_codes: List[str] = []
        new_bars_map: Dict[str, list] = {}
        for ts_code, modes in stock_modes.items():
            mode, new_bars = modes.get((indicator_name, freq), ("FULL", []))
            if mode != "APPEND":
                continue
            append_codes.append(ts_code)
            new_bars_map[ts_code] = new_bars

        if not append_codes:
            continue

        if source == "quote":
            tails = daily_tails if freq == "daily" else weekly_tails
            data_groups = {c: tails[c] for c in append_codes if c in tails}
        else:
            tails = dde_daily if freq == "daily" else dde_weekly
            data_groups = {c: tails[c] for c in append_codes if c in tails}

        batch_fn = BATCH_APPEND_FNS[indicator_name]
        result = batch_fn(
            con, freq, append_codes, calc_date, data_groups, new_bars_map, state_map,
        )
        agg_by_key[(indicator_name, freq)].calculated += result.calculated
        logger.info(
            "progress calc.batch_append: %s %s | append=%d calculated=%d",
            indicator_name, freq, len(append_codes), result.calculated,
        )

        calc = CalcCls(con, freq)
        for ts_code in append_codes:
            completed_keys.add((ts_code, indicator_name, freq))
            tdf = data_groups.get(ts_code)
            if tdf is None or len(tdf) == 0:
                continue
            write_calc_state_from_df(
                con, ts_code, freq, indicator_name, tdf, calc.SIGNATURE_COLS, calc_date,
            )

    logger.info(
        "progress calc.batch_append: done | %.0fs | chunk=%d batch_only=%d",
        time.monotonic() - t0,
        len(chunk_codes),
        len(codes) - len(chunk_codes),
    )
    return {
        "chunk_codes": sorted(chunk_codes),
        "completed_keys": completed_keys,
        "agg_by_key": dict(agg_by_key),
        "stock_modes": stock_modes,
        "state_map": state_map,
        "daily_tails": daily_tails,
        "weekly_tails": weekly_tails,
        "dde_daily": dde_daily,
        "dde_weekly": dde_weekly,
    }
