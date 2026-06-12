#!/usr/bin/env python3
"""M2d: profile MACD weekly B4 b4_weekly_series_from_daily cost.

Usage:
    python3 scripts/profile_macd_b4_weekly.py
    python3 scripts/profile_macd_b4_weekly.py --stocks 500 --bars 245 --repeat 3
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, List, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.etl.b4_macd import (  # noqa: E402
    b4_weekly_series_from_daily,
    convert_daily_to_weekly_resample_w,
)


def _synthetic_daily(n_days: int = 600, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-01", periods=n_days, freq="B")
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n_days))
    return pd.DataFrame({
        "trade_date": [d.strftime("%Y%m%d") for d in dates],
        "close_qfq": close,
    })


def _week_ends(daily: pd.DataFrame, n_weeks: int) -> List[str]:
    w = convert_daily_to_weekly_resample_w(daily)
    return w["trade_date"].astype(str).tail(n_weeks).tolist()


def _time_call(fn: Callable, repeat: int) -> Tuple[float, object]:
    t0 = time.perf_counter()
    last = None
    for _ in range(repeat):
        last = fn()
    return time.perf_counter() - t0, last


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile MACD weekly B4 resample cost")
    parser.add_argument("--stocks", type=int, default=500)
    parser.add_argument("--bars", type=int, default=245)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--market", type=int, default=5389)
    args = parser.parse_args()

    expanding_total = 0.0
    target_total = 0.0

    for i in range(args.stocks):
        daily = _synthetic_daily(600, seed=42 + i)
        week_ends = _week_ends(daily, args.bars)
        last_idx = len(week_ends) - 1

        exp_elapsed, _ = _time_call(
            lambda d=daily, w=week_ends: b4_weekly_series_from_daily(d, w),
            args.repeat,
        )
        tgt_elapsed, _ = _time_call(
            lambda d=daily, w=week_ends, li=last_idx: b4_weekly_series_from_daily(
                d, w, target_indices={li},
            ),
            args.repeat,
        )
        expanding_total += exp_elapsed
        target_total += tgt_elapsed

    exp_per = expanding_total / args.stocks * 1000
    tgt_per = target_total / args.stocks * 1000
    speedup = expanding_total / target_total if target_total > 0 else float("inf")

    print(f"stocks={args.stocks} bars={args.bars} repeat={args.repeat}")
    print(f"expanding: {exp_per:.2f} ms/stock  total={expanding_total:.2f}s")
    print(f"target_last: {tgt_per:.2f} ms/stock  total={target_total:.2f}s")
    print(f"speedup: {speedup:.1f}x")
    print(
        f"extrapolate {args.market} stocks expanding: "
        f"{expanding_total / args.stocks * args.market:.1f}s"
    )
    print(
        f"extrapolate {args.market} stocks target_last: "
        f"{target_total / args.stocks * args.market:.1f}s"
    )


if __name__ == "__main__":
    main()
