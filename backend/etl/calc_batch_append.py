"""Cross-stock batch APPEND orchestration."""
import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from backend.etl.base import CalcResult, SkipReason, insert_dws_batch, insert_dws_batch_multi
from backend.etl.calc_batch_seeds import load_ema_seeds_batch, load_zone_seeds_batch

logger = logging.getLogger(__name__)

ModeMap = Dict[str, Dict[Tuple[str, str], str]]

BATCH_INDICATOR_ZH = {
    ("macd", "daily"): "MACD日线",
    ("macd", "weekly"): "MACD周线",
    ("ma", "daily"): "均线日线",
    ("ma", "weekly"): "均线周线",
    ("kpattern", "daily"): "K线形态日线",
    ("kpattern", "weekly"): "K线形态周线",
    ("volume", "daily"): "量能日线",
    ("volume", "weekly"): "量能周线",
    ("priceposition", "daily"): "价格位置日线",
    ("priceposition", "weekly"): "价格位置周线",
    ("dde", "daily"): "DDE日线",
    ("dde", "weekly"): "DDE周线",
}

BATCH_TAIL_ZH = {
    "state": "计算状态",
    "quote_daily": "日线行情尾窗",
    "quote_weekly": "周线行情尾窗",
    "dde_daily": "DDE日线尾窗",
    "dde_weekly": "DDE周线尾窗",
    "skip_refresh": "跳过路径状态刷新",
}


