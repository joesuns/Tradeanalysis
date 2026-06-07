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
from backend.etl.calc_macd import MACDCalculator
from backend.etl.calc_dde import DDECalculator
from backend.etl.calc_volume import VolumeCalculator


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


def test_kpattern_signature_changes_when_vol_changes():
    """KPattern uses vol (volume thresholds + strength), so the history signature
    MUST flip when only vol changes (OHLC unchanged) — else a vol correction
    would be silently misrouted to APPEND with stale pattern logic."""
    from backend.etl.base import compute_history_signature
    df = _make_ohlcv_df(120)
    sig_cols = ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol"]
    df2 = df.copy()
    df2.loc[10, "vol"] = df2.loc[10, "vol"] * 2.0  # vol-only change
    assert compute_history_signature(df, sig_cols) != \
        compute_history_signature(df2, sig_cols)


# ---------------------------------------------------------------------------
# Shared helper for DDE tests
# ---------------------------------------------------------------------------

def _make_moneyflow_df(n: int = 200) -> pd.DataFrame:
    """Moneyflow + close df with all required DDE input columns."""
    rng = np.random.default_rng(55)
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    total_vol = rng.uniform(10000, 50000, n)
    frac = rng.uniform(0.3, 0.7, n)
    buy_lg = total_vol * frac * rng.uniform(0.2, 0.4, n)
    sell_lg = total_vol * (1 - frac) * rng.uniform(0.2, 0.4, n)
    buy_elg = total_vol * frac * rng.uniform(0.05, 0.15, n)
    sell_elg = total_vol * (1 - frac) * rng.uniform(0.05, 0.15, n)
    net_mf = buy_lg + buy_elg - sell_lg - sell_elg
    return pd.DataFrame({
        "trade_date": [f"26{i:06d}" for i in range(n)],
        "buy_lg_vol":  buy_lg,
        "sell_lg_vol": sell_lg,
        "buy_elg_vol": buy_elg,
        "sell_elg_vol":sell_elg,
        "total_vol":   total_vol,
        "net_mf_amount": net_mf,
        "close_qfq":   close,
    })


# ---------------------------------------------------------------------------
# Task 9 — MACDCalculator
# ---------------------------------------------------------------------------

def test_macd_append_ema_matches_full():
    """Seeded EMA one-step recursion: last bar ema_12/ema_26/dif/dea/macd_bar == FULL.

    Seeds taken from FULL's second-to-last bar (simulating prior DWS state).
    The new bar is processed alone with those seeds; result must equal FULL
    on the same bar to atol=1e-9 by the EMA recursion property.
    """
    df = _make_df(80)
    calc = MACDCalculator(None, "daily")

    # FULL: SMA warm-up, no external seeds
    full_df = calc._compute_indicators(df.copy())

    # Seeds from second-to-last bar (what would be stored in DWS)
    s_row = full_df.iloc[-2]
    seeds = {
        "ema_12": float(s_row["ema_12"]),
        "ema_26": float(s_row["ema_26"]),
        "dea":    float(s_row["dea"]),
    }

    # APPEND: only the new bar, seeded from bar N-2
    new_bar_df = df.iloc[-1:].reset_index(drop=True)
    app_df = calc._compute_indicators(new_bar_df.copy(), ema_seeds=seeds)

    for col in ["ema_12", "ema_26", "dif", "dea", "macd_bar"]:
        a = app_df.iloc[-1][col]
        b = full_df.iloc[-1][col]
        if pd.isna(b):
            assert pd.isna(a), f"MACD {col}: expected NaN, got {a}"
        else:
            assert abs(a - b) < 1e-9, f"MACD {col}: |{a} - {b}| >= 1e-9"


