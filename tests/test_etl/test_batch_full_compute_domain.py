"""Batch FULL compute-domain wiring (算域=写域)."""
import duckdb
import numpy as np
import pandas as pd
import pytest

from backend.etl.calc_batch_append import batch_full_volume
from backend.etl.calc_compute_domain import resolve_compute_indices
from backend.etl.calc_volume import (
    VOLUME_TREND_V2_DAILY,
    VOLUME_TREND_V2_WEEKLY,
    VolumeCalculator,
    compute_volume_trend_series,
)


def _make_volume_quote_df(n: int, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = 20250101
    dates = [str(base + i) for i in range(n)]
    vol = np.maximum(rng.lognormal(11.0, 0.4, size=n), 100.0)
    return pd.DataFrame({
        "trade_date": dates,
        "vol": vol,
        "close_qfq": 10.0 + np.arange(n) * 0.01,
        "active_days": rng.integers(3, 6, size=n).astype(float),
    })


def test_batch_full_volume_passes_write_window_trend_indices(monkeypatch):
    captured = []
    orig = VolumeCalculator._compute_volume_derived

    def spy(self, df, zone_seed=None, trend_target_indices=None):
        captured.append(list(trend_target_indices) if trend_target_indices is not None else None)
        return orig(self, df, zone_seed=zone_seed, trend_target_indices=trend_target_indices)

    monkeypatch.setattr(VolumeCalculator, "_compute_volume_derived", spy)
    monkeypatch.setattr("backend.etl.base.check_dwd_unchanged", lambda *a, **k: False)
    monkeypatch.setattr("backend.etl.base.insert_dws_batch_multi", lambda *a, **k: 1)
    monkeypatch.setattr("backend.etl.base.load_latest_fingerprints", lambda *a, **k: {})
    monkeypatch.setattr("backend.etl.base.load_latest_spec_versions", lambda *a, **k: {})

    con = duckdb.connect(":memory:")
    df = _make_volume_quote_df(130, seed=7)
    recalc_start = str(df.iloc[100]["trade_date"])
    calc_date = str(df.iloc[-1]["trade_date"])
    expected_idx = resolve_compute_indices(df, recalc_start, calc_date)

    batch_full_volume(
        con, "daily", ["T.SZ"], calc_date, recalc_start, {"T.SZ": df},
    )

    assert len(captured) == 1
    assert captured[0] == expected_idx
    assert len(captured[0]) == len(expected_idx)
    con.close()


def test_volume_trend_write_window_matches_expanding_daily():
    rng = np.random.default_rng(11)
    n = 245
    vol = np.maximum(rng.lognormal(11.0, 0.5, size=n), 100.0)
    df = pd.DataFrame({
        "trade_date": [str(20250101 + i) for i in range(n)],
        "vol": vol,
    })
    recalc_start = str(df.iloc[180]["trade_date"])
    calc_date = str(df.iloc[-1]["trade_date"])
    idx = resolve_compute_indices(df, recalc_start, calc_date)

    full = compute_volume_trend_series(vol, VOLUME_TREND_V2_DAILY)
    windowed = compute_volume_trend_series(
        vol, VOLUME_TREND_V2_DAILY, target_indices=idx,
    )
    for i in idx:
        assert windowed[i] == full[i], f"daily idx={i} window={windowed[i]!r} full={full[i]!r}"


def test_volume_trend_write_window_matches_expanding_weekly():
    rng = np.random.default_rng(13)
    n = 80
    vol = np.maximum(rng.lognormal(11.0, 0.4, size=n), 100.0)
    ad = rng.integers(3, 6, size=n).astype(float)
    vol_scaled = vol * ad / 5.0
    df = pd.DataFrame({
        "trade_date": [str(20240101 + i * 7) for i in range(n)],
        "vol": vol,
        "active_days": ad,
    })
    recalc_start = str(df.iloc[50]["trade_date"])
    calc_date = str(df.iloc[-1]["trade_date"])
    idx = resolve_compute_indices(df, recalc_start, calc_date)

    full = compute_volume_trend_series(vol_scaled, VOLUME_TREND_V2_WEEKLY)
    windowed = compute_volume_trend_series(
        vol_scaled, VOLUME_TREND_V2_WEEKLY, target_indices=idx,
    )
    for i in idx:
        assert windowed[i] == full[i], f"weekly idx={i} window={windowed[i]!r} full={full[i]!r}"


def test_batch_full_volume_derived_trend_matches_full_on_write_window(monkeypatch):
    """Integration: batch FULL compute path trend column equals full expanding on write window."""
    monkeypatch.setattr("backend.etl.base.check_dwd_unchanged", lambda *a, **k: False)
    monkeypatch.setattr("backend.etl.base.insert_dws_batch_multi", lambda *a, **k: 1)
    monkeypatch.setattr("backend.etl.base.load_latest_fingerprints", lambda *a, **k: {})
    monkeypatch.setattr("backend.etl.base.load_latest_spec_versions", lambda *a, **k: {})

    con = duckdb.connect(":memory:")
    df = _make_volume_quote_df(130, seed=99)
    recalc_start = str(df.iloc[90]["trade_date"])
    calc_date = str(df.iloc[-1]["trade_date"])
    idx = resolve_compute_indices(df, recalc_start, calc_date)

    calc = VolumeCalculator(con, "daily")
    full_df = calc._compute_indicators(df.copy())

    _, stock_rows = batch_full_volume(
        con, "daily", ["T.SZ"], calc_date, recalc_start, {"T.SZ": df},
    )
    assert len(stock_rows) == 1
    out = stock_rows[0][1]

    for i in idx:
        td = str(out.iloc[i]["trade_date"])
        assert out.iloc[i]["trend"] == full_df.iloc[i]["trend"], (
            f"trend mismatch trade_date={td} idx={i}"
        )
    con.close()