def _batch_label_zh(indicator_name: str, freq: str) -> str:
    return BATCH_INDICATOR_ZH.get((indicator_name, freq), f"{indicator_name}_{freq}")


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
    label_zh: str = "",
):
    """Shared batch APPEND: compute per stock, INSERT once via insert_dws_batch_multi."""
    from backend.etl.base import compute_history_signature
    from backend.etl.progress import stock_progress

    if dws_cols is None or float_cols is None:
        raise ValueError("dws_cols and float_cols required for batch append loop")

    prog = stock_progress("calc.batch_compute", len(ts_codes), detail=label_zh)
    prog.log_start()

    stock_rows = []
    for ts_code in ts_codes:
        df = data_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            prog.tick()
            continue
        out = compute_fn(calc, ts_code, df, new_bars)
        fp = compute_history_signature(out, calc.SIGNATURE_COLS)
        stock_rows.append((ts_code, out, fp, new_bars[0], new_bars[-1]))
        prog.tick()

    agg = CalcResult()
    if not stock_rows:
        prog.log_done(写入行=0)
        return agg, []

    logger.info(
        "progress calc.batch_compute: %s | 批量写入 %d 股",
        label_zh or "batch", len(stock_rows),
    )
    n = insert_dws_batch_multi(
        calc.con, calc.dws_table, stock_rows, calc_date, dws_cols, float_cols,
    )
    agg.calculated = n
    prog.log_done(写入行=n)
    return agg, stock_rows


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

    daily_b4_groups: dict = {}
    if freq == "weekly" and ts_codes:
        b4_start = calc._weekly_b4_daily_start(calc_date)
        daily_b4_groups = calc._load_daily_for_b4_batch(
            ts_codes, start_date=b4_start, end_date=calc_date,
        )

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

    from backend.config import CALC_VECTOR_APPEND
    from backend.etl.progress import stock_progress

    label_zh = _batch_label_zh("macd", freq)
    prog = stock_progress("calc.batch_compute", len(ts_codes), detail=label_zh)
    prog.log_start()

    vector_cores = {}
    if CALC_VECTOR_APPEND:
        from backend.etl.vector.macd_batch import batch_macd_ema_core
        vector_cores = batch_macd_ema_core(ts_codes, quote_groups, seeds_by_code)

    for ts_code in ts_codes:
        df = quote_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            prog.tick()
            continue

        seeds = seeds_by_code.get(ts_code)
        daily_b4 = daily_b4_groups.get(ts_code) if freq == "weekly" else None
        core = vector_cores.get(ts_code)
        if core is not None:
            import numpy as np
            from backend.etl.vector.macd_batch import attach_macd_core_to_df
            from backend.etl.calc_macd import require_b4_weekly_target_indices

            base = attach_macd_core_to_df(df, core)
            if freq == "weekly" and new_bars:
                target_idx = set(require_b4_weekly_target_indices(
                    base, new_bars, ts_code=ts_code,
                ))
            else:
                td_set = set(base["trade_date"].astype(str).values)
                target_idx = {
                    int(np.where(base["trade_date"].astype(str).values == td)[0][0])
                    for td in new_bars
                    if td in td_set
                }
            b4_target = target_idx if freq == "weekly" else None
            out = calc._compute_macd_derived(
                base,
                daily_for_b4=daily_b4,
                target_indices=target_idx or None,
                b4_target_indices=b4_target,
            )
        else:
            if freq == "weekly" and new_bars:
                from backend.etl.calc_macd import require_b4_weekly_target_indices

                target_idx = set(require_b4_weekly_target_indices(
                    df, new_bars, ts_code=ts_code,
                ))
                base = calc._compute_macd_core(df, ema_seeds=seeds)
                out = calc._compute_macd_derived(
                    base,
                    daily_for_b4=daily_b4,
                    target_indices=target_idx,
                    b4_target_indices=target_idx,
                )
            else:
                out = calc._compute_indicators(
                    df, ema_seeds=seeds, daily_for_b4=daily_b4,
                )
        fp = compute_history_signature(out, calc.SIGNATURE_COLS)
        stock_rows.append((ts_code, out, fp, new_bars[0], new_bars[-1]))
        prog.tick()

    agg = CalcResult()
    if stock_rows:
        logger.info(
            "progress calc.batch_compute: %s | 批量写入 %d 股",
            label_zh, len(stock_rows),
        )
        n = insert_dws_batch_multi(
            con, calc.dws_table, stock_rows, calc_date,
            MACDCalculator.DWS_COLS, MACDCalculator.FLOAT_COLS,
        )
        agg.calculated = n
        prog.log_done(写入行=n)
    else:
        prog.log_done(写入行=0)
    return agg, stock_rows


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
        label_zh=_batch_label_zh("ma", freq),
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
        label_zh=_batch_label_zh("kpattern", freq),
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
    from backend.etl.base import compute_history_signature
    from backend.etl.calc_volume import VolumeCalculator, require_trend_target_indices

    calc = VolumeCalculator(con, freq)
    stock_rows = []

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

    from backend.config import CALC_VECTOR_APPEND
    from backend.etl.progress import stock_progress

    label_zh = _batch_label_zh("volume", freq)
    prog = stock_progress("calc.batch_compute", len(ts_codes), detail=label_zh)
    prog.log_start()

    vector_cores = {}
    if CALC_VECTOR_APPEND:
        from backend.etl.vector.volume_batch import batch_volume_rolling_core
        vector_cores = batch_volume_rolling_core(ts_codes, quote_groups)

    for ts_code in ts_codes:
        df = quote_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            prog.tick()
            continue

        zone_seed = zone_seeds_by_code.get(ts_code)
        core = vector_cores.get(ts_code)
        if core is not None:
            from backend.etl.vector.volume_batch import attach_volume_core_to_df
            trend_target = require_trend_target_indices(
                df, new_bars, ts_code=ts_code,
            )
            base = attach_volume_core_to_df(df, core)
            out = calc._compute_volume_derived(
                base, zone_seed=zone_seed, trend_target_indices=trend_target,
            )
        else:
            out = calc._compute_indicators_append(
                df, new_bars, zone_seed=zone_seed, ts_code=ts_code,
            )
        fp = compute_history_signature(out, calc.SIGNATURE_COLS)
        stock_rows.append((ts_code, out, fp, new_bars[0], new_bars[-1]))
        prog.tick()

    agg = CalcResult()
    if stock_rows:
        logger.info(
            "progress calc.batch_compute: %s | 批量写入 %d 股",
            label_zh, len(stock_rows),
        )
        n = insert_dws_batch_multi(
            con, calc.dws_table, stock_rows, calc_date,
            VolumeCalculator.DWS_COLS, VolumeCalculator.FLOAT_COLS,
        )
        agg.calculated = n
        prog.log_done(写入行=n)
    else:
        prog.log_done(写入行=0)
    return agg, stock_rows


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
        label_zh=_batch_label_zh("priceposition", freq),
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

    daily_trend_groups: dict = {}
    if freq == "weekly" and ts_codes:
        daily_trend_groups = calc._load_daily_for_trend_batch(
            ts_codes, end_date=calc_date,
        )

    from backend.config import CALC_VECTOR_APPEND
    from backend.etl.progress import stock_progress

    label_zh = _batch_label_zh("dde", freq)
    prog = stock_progress("calc.batch_compute", len(ts_codes), detail=label_zh)
    prog.log_start()

    vector_cores = {}
    if CALC_VECTOR_APPEND:
        from backend.etl.vector.dde_batch import batch_ddx_ddx2_core
        vector_cores = batch_ddx_ddx2_core(ts_codes, dde_groups, seeds_by_code)

    for ts_code in ts_codes:
        df = dde_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            prog.tick()
            continue

        seeds = seeds_by_code.get(ts_code)
        daily_trend = daily_trend_groups.get(ts_code) if freq == "weekly" else None
        core = vector_cores.get(ts_code)
        if core is not None:
            import numpy as np
            from backend.etl.vector.dde_batch import attach_dde_core_to_df
            base = attach_dde_core_to_df(df, core)
            td_set = set(base["trade_date"].astype(str).values)
            target_idx = {
                int(np.where(base["trade_date"].astype(str).values == td)[0][0])
                for td in new_bars
                if td in td_set
            }
            out = calc._compute_dde_derived(
                base,
                daily_for_trend=daily_trend,
                calc_date=calc_date,
                target_indices=target_idx or None,
            )
        else:
            out = calc._compute_indicators(
                df, ema_seeds=seeds, daily_for_trend=daily_trend, calc_date=calc_date,
            )
        fp = compute_history_signature(out, calc.SIGNATURE_COLS)
        stock_rows.append((ts_code, out, fp, new_bars[0], new_bars[-1]))
        prog.tick()

    agg = CalcResult()
    if stock_rows:
        logger.info(
            "progress calc.batch_compute: %s | 批量写入 %d 股",
            label_zh, len(stock_rows),
        )
        n = insert_dws_batch_multi(
            con, calc.dws_table, stock_rows, calc_date,
            DDECalculator.DWS_COLS, DDECalculator.FLOAT_COLS,
        )
        agg.calculated = n
        prog.log_done(写入行=n)
    else:
        prog.log_done(写入行=0)
    return agg, stock_rows