def test_macd_append_divergence_matches_full():
    """Tail-window (80 bars) with seeded EMA: last bar divergence == FULL(200 bars).

    tail_window >= 60+5 guarantees dedup context is fully within the window,
    making divergence at the last bar identical to the full-history computation.
    """
    df_full = _make_df(200)
    calc = MACDCalculator(None, "daily")

    # FULL on all 200 bars
    full_df = calc._compute_indicators(df_full.copy())

    # APPEND: last 80 bars as tail window, seeds from bar just before tail[0]
    tail = df_full.iloc[-80:].reset_index(drop=True)
    s_row = full_df.iloc[-81]   # index 119, immediately before tail[0]=index 120
    seeds = {
        "ema_12": float(s_row["ema_12"]),
        "ema_26": float(s_row["ema_26"]),
        "dea":    float(s_row["dea"]),
    }
    app_df = calc._compute_indicators(tail.copy(), ema_seeds=seeds)

    a_div = app_df.iloc[-1]["divergence"]
    b_div = full_df.iloc[-1]["divergence"]
    assert a_div == b_div, (
        f"MACD divergence: append={a_div!r} != full={b_div!r}"
    )

    # All EMA-derived output columns must also match on the new bar.
    for col in ["macd_bar", "dif", "dea", "trend_strength"]:
        a = app_df.iloc[-1][col]
        b = full_df.iloc[-1][col]
        if pd.isna(b):
            assert pd.isna(a), f"MACD {col}: expected NaN, got {a}"
        else:
            assert abs(a - b) < 1e-9, f"MACD {col}: |{a} - {b}| >= 1e-9"
    for col in ["zone", "trend", "turning_point", "alert"]:
        assert app_df.iloc[-1][col] == full_df.iloc[-1][col], (
            f"MACD {col}: append={app_df.iloc[-1][col]!r} != full={full_df.iloc[-1][col]!r}"
        )


# ---------------------------------------------------------------------------
# Task 10 — DDECalculator
# ---------------------------------------------------------------------------

def test_dde_append_matches_full():
    """DDE tail-window + seeded DDX2 gives same ddx/ddx2/divergence as FULL.

    ddx is causal (no EMA), so it matches by construction.
    ddx2 = EMA(ddx, 5) with seed from bar before tail[0] gives exact match.
    Divergence uses ddx (causal) in a 60-bar window; tail >= 65 bars ensures
    dedup context is fully contained, so last bar divergence matches FULL.
    """
    df_full = _make_moneyflow_df(200)
    calc = DDECalculator(None, "daily")

    # FULL on all 200 bars
    full_df = calc._compute_indicators(df_full.copy())

    # APPEND: last 80 bars + seed for ddx2 from bar just before tail[0]
    tail = df_full.iloc[-80:].reset_index(drop=True)
    s_row = full_df.iloc[-81]   # index 119, immediately before tail[0]=index 120
    seeds = {"ddx2": float(s_row["ddx2"])}
    app_df = calc._compute_indicators(tail.copy(), ema_seeds=seeds)

    # DDX (causal, no EMA) and DDX2 (EMA with seed) must match exactly
    for col in ["ddx", "ddx2"]:
        a = app_df.iloc[-1][col]
        b = full_df.iloc[-1][col]
        if pd.isna(b):
            assert pd.isna(a), f"DDE {col}: expected NaN, got {a}"
        else:
            assert abs(a - b) < 1e-9, f"DDE {col}: |{a} - {b}| >= 1e-9"

    # Divergence: ddx is causal and tail >= 65 bars → last bar must match FULL
    a_div = app_df.iloc[-1]["divergence"]
    b_div = full_df.iloc[-1]["divergence"]
    assert a_div == b_div, (
        f"DDE divergence: append={a_div!r} != full={b_div!r}"
    )


# ---------------------------------------------------------------------------
# Task 11 — VolumeCalculator
# ---------------------------------------------------------------------------

def _make_vol_df(n: int = 300) -> pd.DataFrame:
    """Volume df: trade_date + vol + close_qfq."""
    rng = np.random.default_rng(42)
    vol = rng.uniform(100, 1000, n)
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    return pd.DataFrame({
        "trade_date": [f"26{i:06d}" for i in range(n)],
        "vol": vol,
        "close_qfq": close,
    })


