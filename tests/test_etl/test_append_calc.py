"""Tests for Calculator.append_calculate equivalence with FULL mode.

For causal rolling indicators (PP / MA / KPattern), a bar's value depends
only on its trailing window.  As long as the tail-window df supplied to
append_calculate covers the full lookback, append on new_bars must produce
the same numeric result as FULL.  These tests lock that contract.

Date strings use the f"26{i:06d}" format (e.g. "26000000" … "26000299"):
unique, sortable, and unambiguous without needing a real calendar.
PP / MA / KPattern are all date-string-agnostic (no calendar arithmetic).
"""
import numpy as np
import pandas as pd
import pytest

from backend.etl.calc_price_position import PricePositionCalculator
from backend.etl.calc_ma import MACalculator
from backend.etl.calc_kpattern import KPatternCalculator


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_df(n: int = 300) -> pd.DataFrame:
    """Minimal daily-quote df: trade_date (unique sortable) + close_qfq."""
    rng = np.random.default_rng(42)
    close = 10 + np.cumsum(rng.normal(0, 0.2, n))
    return pd.DataFrame({
        "trade_date": [f"26{i:06d}" for i in range(n)],
        "close_qfq": close,
    })


def _make_ohlcv_df(n: int = 300) -> pd.DataFrame:
    """Full OHLCV df with valid OHLC (high>=max(o,c), low<=min(o,c))."""
    rng = np.random.default_rng(99)
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    noise = np.abs(rng.normal(0, 0.05, n))
    open_ = close * (1 + rng.uniform(-0.01, 0.01, n))
    high = np.maximum(open_, close) + noise
    low = np.minimum(open_, close) - noise
    vol = rng.uniform(100, 1000, n)
    pct_chg = np.concatenate([[0.0], (close[1:] - close[:-1]) / close[:-1] * 100])
    return pd.DataFrame({
        "trade_date": [f"26{i:06d}" for i in range(n)],
        "open_qfq":   open_,
        "high_qfq":   high,
        "low_qfq":    low,
        "close_qfq":  close,
        "vol":        vol,
        "pct_chg":    pct_chg,
    })


# ---------------------------------------------------------------------------
# Task 6 — PricePositionCalculator
# ---------------------------------------------------------------------------

def test_pp_append_matches_full_on_new_bar():
    """append path produces identical price_position on the last bar as FULL."""
    df = _make_df(300)
    calc = PricePositionCalculator(None, "daily")

    full_df = calc._compute_positions(df.copy())
    last_date = df.iloc[-1]["trade_date"]

    append_df = calc._compute_positions_append(df.copy(), new_bars=[last_date])
    app_row = append_df[append_df["trade_date"] == last_date].iloc[0]
    full_row = full_df[full_df["trade_date"] == last_date].iloc[0]

    for w in (60, 120, 250):
        col = f"price_position_{w}d"
        a, b = app_row[col], full_row[col]
        if pd.isna(b):
            assert pd.isna(a), f"{col}: expected NaN, got {a}"
        else:
            assert abs(a - b) < 1e-9, f"{col}: |{a} - {b}| >= 1e-9"


# ---------------------------------------------------------------------------
# Task 7 — MACalculator
# ---------------------------------------------------------------------------

def test_ma_append_matches_full_on_new_bar():
    """append path produces identical MA indicators on the last bar as FULL."""
    df = _make_df(300)
    calc = MACalculator(None, "daily")

    full_df = calc._compute_indicators(df.copy())
    last_date = df.iloc[-1]["trade_date"]

    append_df = calc._compute_indicators_append(df.copy(), new_bars=[last_date])
    app_row = append_df[append_df["trade_date"] == last_date].iloc[0]
    full_row = full_df[full_df["trade_date"] == last_date].iloc[0]

    float_cols = ["ma_5", "ma_10", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope"]
    for col in float_cols:
        a, b = app_row[col], full_row[col]
        if pd.isna(b):
            assert pd.isna(a), f"{col}: expected NaN, got {a}"
        else:
            assert abs(a - b) < 1e-9, f"{col}: |{a} - {b}| >= 1e-9"

    for col in ("alignment", "turning_point"):
        assert app_row[col] == full_row[col], (
            f"{col}: append={app_row[col]!r} full={full_row[col]!r}"
        )


# ---------------------------------------------------------------------------
# Task 8 — KPatternCalculator
# ---------------------------------------------------------------------------

def test_kpattern_append_matches_full_on_new_bar():
    """append path produces identical pattern flags + strength on last bar."""
    df = _make_ohlcv_df(300)
    calc = KPatternCalculator(None, "daily")

    full_df = calc._compute_patterns(df.copy(), is_st=False)
    last_date = df.iloc[-1]["trade_date"]

    append_df = calc._compute_patterns_append(df.copy(), new_bars=[last_date], is_st=False)
    app_row = append_df[append_df["trade_date"] == last_date].iloc[0]
    full_row = full_df[full_df["trade_date"] == last_date].iloc[0]

    pattern_cols = [
        "yang_bao_yin", "yang_ke_yin", "mu_bei_xian", "bi_lei_zhen",
        "gao_kai_chang_yin", "yin_bao_yang", "yin_ke_yang",
    ]
    for col in pattern_cols:
        assert app_row[col] == full_row[col], (
            f"{col}: append={app_row[col]} full={full_row[col]}"
        )

    a_str, b_str = app_row["strength"], full_row["strength"]
    if pd.isna(b_str):
        assert pd.isna(a_str), f"strength: expected NaN, got {a_str}"
    else:
        assert abs(float(a_str) - float(b_str)) < 1e-9, (
            f"strength: |{a_str} - {b_str}| >= 1e-9"
        )