BATCH_APPEND_FNS = {
    "macd": batch_append_macd,
    "ma": batch_append_ma,
    "kpattern": batch_append_kpattern,
    "volume": batch_append_volume,
    "priceposition": batch_append_priceposition,
    "dde": batch_append_dde,
}


def _batch_full_loop(
    calc,
    ts_codes: List[str],
    calc_date: str,
    recalc_start: Optional[str],
    data_groups: dict,
    compute_fn,
    dws_cols: List[str],
    float_cols: List[str],
    label_zh: str,
    min_rows: int = 1,
    spec_version: str = "v1",
    latest_fps: Optional[dict] = None,
    latest_specs: Optional[dict] = None,
    check_spec: bool = False,
):
    """Shared batch FULL: per-stock compute, one insert_dws_batch_multi narrow write."""
    from backend.etl.base import (
        check_dwd_unchanged,
        compute_input_fingerprint,
        insert_dws_batch_multi,
        load_latest_fingerprints,
        load_latest_spec_versions,
    )
    from backend.etl.progress import stock_progress

    if latest_fps is None:
        latest_fps = load_latest_fingerprints(calc.con, calc.dws_table, ts_codes)
    if check_spec and latest_specs is None:
        latest_specs = load_latest_spec_versions(calc.con, calc.dws_table, ts_codes)

    prog = stock_progress("calc.batch_full", len(ts_codes), detail=label_zh)
    prog.log_start()

    stock_rows = []
    agg = CalcResult()
    write_end = calc_date if recalc_start else None

    for ts_code in ts_codes:
        df = data_groups.get(ts_code)
        if df is None or len(df) == 0:
            agg.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
            prog.tick()
            continue
        if len(df) < min_rows:
            agg.add_skip(
                SkipReason.INSUFFICIENT_ROWS, ts_code,
                f"DWD rows={len(df)}, min={min_rows}",
            )
            prog.tick()
            continue

        unchanged_kwargs = {
            "latest_fps": latest_fps,
            "recalc_start": recalc_start,
        }
        if check_spec:
            unchanged_kwargs["expected_spec_version"] = spec_version
            unchanged_kwargs["latest_specs"] = latest_specs

        if check_dwd_unchanged(calc.con, calc.dws_table, ts_code, df, **unchanged_kwargs):
            agg.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code, "DWD fingerprint match")
            prog.tick()
            continue

        fp = compute_input_fingerprint(df, recalc_start=recalc_start)
        out = compute_fn(calc, ts_code, df)
        if out is None or len(out) == 0:
            prog.tick()
            continue
        stock_rows.append((ts_code, out, fp, recalc_start, write_end))
        prog.tick()

    if stock_rows:
        logger.info(
            "progress calc.batch_full: %s | 批量写入 %d 股",
            label_zh, len(stock_rows),
        )
        n = insert_dws_batch_multi(
            calc.con, calc.dws_table, stock_rows, calc_date,
            dws_cols, float_cols, spec_version=spec_version,
        )
        agg.calculated = len(stock_rows)
        prog.log_done(写入行=n)
    else:
        prog.log_done(写入行=0)
    return agg, stock_rows