def test_volume_append_matches_full_on_new_bar():
    """_compute_indicators_append with zone_seed from FULL: last bar matches FULL.

    Tail window (140 bars) covers the 120-bar pct_vol_rank lookback plus a
    safety margin (20 bars).  Zone seed is taken from FULL's zone at the
    last bar immediately before the tail, correctly initialising hysteresis
    state.  All rolling indicators are causal, so last-bar values match
    FULL to atol=1e-9.
    """
    df = _make_vol_df(300)
    calc = VolumeCalculator(None, "daily")

    # FULL on all 300 bars
    full_df = calc._compute_indicators(df.copy())
    last_date = df.iloc[-1]["trade_date"]

    # Tail: last 140 bars; zone seed = FULL's zone at the bar before tail[0]
    tail = df.iloc[-140:].reset_index(drop=True)
    zone_seed = full_df.iloc[-141]["zone"]   # global index 159, before tail start
    append_df = calc._compute_indicators_append(tail.copy(), zone_seed=zone_seed)

    app_row = append_df[append_df["trade_date"] == last_date].iloc[0]
    full_row = full_df[full_df["trade_date"] == last_date].iloc[0]

    float_cols = ["ma_vol_5", "pct_vol_rank", "volume_ratio", "trend_strength"]
    for col in float_cols:
        a, b = app_row[col], full_row[col]
        if pd.isna(b):
            assert pd.isna(a), f"{col}: expected NaN, got {a}"
        else:
            assert abs(a - b) < 1e-9, f"{col}: |{a} - {b}| >= 1e-9"

    for col in ("zone", "trend", "divergence"):
        assert app_row[col] == full_row[col], (
            f"{col}: append={app_row[col]!r} full={full_row[col]!r}"
        )


def test_volume_append_zone_hysteresis_at_new_bar():
    """Zone seed correctly re-establishes hysteresis; naive recompute would err.

    Scenario:
    - Full 130-bar df: bars 0-1 have pct_vol_rank=91 → enter 'explosive';
      bars 2-129 have rank=80 → stay explosive (>75) but can't re-enter (<90).
    - Tail = last 120 bars (bars 10-129): all rank=80, never ≥90 for 2
      consecutive → naive (no-seed) recompute never enters → zone='normal' (wrong).
    - With zone_seed='explosive', prior state continues → zone='explosive' (correct).

    This test drives the zone_seed implementation in _compute_zone.
    """
    calc = VolumeCalculator(None, "daily")
    n_full = 130

    # pct_vol_rank array: spike at bars 0-1 → enter explosive; rest = 80
    rank_arr = np.full(n_full, 80.0)
    rank_arr[0] = 91.0
    rank_arr[1] = 91.0

    df_full = pd.DataFrame({
        "trade_date": [f"26{i:06d}" for i in range(n_full)],
        "pct_vol_rank": rank_arr,
        "vol": np.ones(n_full) * 500.0,
    })

    # FULL: bars 1+ are explosive (entry at bar 1; rank=80>75 → never exits)
    zone_full = calc._compute_zone(df_full)
    assert zone_full[-1] == "explosive", (
        f"FULL: expected last bar 'explosive', got {zone_full[-1]!r}"
    )

    # Tail: last 120 bars (bars 10-129), all rank=80 — never re-enters explosive
    df_tail = df_full.iloc[-120:].reset_index(drop=True)

    # Naive recompute (no seed): in_explosive starts False → never enters → 'normal'
    zone_naive = calc._compute_zone(df_tail)
    assert zone_naive[-1] == "normal", (
        f"Naive (no seed): expected 'normal', got {zone_naive[-1]!r}"
    )

    # Seeded recompute: zone_seed='explosive' → carries prior state → stays explosive
    zone_seeded = calc._compute_zone(df_tail, zone_seed="explosive")
    assert zone_seeded[-1] == "explosive", (
        f"Seeded: expected 'explosive', got {zone_seeded[-1]!r}"
    )
