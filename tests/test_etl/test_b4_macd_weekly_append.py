"""M2d: MACD weekly B4 target_indices equivalence."""
import numpy as np
import pandas as pd
import pytest

from backend.etl.b4_macd import b4_weekly_series_from_daily


def _synthetic_daily(n_days: int = 600, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-01", periods=n_days, freq="B")
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n_days))
    return pd.DataFrame({
        "trade_date": [d.strftime("%Y%m%d") for d in dates],
        "close_qfq": close,
    })


def _week_ends_from_daily(daily: pd.DataFrame, n_weeks: int) -> list:
    from backend.etl.b4_macd import convert_daily_to_weekly_resample_w

    w = convert_daily_to_weekly_resample_w(daily)
    return w["trade_date"].astype(str).tail(n_weeks).tolist()


def test_b4_weekly_target_indices_matches_full_expanding():
    daily = _synthetic_daily(600)
    week_ends = _week_ends_from_daily(daily, 120)
    full_t, full_c = b4_weekly_series_from_daily(daily, week_ends)
    for idx in [0, 1, 59, 119]:
        t_sub, c_sub = b4_weekly_series_from_daily(
            daily, week_ends, target_indices={idx},
        )
        assert t_sub[idx] == full_t[idx]
        assert c_sub[idx] == full_c[idx]
        for j, (tv, cv) in enumerate(zip(t_sub, c_sub)):
            if j != idx:
                assert tv is None
                assert cv is None


def test_require_b4_weekly_target_indices_gate():
    from backend.etl.calc_macd import (
        require_b4_weekly_target_indices,
    )

    df = pd.DataFrame({
        "trade_date": ["20260101", "20260108", "20260115"],
        "close_qfq": [10.0, 10.5, 11.0],
    })
    assert require_b4_weekly_target_indices(df, ["20260115"]) == [2]
    with pytest.raises(ValueError, match="new_bars"):
        require_b4_weekly_target_indices(df, None)
    with pytest.raises(ValueError, match="duplicate"):
        require_b4_weekly_target_indices(df, ["20260101", "20260101"])


def test_macd_weekly_derived_b4_target_matches_full():
    """_compute_macd_derived: B4 at target bar matches full expanding."""
    from backend.etl.calc_macd import MACDCalculator

    daily = _synthetic_daily(600)
    from backend.etl.b4_macd import convert_daily_to_weekly_resample_w

    weekly_df = convert_daily_to_weekly_resample_w(daily).tail(80).reset_index(drop=True)
    weekly_df["trade_date"] = weekly_df["trade_date"].dt.strftime("%Y%m%d")
    calc = MACDCalculator(None, "weekly")
    full = calc._compute_macd_derived(
        calc._compute_macd_core(weekly_df.copy()),
        daily_for_b4=daily,
    )
    last_idx = len(weekly_df) - 1
    target = {last_idx}
    sub = calc._compute_macd_derived(
        calc._compute_macd_core(weekly_df.copy()),
        daily_for_b4=daily,
        target_indices=target,
        b4_target_indices=target,
    )
    assert sub.iloc[last_idx]["trend"] == full.iloc[last_idx]["trend"]
    assert sub.iloc[last_idx]["turning_point"] == full.iloc[last_idx]["turning_point"]


def test_b4_weekly_target_indices_multi_bar():
    daily = _synthetic_daily(600)
    week_ends = _week_ends_from_daily(daily, 80)
    full_t, full_c = b4_weekly_series_from_daily(daily, week_ends)
    targets = {10, 40, 79}
    t_sub, c_sub = b4_weekly_series_from_daily(
        daily, week_ends, target_indices=targets,
    )
    for idx in targets:
        assert t_sub[idx] == full_t[idx]
        assert c_sub[idx] == full_c[idx]