def batch_full_macd(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    recalc_start: Optional[str],
    quote_groups: dict,
    state_map: Optional[dict] = None,
):
    from backend.etl.base import load_latest_fingerprints, load_latest_spec_versions
    from backend.etl.calc_macd import MACDCalculator

    calc = MACDCalculator(con, freq)
    daily_b4_groups: dict = {}
    if freq == "weekly" and ts_codes:
        b4_start = calc._weekly_b4_daily_start(calc_date)
        daily_b4_groups = calc._load_daily_for_b4_batch(
            ts_codes, start_date=b4_start, end_date=calc_date,
        )

    anchor_groups = {}
    for ts_code in ts_codes:
        df = quote_groups.get(ts_code)
        if df is None or len(df) == 0:
            continue
        first_td = str(df.iloc[0]["trade_date"])
        anchor_groups.setdefault(first_td, []).append(ts_code)

    seeds_by_code = {}
    seed_cols = ("ema_12", "ema_26", "dea")
    for first_td, codes_at_anchor in anchor_groups.items():
        batch_seeds = load_ema_seeds_batch(
            con, calc.dws_table, codes_at_anchor, first_td, seed_cols,
        )
        seeds_by_code.update(batch_seeds)

    def _compute(c, ts_code, df):
        seeds = seeds_by_code.get(ts_code)
        daily_b4 = daily_b4_groups.get(ts_code) if freq == "weekly" else None
        return c._compute_indicators(df, ema_seeds=seeds, daily_for_b4=daily_b4)

    return _batch_full_loop(
        calc, ts_codes, calc_date, recalc_start, quote_groups, _compute,
        MACDCalculator.DWS_COLS, MACDCalculator.FLOAT_COLS,
        _batch_label_zh("macd", freq), min_rows=27,
        spec_version=MACDCalculator.SPEC_VERSION, check_spec=True,
        latest_fps=load_latest_fingerprints(con, calc.dws_table, ts_codes),
        latest_specs=load_latest_spec_versions(con, calc.dws_table, ts_codes),
    )


def batch_full_ma(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    recalc_start: Optional[str],
    quote_groups: dict,
    state_map: Optional[dict] = None,
):
    from backend.etl.calc_ma import MACalculator

    calc = MACalculator(con, freq)
    return _batch_full_loop(
        calc, ts_codes, calc_date, recalc_start, quote_groups,
        lambda c, _code, df: c._compute_indicators(df),
        MACalculator.DWS_COLS, MACalculator.FLOAT_COLS,
        _batch_label_zh("ma", freq), min_rows=11,
    )


def batch_full_kpattern(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    recalc_start: Optional[str],
    quote_groups: dict,
    state_map: Optional[dict] = None,
):
    from backend.kpattern_params import KPATTERN_PARAMS
    from backend.etl.calc_kpattern import KPatternCalculator

    calc = KPatternCalculator(con, freq)
    min_rows = KPATTERN_PARAMS["common"]["min_data_rows"]

    def _compute(c, ts_code, df):
        is_st = c._is_st_stock(ts_code) if c.con is not None else False
        return c._compute_patterns(df, is_st)

    return _batch_full_loop(
        calc, ts_codes, calc_date, recalc_start, quote_groups, _compute,
        KPatternCalculator.DWS_COLS, KPatternCalculator.FLOAT_COLS,
        _batch_label_zh("kpattern", freq), min_rows=min_rows,
    )


def batch_full_volume(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    recalc_start: Optional[str],
    quote_groups: dict,
    state_map: Optional[dict] = None,
):
    from backend.etl.calc_volume import VolumeCalculator

    calc = VolumeCalculator(con, freq)
    return _batch_full_loop(
        calc, ts_codes, calc_date, recalc_start, quote_groups,
        lambda c, _code, df: c._compute_indicators(df),
        VolumeCalculator.DWS_COLS, VolumeCalculator.FLOAT_COLS,
        _batch_label_zh("volume", freq), min_rows=5,
        spec_version=VolumeCalculator.SPEC_VERSION,
    )


def batch_full_priceposition(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    recalc_start: Optional[str],
    quote_groups: dict,
    state_map: Optional[dict] = None,
):
    from backend.etl.calc_price_position import PricePositionCalculator

    calc = PricePositionCalculator(con, freq)
    return _batch_full_loop(
        calc, ts_codes, calc_date, recalc_start, quote_groups,
        lambda c, _code, df: c._compute_positions(df),
        PricePositionCalculator.DWS_COLS, PricePositionCalculator.FLOAT_COLS,
        _batch_label_zh("priceposition", freq), min_rows=2,
    )


