#!/usr/bin/env python3
"""M2c spike: profile volume_trend_v2 cost in batch APPEND path.

Usage:
    python3 scripts/profile_volume_trend_v2.py
    python3 scripts/profile_volume_trend_v2.py --stocks 5389 --bars 245 --repeat 3
    python3 scripts/profile_volume_trend_v2.py --cprofile
"""
from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import sys
import time
from typing import Callable, Dict, List, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.etl.calc_volume import (  # noqa: E402
    VOLUME_TREND_V2_DAILY,
    VolumeCalculator,
    compute_volume_trend_series,
    trend_from_v2_label,
    volume_trend_v2,
)


def _synthetic_vol_series(n_bars: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = 1000.0 + np.cumsum(rng.normal(0, 50, n_bars))
    return np.maximum(base, 100.0)


def _synthetic_quote_groups(codes: List[str], n_bars: int) -> dict:
    import pandas as pd

    dates = [
        (pd.Timestamp("2020-01-01") + pd.Timedelta(days=i)).strftime("%Y%m%d")
        for i in range(n_bars)
    ]
    groups = {}
    for j, code in enumerate(codes):
        vol = _synthetic_vol_series(n_bars, seed=1000 + j)
        close = 10.0 + np.cumsum(np.random.default_rng(2000 + j).normal(0, 0.1, n_bars))
        groups[code] = pd.DataFrame({
            "trade_date": dates,
            "close_qfq": close,
            "vol": vol,
        })
    return groups


def compute_volume_trend_series_last_only(vol_series, params: dict) -> list:
    """Benchmark: APPEND path via production ``target_indices=[n-1]``."""
    vol = np.asarray(vol_series, dtype=float)
    n = len(vol)
    if n == 0:
        return []
    return compute_volume_trend_series(vol, params, target_indices=[n - 1])


from backend.etl.vector.volume_batch import batch_volume_rolling_core  # noqa: E402


def _time_call(fn: Callable, repeat: int) -> Tuple[float, object]:
    t0 = time.perf_counter()
    last = None
    for _ in range(repeat):
        last = fn()
    elapsed = time.perf_counter() - t0
    return elapsed, last


def profile_components(
    n_stocks: int,
    n_bars: int,
    repeat: int,
) -> Dict[str, float]:
    codes = [f"P{j:05d}.SZ" for j in range(n_stocks)]
    groups = _synthetic_quote_groups(codes, n_bars)
    calc = VolumeCalculator(None, "daily")
    params = VOLUME_TREND_V2_DAILY

    def run_trend_full():
        for code in codes:
            compute_volume_trend_series(groups[code]["vol"].values, params)

    def run_trend_last_only():
        for code in codes:
            compute_volume_trend_series_last_only(groups[code]["vol"].values, params)

    def run_vector_core():
        batch_volume_rolling_core(codes, groups)

    def run_volume_derived_excl_trend():
        cores = batch_volume_rolling_core(codes, groups)
        from backend.etl.vector.volume_batch import attach_volume_core_to_df
        for code in codes:
            base = attach_volume_core_to_df(groups[code], cores[code])
            calc._compute_zone(base)
            calc._compute_trend_strength(base["vol"].values, window=10)
            calc._compute_divergence(base)

    timings = {}
    timings["trend_v2_full_series"], _ = _time_call(run_trend_full, repeat)
    timings["trend_v2_last_bar_only"], _ = _time_call(run_trend_last_only, repeat)
    timings["vector_rolling_core"], _ = _time_call(run_vector_core, repeat)
    timings["derived_excl_trend"], _ = _time_call(run_volume_derived_excl_trend, repeat)

    # Per-stock single-path micro (1 repeat, averaged)
    sample = groups[codes[0]]["vol"].values
    per = {}
    _, _ = _time_call(lambda: compute_volume_trend_series(sample, params), 1)
    t_series, _ = _time_call(lambda: compute_volume_trend_series(sample, params), 50)
    per["trend_series_per_stock_ms"] = t_series / 50 * 1000

    n_calls = max(0, n_bars - params["anchor_bars"] + 1)
    _, _ = _time_call(lambda: volume_trend_v2(sample, **params), 200)
    t_single, _ = _time_call(lambda: volume_trend_v2(sample, **params), 200)
    per["volume_trend_v2_full_window_ms"] = t_single / 200 * 1000
    per["expanding_calls_per_stock"] = n_calls
    per["complexity_order_per_stock"] = n_calls * (n_bars + params["anchor_bars"]) // 2

    timings["_per_stock"] = per
    return timings


def run_cprofile(n_stocks: int, n_bars: int) -> str:
    codes = [f"C{j:04d}.SZ" for j in range(min(n_stocks, 200))]
    groups = _synthetic_quote_groups(codes, n_bars)
    params = VOLUME_TREND_V2_DAILY

    def target():
        for code in codes:
            compute_volume_trend_series(groups[code]["vol"].values, params)

    pr = cProfile.Profile()
    pr.enable()
    target()
    pr.disable()
    buf = io.StringIO()
    ps = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
    ps.print_stats(25)
    return buf.getvalue()


def _fmt_sec(sec: float) -> str:
    if sec >= 60:
        return f"{sec:.1f}s ({sec/60:.1f}min)"
    return f"{sec:.2f}s"


def verify_last_bar_equivalence(n_seeds: int = 50, n_bars: int = 245) -> Tuple[int, int]:
    """Confirm APPEND last-bar-only matches expanding series at tail index."""
    params = VOLUME_TREND_V2_DAILY
    ok = 0
    for seed in range(n_seeds):
        vol = _synthetic_vol_series(n_bars, seed)
        full = compute_volume_trend_series(vol, params)
        last = compute_volume_trend_series_last_only(vol, params)
        if full[-1] == last[-1]:
            ok += 1
    return ok, n_seeds


def main():
    parser = argparse.ArgumentParser(description="Profile volume_trend_v2 (M2c)")
    parser.add_argument("--stocks", type=int, default=5389)
    parser.add_argument("--bars", type=int, default=245)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--cprofile", action="store_true")
    parser.add_argument("--baseline-sec", type=float, default=944.0,
                        help="0fd66428 volume batch_compute seconds for extrapolation")
    parser.add_argument("--equiv-seeds", type=int, default=50,
                        help="random seeds for last-bar equivalence check (0=skip)")
    parser.add_argument("--market-stocks", type=int, default=5389,
                        help="active stock count for linear extrapolation")
    args = parser.parse_args()

    print("=== M2c volume_trend_v2 profiling ===")
    print(f"stocks={args.stocks} bars={args.bars} repeat={args.repeat}")
    print()

    if args.equiv_seeds > 0:
        ok, total = verify_last_bar_equivalence(args.equiv_seeds, args.bars)
        print(f"--- Equivalence (last-bar vs expanding tail) ---")
        print(f"  passed                    : {ok}/{total}")
        print()

    if args.cprofile:
        print("--- cProfile (subset stocks=200) ---")
        print(run_cprofile(args.stocks, args.bars))
        print()

    t0 = time.perf_counter()
    timings = profile_components(args.stocks, args.bars, args.repeat)
    wall = time.perf_counter() - t0

    trend_full = timings["trend_v2_full_series"]
    trend_last = timings["trend_v2_last_bar_only"]
    vector_core = timings["vector_rolling_core"]
    derived_rest = timings["derived_excl_trend"]
    per = timings["_per_stock"]

    total_derived_est = trend_full + derived_rest
    total_append_est = trend_last + derived_rest + vector_core

    print("--- Component wall (this run) ---")
    print(f"  trend_v2 expanding series : {_fmt_sec(trend_full)}")
    print(f"  trend_v2 last-bar only    : {_fmt_sec(trend_last)}")
    print(f"  vector rolling core       : {_fmt_sec(vector_core)}")
    print(f"  zone+strength+divergence  : {_fmt_sec(derived_rest)}")
    print(f"  profile wall              : {_fmt_sec(wall)}")
    print()

    print("--- Per-stock micro ---")
    print(f"  expanding calls/stock     : {per['expanding_calls_per_stock']}")
    print(f"  trend_series/stock        : {per['trend_series_per_stock_ms']:.2f} ms")
    print(f"  volume_trend_v2(full win) : {per['volume_trend_v2_full_window_ms']:.3f} ms")
    print()

    if trend_full > 0:
        speedup = trend_full / max(trend_last, 1e-9)
        print("--- APPEND last-bar-only spike ---")
        print(f"  speedup vs expanding      : {speedup:.1f}x")
        print(f"  est. volume batch_compute : {_fmt_sec(args.baseline_sec / speedup)} "
              f"(from baseline {args.baseline_sec:.0f}s trend-dominated)")
        print()

    pct_trend = 100.0 * trend_full / max(total_derived_est, 1e-9)
    print("--- Share in volume derived path ---")
    print(f"  trend_v2 (expanding)      : {pct_trend:.1f}%")
    print(f"  other derived             : {100 - pct_trend:.1f}%")
    print()

    scale = args.market_stocks / max(args.stocks, 1)
    print(f"--- Extrapolation ({args.market_stocks} stocks, linear ×{scale:.2f}) ---")
    print(f"  trend_v2 expanding        : {_fmt_sec(trend_full * scale)}")
    print(f"  trend_v2 last-bar only    : {_fmt_sec(trend_last * scale)}")
    print(f"  vector rolling core       : {_fmt_sec(vector_core * scale)}")
    print(f"  zone+strength+divergence  : {_fmt_sec(derived_rest * scale)}")
    est_volume_batch = args.baseline_sec * (trend_last + derived_rest) / max(trend_full + derived_rest, 1e-9)
    print(f"  est. volume batch_compute : {_fmt_sec(est_volume_batch)} "
          f"(baseline {args.baseline_sec:.0f}s, trend share only)")
    print()

    print("--- Verdict ---")
    if pct_trend >= 70:
        print("  BOTTLENECK: compute_volume_trend_series O(n^2) expanding prefixes.")
        print("  Recommend: APPEND path compute trend at new_bars indices only (1 call/stock).")
    else:
        print("  trend_v2 significant but not sole bottleneck; profile other derived too.")
    print()
    print("M2c+ landed: APPEND uses target_indices=new_bars; run benchmark_run for prod sign-off.")


if __name__ == "__main__":
    main()
