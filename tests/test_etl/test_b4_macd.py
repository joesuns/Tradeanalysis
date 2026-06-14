"""B4 MACD trend / crossover aligned with 123."""
import numpy as np
import pandas as pd

from backend.etl.b4_macd import (
    B4_NEAR_DAILY,
    compute_macd_crossover_123_series,
    compute_macd_trend_123_series,
)


def test_macd_trend_up_on_rising_histogram():
    macd = np.array([0.0, 0.0, 0.0, 0.0, 0.01, 0.03, 0.06, 0.10, 0.15])
    trends = compute_macd_trend_123_series(macd)
    assert trends[8] == "up"


def test_macd_trend_down_on_falling_histogram():
    macd = np.array([0.15, 0.10, 0.06, 0.03, 0.01, 0.0, 0.0, 0.0, 0.0])
    trends = compute_macd_trend_123_series(macd)
    assert trends[5] == "down"


def test_macd_golden_cross_on_dif_dea_cross():
    dif = np.array([0.0, 0.0, -0.1, -0.05, 0.02])
    dea = np.array([0.0, 0.0, 0.0, -0.02, 0.0])
    crosses = compute_macd_crossover_123_series(
        dif, dea, B4_NEAR_DAILY["n_std"], B4_NEAR_DAILY["frac"],
    )
    assert crosses[4] == "golden_cross"


def test_macd_dead_cross_on_dif_dea_cross():
    dif = np.array([0.0, 0.0, 0.1, 0.05, -0.02])
    dea = np.array([0.0, 0.0, 0.0, 0.02, 0.0])
    crosses = compute_macd_crossover_123_series(
        dif, dea, B4_NEAR_DAILY["n_std"], B4_NEAR_DAILY["frac"],
    )
    assert crosses[4] == "dead_cross"


def test_macd_near_golden_narrowing_gap():
    """123 near-cross: DIF<DEA, gap shrinks, gap < std threshold."""
    n = 25
    dif = np.full(n, -0.05)
    dea = np.full(n, -0.04)
    dif[-3] = -0.08
    dif[-2] = -0.06
    dif[-1] = -0.051
    dea[-3] = -0.05
    dea[-2] = -0.049
    dea[-1] = -0.0505  # gap: 0.03 → 0.011 → 0.0005 (all DIF < DEA)
    crosses = compute_macd_crossover_123_series(
        dif, dea, B4_NEAR_DAILY["n_std"], B4_NEAR_DAILY["frac"],
    )
    assert crosses[-1] == "near_golden"