def batch_full_dde(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    recalc_start: Optional[str],
    dde_groups: dict,
    state_map: Optional[dict] = None,
):
    import pandas as pd

    from backend.etl.base import load_latest_fingerprints, load_latest_spec_versions
    from backend.etl.calc_dde import DDECalculator

    calc = DDECalculator(con, freq)
    min_rows = 2 if freq == "weekly" else 10
    empty = pd.DataFrame()
    daily_trend_groups: dict = {}
    if freq == "weekly" and ts_codes:
        daily_trend_groups = calc._load_daily_for_trend_batch(
            ts_codes, end_date=calc_date,
        )

    anchor_groups = {}
    for ts_code in ts_codes:
        df = dde_groups.get(ts_code)
        if df is None or len(df) == 0:
            continue
        first_td = str(df.iloc[0]["trade_date"])
        anchor_groups.setdefault(first_td, []).append(ts_code)

    seeds_by_code = {}
    for first_td, codes_at_anchor in anchor_groups.items():
        batch_seeds = load_ema_seeds_batch(
            con, calc.dws_table, codes_at_anchor, first_td, ("ddx2",),
        )
        seeds_by_code.update(batch_seeds)

    def _compute(c, ts_code, df):
        if ts_code.endswith(".BJ"):
            return None
        seeds = seeds_by_code.get(ts_code)
        daily_trend = daily_trend_groups.get(ts_code, empty) if freq == "weekly" else None
        return c._compute_indicators(
            df, ema_seeds=seeds, daily_for_trend=daily_trend, calc_date=calc_date,
        )

    filtered_codes = [c for c in ts_codes if not c.endswith(".BJ")]
    agg, stock_rows = _batch_full_loop(
        calc, filtered_codes, calc_date, recalc_start, dde_groups, _compute,
        DDECalculator.DWS_COLS, DDECalculator.FLOAT_COLS,
        _batch_label_zh("dde", freq), min_rows=min_rows,
        spec_version=DDECalculator.SPEC_VERSION, check_spec=True,
        latest_fps=load_latest_fingerprints(con, calc.dws_table, filtered_codes),
        latest_specs=load_latest_spec_versions(con, calc.dws_table, filtered_codes),
    )
    for ts_code in ts_codes:
        if ts_code.endswith(".BJ"):
            agg.add_skip(
                SkipReason.SOURCE_UNAVAILABLE, ts_code,
                "BSE stocks have no moneyflow data from tushare",
            )
    return agg, stock_rows


BATCH_FULL_FNS = {
    "macd": batch_full_macd,
    "ma": batch_full_ma,
    "kpattern": batch_full_kpattern,
    "volume": batch_full_volume,
    "priceposition": batch_full_priceposition,
    "dde": batch_full_dde,
}


def run_batch_full_phase(
    con,
    calc_date: str,
    full_groups: Dict[Tuple[str, str], List[str]],
    batch_ctx: dict,
) -> dict:
    """Batch FULL for mass single-indicator FULL before chunk worker.

    Per-stock independent computation; shared narrow-window tail loads only.
    """
    from backend.config import CALC_BATCH_FULL
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS
    from backend.etl.orchestrator import resolve_recalc_start

    if not CALC_BATCH_FULL or not full_groups:
        return {
            "batch_full_items": 0,
            "full_by_indicator": {},
            "completed_keys": set(),
            "agg_by_key": {},
        }

    daily_recalc = resolve_recalc_start(con, calc_date, "daily")
    weekly_recalc = resolve_recalc_start(con, calc_date, "weekly")
    stock_modes = batch_ctx.get("stock_modes", {})
    daily_tails = batch_ctx.get("daily_tails", {})
    weekly_tails = batch_ctx.get("weekly_tails", {})
    dde_daily = batch_ctx.get("dde_daily", {})
    dde_weekly = batch_ctx.get("dde_weekly", {})
    state_map = batch_ctx.get("state_map", {})

    completed_keys: Set[Tuple[str, str, str]] = set()
    full_by_indicator: Dict[str, int] = {}
    batch_full_items = 0
    agg_by_key = defaultdict(CalcResult)

    spec_by_key = {
        (indicator_name, freq): (CalcCls, source)
        for indicator_name, freq, CalcCls, _, source in CALC_ROUTE_SPECS
    }

    for (indicator_name, freq), ts_codes in full_groups.items():
        if not ts_codes:
            continue

        meta = spec_by_key.get((indicator_name, freq))
        if meta is None:
            continue
        CalcCls, source = meta
        recalc_start = daily_recalc if freq == "daily" else weekly_recalc
        label_zh = _batch_label_zh(indicator_name, freq)

        if source == "quote":
            tails = daily_tails if freq == "daily" else weekly_tails
            data_groups = {c: tails[c] for c in ts_codes if c in tails}
        else:
            tails = dde_daily if freq == "daily" else dde_weekly
            data_groups = {c: tails[c] for c in ts_codes if c in tails}

        batch_fn = BATCH_FULL_FNS[indicator_name]
        logger.info(
            "progress calc.batch_full: 开始 %s | FULL=%d",
            label_zh, len(ts_codes),
        )
        t_group = time.monotonic()

        result, stock_rows = batch_fn(
            con, freq, ts_codes, calc_date, recalc_start,
            data_groups, state_map,
        )

        key = (indicator_name, freq)
        agg_by_key[key].calculated += result.calculated
        for reason, items in result.skipped.items():
            for code, detail in items:
                agg_by_key[key].add_skip(reason, code, detail)

        calc = CalcCls(con, freq)
        from backend.etl.calc_state import build_append_state_records, upsert_calc_state_batch
        spec_ver = getattr(calc, "SPEC_VERSION", "v1")
        full_state_records = build_append_state_records(
            stock_rows, freq, indicator_name, calc_date, spec_version=spec_ver,
        )
        if full_state_records:
            from backend.etl.progress import log_timed_step
            log_timed_step(
                "calc.batch_state", f"{label_zh}FULL状态",
                lambda: upsert_calc_state_batch(con, full_state_records),
                extra=f"records={len(full_state_records)}",
            )
        for ts_code in ts_codes:
            completed_keys.add((ts_code, indicator_name, freq))

        indicator_key = f"{indicator_name}_{freq}"
        full_by_indicator[indicator_key] = len(ts_codes)
        batch_full_items += len(ts_codes)
        logger.info(
            "progress calc.batch_full: 完成 %s | %.0fs | FULL=%d 写入=%d",
            label_zh, time.monotonic() - t_group, len(ts_codes), result.calculated,
        )

    return {
        "batch_full_items": batch_full_items,
        "full_by_indicator": full_by_indicator,
        "completed_keys": completed_keys,
        "agg_by_key": dict(agg_by_key),
    }


def _modes_from_state_only(
    ts_code: str,
    state_map: dict,
    calc_date: str,
) -> Optional[Dict[Tuple[str, str], Tuple[str, list]]]:
    """Build all-SKIP modes from dws_calc_state when state was refreshed on calc_date."""
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS

    modes: Dict[Tuple[str, str], Tuple[str, list]] = {}
    for indicator_name, freq, _, _sig_cols, source in CALC_ROUTE_SPECS:
        st = state_map.get((ts_code, freq, indicator_name))
        if st is None:
            return None
        if st.get("updated_calc_date") != calc_date:
            return None
        modes[(indicator_name, freq)] = ("SKIP", [])
    return modes


def try_force_same_day_batch_shortcut(
    con,
    codes: List[str],
    calc_date: str,
    force: bool,
) -> Optional[dict]:
    """Skip batch tail SQL when --force reruns unchanged same-day calc."""
    from backend.config import CALC_FORCE_BATCH_REUSE, CALC_FORCE_HARD
    from backend.etl.calc_gate import data_mutated_since_last_calc, get_last_calc_log
    from backend.etl.calc_state import load_calc_state_batch

    if not (force and CALC_FORCE_BATCH_REUSE and not CALC_FORCE_HARD):
        return None
    if not get_last_calc_log(con, calc_date):
        return None
    if data_mutated_since_last_calc(con, calc_date):
        return None

    logger.info(
        "calc force same-day: skipping batch tail loads (%d stocks, state-only routing)",
        len(codes),
    )
    t0 = time.monotonic()
    state_map = load_calc_state_batch(con, codes)

    stock_modes: Dict[str, Dict[Tuple[str, str], Tuple[str, list]]] = {}
    chunk_codes: Set[str] = set()
    completed_keys: Set[Tuple[str, str, str]] = set()
    agg_by_key = defaultdict(CalcResult)

    for ts_code in codes:
        modes = _modes_from_state_only(ts_code, state_map, calc_date)
        if modes is None:
            chunk_codes.add(ts_code)
            continue
        stock_modes[ts_code] = modes
        for (indicator_name, freq), _ in modes.items():
            completed_keys.add((ts_code, indicator_name, freq))
            agg_by_key[(indicator_name, freq)].add_skip(
                SkipReason.FINGERPRINT_MATCH, ts_code,
                "batch_append: force same-day state skip",
            )

    from backend.etl.calc_executor import build_work_queue

    wq = build_work_queue(stock_modes, completed_keys)
    chunk_codes |= {ts for ts, _ in wq.full_items}
    chunk_codes = sorted(chunk_codes)

    logger.info(
        "progress calc.batch_append: done | %.0fs | chunk=%d batch_only=%d | force_shortcut",
        time.monotonic() - t0,
        len(chunk_codes),
        len(codes) - len(chunk_codes),
    )
    return {
        "chunk_codes": chunk_codes,
        "completed_keys": completed_keys,
        "agg_by_key": dict(agg_by_key),
        "stock_modes": stock_modes,
        "state_map": state_map,
        "daily_tails": {},
        "weekly_tails": {},
        "dde_daily": {},
        "dde_weekly": {},
        "full_items": wq.full_items,
        "chunk_work_items": len(wq.full_items),
        "batch_full_items": 0,
        "full_by_indicator": {},
    }


def _merge_cold_tails_and_preflight(
    con,
    codes: List[str],
    state_map: dict,
    daily_tails: dict,
    weekly_tails: dict,
    dde_daily: dict,
    dde_weekly: dict,
    stock_modes: dict,
    fp_cache_by_stock: dict,
    chunk_codes: Set[str],
) -> None:
    """Cold-load tails + preflight for codes missing from refresh context."""
    from backend.etl.calc_indicators import quote_tail_columns
    from backend.etl.calc_fast_skip import (
        batch_load_dde_tails,
        batch_load_quote_tails,
        preflight_stock_modes_with_fps,
    )

    missing = [c for c in codes if c not in stock_modes]
    if not missing:
        return

    daily_tails.update(batch_load_quote_tails(
        con, missing, "daily", quote_tail_columns("daily"),
    ))
    weekly_tails.update(batch_load_quote_tails(
        con, missing, "weekly", quote_tail_columns("weekly"),
    ))
    dde_daily.update(batch_load_dde_tails(con, missing, "daily"))
    dde_weekly.update(batch_load_dde_tails(con, missing, "weekly"))

    for ts_code in missing:
        modes, fps = preflight_stock_modes_with_fps(
            ts_code, state_map,
            daily_tails.get(ts_code), weekly_tails.get(ts_code),
            dde_daily.get(ts_code), dde_weekly.get(ts_code),
        )
        if modes is None:
            chunk_codes.add(ts_code)
            continue
        stock_modes[ts_code] = modes
        fp_cache_by_stock[ts_code] = fps


def run_batch_append_phase(
    con, codes: List[str], calc_date: str, force: bool = False,
    preflight_ctx=None,
) -> Optional[dict]:
    """Batch APPEND + SKIP for all codes before per-stock chunk workers.

    Returns context dict with chunk_codes, completed_keys, agg_by_key, and
    preloaded tail frames; None when batch path is inactive.
    """
    from backend.config import CALC_APPEND, CALC_BATCH_APPEND
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS, quote_tail_columns
    from backend.etl.calc_fast_skip import (
        batch_load_dde_tails,
        batch_load_quote_tails,
        build_skip_state_records,
        partition_preflight_modes,
        preflight_stock_modes_with_fps,
    )
    from backend.etl.calc_state import (
        load_calc_state_batch,
        upsert_calc_state_batch,
    )

    if not (CALC_APPEND and CALC_BATCH_APPEND):
        return None

    shortcut = try_force_same_day_batch_shortcut(con, codes, calc_date, force)
    if shortcut is not None:
        return shortcut

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
            "full_items": [],
            "chunk_work_items": 0,
            "batch_full_items": 0,
            "full_by_indicator": {},
        }

    logger.info("progress calc.batch_append: 开始 | %d股", len(codes))
    t0 = time.monotonic()
    t_preflight = time.monotonic()

    from backend.config import CALC_REUSE_REFRESH_CTX
    from backend.etl.calc_preflight_context import slice_context_for_codes
    from backend.etl.progress import log_timed_step

    n = len(codes)
    preflight_source = "cold"
    tails_load_skipped = False
    chunk_codes: Set[str] = set()
    completed_keys: Set[Tuple[str, str, str]] = set()
    agg_by_key = defaultdict(CalcResult)
    stock_modes: Dict[str, Dict[Tuple[str, str], Tuple[str, list]]] = {}
    fp_cache_by_stock: Dict[str, Dict[Tuple[str, str], str]] = {}

    use_hot = (
        CALC_REUSE_REFRESH_CTX
        and preflight_ctx is not None
        and preflight_ctx.source == "refresh_state"
        and preflight_ctx.calc_date == calc_date
    )

    if use_hot:
        sliced = slice_context_for_codes(preflight_ctx, codes)
        state_map = log_timed_step(
            "calc.batch_tails", "state",
            lambda: load_calc_state_batch(con, codes), stocks=n,
            step_zh=BATCH_TAIL_ZH["state"],
        )
        daily_tails = dict(sliced.daily_tails)
        weekly_tails = dict(sliced.weekly_tails)
        dde_daily = dict(sliced.dde_daily)
        dde_weekly = dict(sliced.dde_weekly)
        stock_modes = dict(sliced.stock_modes)
        fp_cache_by_stock = dict(sliced.fp_cache_by_stock)
        chunk_codes = {c for c in codes if c not in stock_modes}
        preflight_source = "refresh"
        tails_load_skipped = True
        logger.info(
            "progress calc.batch_append: 热路径 | preflight_source=refresh | %d股",
            len(codes),
        )
        _merge_cold_tails_and_preflight(
            con, codes, state_map,
            daily_tails, weekly_tails, dde_daily, dde_weekly,
            stock_modes, fp_cache_by_stock, chunk_codes,
        )
        if chunk_codes:
            tails_load_skipped = False
    else:
        logger.info(
            "progress calc.batch_append: 冷路径 | preflight_source=cold | %d股",
            len(codes),
        )
        state_map = log_timed_step(
            "calc.batch_tails", "state",
            lambda: load_calc_state_batch(con, codes), stocks=n,
            step_zh=BATCH_TAIL_ZH["state"],
        )
        daily_tails = log_timed_step(
            "calc.batch_tails", "quote_daily",
            lambda: batch_load_quote_tails(
                con, codes, "daily", quote_tail_columns("daily"),
            ),
            stocks=n,
            step_zh=BATCH_TAIL_ZH["quote_daily"],
        )
        weekly_tails = log_timed_step(
            "calc.batch_tails", "quote_weekly",
            lambda: batch_load_quote_tails(
                con, codes, "weekly", quote_tail_columns("weekly"),
            ),
            stocks=n,
            step_zh=BATCH_TAIL_ZH["quote_weekly"],
        )
        dde_daily = log_timed_step(
            "calc.batch_tails", "dde_daily",
            lambda: batch_load_dde_tails(con, codes, "daily"),
            stocks=n,
            step_zh=BATCH_TAIL_ZH["dde_daily"],
        )
        dde_weekly = log_timed_step(
            "calc.batch_tails", "dde_weekly",
            lambda: batch_load_dde_tails(con, codes, "weekly"),
            stocks=n,
            step_zh=BATCH_TAIL_ZH["dde_weekly"],
        )

        from backend.etl.progress import stock_progress
        preflight = stock_progress("calc.batch_preflight", len(codes), detail="批算路由预检")
        preflight.log_start()
        for ts_code in codes:
            modes, fps = preflight_stock_modes_with_fps(
                ts_code, state_map,
                daily_tails.get(ts_code), weekly_tails.get(ts_code),
                dde_daily.get(ts_code), dde_weekly.get(ts_code),
            )
            if modes is None:
                chunk_codes.add(ts_code)
                preflight.tick()
                continue
            stock_modes[ts_code] = modes
            fp_cache_by_stock[ts_code] = fps
            preflight.tick()
        preflight.log_done()

    preflight_elapsed_sec = round(time.monotonic() - t_preflight, 2)

    for ts_code, modes in stock_modes.items():
        for (indicator_name, freq), (mode, _) in modes.items():
            if mode != "SKIP":
                continue
            completed_keys.add((ts_code, indicator_name, freq))
            agg_by_key[(indicator_name, freq)].add_skip(
                SkipReason.FINGERPRINT_MATCH, ts_code, "batch_append: preflight skip",
            )

    state_records = build_skip_state_records(
        stock_modes, fp_cache_by_stock, state_map, calc_date,
        daily_tails, weekly_tails, dde_daily, dde_weekly,
    )
    log_timed_step(
        "calc.batch_state", "skip_refresh",
        lambda: upsert_calc_state_batch(con, state_records),
        extra=f"records={len(state_records)}",
        step_zh=BATCH_TAIL_ZH["skip_refresh"],
    )

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
        key_label = f"{indicator_name}_{freq}"
        label_zh = _batch_label_zh(indicator_name, freq)

        result, stock_rows = batch_fn(
            con, freq, append_codes, calc_date, data_groups, new_bars_map, state_map,
        )
        agg_by_key[(indicator_name, freq)].calculated += result.calculated
        logger.info(
            "progress calc.batch_append: %s | APPEND=%d 写入=%d",
            label_zh, len(append_codes), result.calculated,
        )

        calc = CalcCls(con, freq)
        from backend.etl.calc_state import build_append_state_records
        spec_ver = getattr(calc, "SPEC_VERSION", "v1")
        append_state_records = build_append_state_records(
            stock_rows, freq, indicator_name, calc_date, spec_version=spec_ver,
        )
        if append_state_records:
            log_timed_step(
                "calc.batch_state", f"{label_zh}状态",
                lambda: upsert_calc_state_batch(con, append_state_records),
                extra=f"records={len(append_state_records)}",
            )
        for ts_code in append_codes:
            completed_keys.add((ts_code, indicator_name, freq))

    from backend.etl.calc_executor import build_work_queue, group_by_indicator

    wq = build_work_queue(stock_modes, completed_keys)
    full_groups = group_by_indicator(wq.full_items)

    batch_full_items = 0
    full_by_indicator: Dict[str, int] = {}
    if full_groups:
        full_ctx = {
            "stock_modes": stock_modes,
            "state_map": state_map,
            "daily_tails": daily_tails,
            "weekly_tails": weekly_tails,
            "dde_daily": dde_daily,
            "dde_weekly": dde_weekly,
        }
        full_result = run_batch_full_phase(con, calc_date, full_groups, full_ctx)
        completed_keys |= full_result["completed_keys"]
        batch_full_items = full_result["batch_full_items"]
        full_by_indicator = full_result["full_by_indicator"]
        for key, agg in full_result.get("agg_by_key", {}).items():
            agg_by_key[key].calculated += agg.calculated
            for reason, items in agg.skipped.items():
                for code, detail in items:
                    agg_by_key[key].add_skip(reason, code, detail)
        wq = build_work_queue(stock_modes, completed_keys)

    chunk_codes |= {ts for ts, _ in wq.full_items}
    chunk_codes = sorted(chunk_codes)

    logger.info(
        "progress calc.batch_append: 完成 | %.0fs | chunk=%d batch_only=%d | batch_full=%d",
        time.monotonic() - t0,
        len(chunk_codes),
        len(codes) - len(chunk_codes),
        batch_full_items,
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
        "full_items": wq.full_items,
        "chunk_work_items": len(wq.full_items),
        "batch_full_items": batch_full_items,
        "full_by_indicator": full_by_indicator,
        "preflight_source": preflight_source,
        "tails_load_skipped": tails_load_skipped,
        "preflight_elapsed_sec": preflight_elapsed_sec,
        "state_upsert_mode": "batch",
    }
