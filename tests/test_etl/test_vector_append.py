"""Golden tests: vector MACD batch vs per-stock oracle."""
import numpy as np
import pandas as pd
import pytest


def _make_tail(n: int = 80, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 10.0 + np.cumsum(rng.normal(0, 0.15, n))
    dates = [
        (pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)).strftime("%Y%m%d")
        for i in range(n)
    ]
    return pd.DataFrame({"trade_date": dates, "close_qfq": close})


def _make_vol_tail(n: int = 130, seed: int = 0) -> pd.DataFrame:
    df = _make_tail(n, seed=seed)
    rng = np.random.default_rng(seed + 100)
    df["vol"] = np.maximum(rng.lognormal(11.0, 0.4, n), 100.0)
    return df


def test_ema_seeded_matrix_matches_base_ema():
    from backend.etl.base import ema
    from backend.etl.vector.macd_batch import ema_seeded_matrix

    rng = np.random.default_rng(1)
    close = 10.0 + np.cumsum(rng.normal(0, 0.1, 60))
    seed = 9.5
    oracle = ema(close.astype(float), 12, seed=seed)

    mat = ema_seeded_matrix(close.reshape(1, -1), 12, np.array([seed]))
    np.testing.assert_allclose(mat[0], oracle, rtol=0, atol=1e-9)


def test_batch_macd_ema_core_matches_compute_macd_core():
    from backend.etl.calc_macd import MACDCalculator
    from backend.etl.vector.macd_batch import attach_macd_core_to_df, batch_macd_ema_core

    codes = ["A.SZ", "B.SZ", "C.SZ"]
    groups = {c: _make_tail(80, seed=i) for i, c in enumerate(codes)}
    seeds = {
        c: {"ema_12": 10.1 + i * 0.01, "ema_26": 10.0 + i * 0.01, "dea": 0.05}
        for i, c in enumerate(codes)
    }
    cores = batch_macd_ema_core(codes, groups, seeds)
    calc = MACDCalculator(None, "daily")

    float_cols = ["ema_12", "ema_26", "dif", "dea", "macd_bar"]
    for code in codes:
        oracle = calc._compute_macd_core(groups[code].copy(), ema_seeds=seeds[code])
        vector_df = attach_macd_core_to_df(groups[code], cores[code])
        for col in float_cols:
            np.testing.assert_allclose(
                vector_df[col].values, oracle[col].values, rtol=0, atol=1e-9,
                err_msg=f"{code} {col}",
            )


def test_vector_batch_macd_matches_per_stock_append(monkeypatch):
    """Vector batch path == per-stock _compute_indicators on tail window."""
    import duckdb

    from backend.db.schema import create_all_tables, ensure_calc_state_table
    from backend.etl.calc_batch_append import batch_append_macd
    from backend.etl.calc_macd import MACDCalculator
    from backend.etl.calc_router import state_signature
    from backend.etl.calc_state import upsert_calc_state

    codes = ["VA.SZ", "VB.SZ"]
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)

    groups = {}
    new_bars_map = {}
    seeds = {}
    calc = MACDCalculator(con, "daily")
    calc_date = None

    for i, code in enumerate(codes):
        df = _make_tail(90, seed=10 + i)
        groups[code] = df
        new_td = df["trade_date"].iloc[-1]
        new_bars_map[code] = [new_td]
        calc_date = new_td
        prev_td = df["trade_date"].iloc[-2]
        fp = state_signature(df.iloc[:-1], prev_td, calc.SIGNATURE_COLS)
        upsert_calc_state(
            con, code, "daily", "macd",
            last_trade_date=prev_td, history_fp=fp, calc_date=prev_td,
        )
        seeds[code] = {
            "ema_12": float(df["close_qfq"].iloc[-3]),
            "ema_26": float(df["close_qfq"].iloc[-3]),
            "dea": 0.01,
        }

    per_stock = {}
    for code in codes:
        out = calc._compute_indicators(groups[code].copy(), ema_seeds=seeds[code])
        per_stock[code] = out[out["trade_date"] == new_bars_map[code][0]].iloc[0]

    import importlib
    import backend.config as cfg
    from backend.etl import calc_batch_append as ba_mod

    monkeypatch.setenv("CALC_VECTOR_APPEND", "1")
    importlib.reload(cfg)
    assert cfg.CALC_VECTOR_APPEND is True

    def fake_seeds(_con, _table, codes_at_anchor, _first_td, _cols):
        return {c: seeds[c] for c in codes_at_anchor if c in seeds}

    monkeypatch.setattr(ba_mod, "load_ema_seeds_batch", fake_seeds)

    _, stock_rows = batch_append_macd(
        con, "daily", codes, calc_date, groups, new_bars_map, {},
    )
    float_cols = MACDCalculator.FLOAT_COLS
    str_cols = ["divergence", "zone", "turning_point", "alert", "trend"]
    for code, _out, _fp, _w0, _w1 in stock_rows:
        batch_row = _out[_out["trade_date"] == new_bars_map[code][0]].iloc[0]
        single_row = per_stock[code]
        for col in float_cols:
            a, b = batch_row[col], single_row[col]
            if pd.isna(b):
                assert pd.isna(a), f"{code} {col}"
            else:
                assert abs(a - b) < 1e-9, f"{code} {col}: {a} vs {b}"
        for col in str_cols:
            assert batch_row[col] == single_row[col], f"{code} {col}"

    con.close()


def test_batch_ddx_ddx2_core_matches_compute_dde_core():
    import numpy as np
    import pandas as pd

    from backend.etl.calc_dde import DDECalculator
    from backend.etl.vector.dde_batch import attach_dde_core_to_df, batch_ddx_ddx2_core

    codes = ["A.SZ", "B.SZ"]
    n = 60
    dates = [(pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]
    groups = {}
    for j, code in enumerate(codes):
        tv = 10000.0 + j
        groups[code] = pd.DataFrame({
            "trade_date": dates,
            "buy_lg_vol": np.full(n, 3000.0),
            "sell_lg_vol": np.full(n, 2500.0),
            "buy_elg_vol": np.full(n, 1000.0),
            "sell_elg_vol": np.full(n, 800.0),
            "total_vol": np.full(n, tv),
            "net_mf_amount": np.full(n, 700.0),
            "close_qfq": 10.0 + np.arange(n) * 0.01,
        })
    seeds = {c: {"ddx2": 0.02 + i * 0.001} for i, c in enumerate(codes)}
    cores = batch_ddx_ddx2_core(codes, groups, seeds)
    calc = DDECalculator(None, "daily")
    for code in codes:
        oracle = calc._compute_dde_core(groups[code].copy(), ema_seeds=seeds[code])
        vector_df = attach_dde_core_to_df(groups[code], cores[code])
        for col in ("ddx", "ddx2", "net_mf_amount"):
            np.testing.assert_allclose(
                vector_df[col].values, oracle[col].values, rtol=0, atol=1e-9,
                err_msg=f"{code} {col}",
            )


def test_batch_volume_rolling_core_matches_compute_volume_core():
    from backend.etl.calc_volume import VolumeCalculator
    from backend.etl.vector.volume_batch import attach_volume_core_to_df, batch_volume_rolling_core

    codes = ["A.SZ", "B.SZ"]
    groups = {c: _make_vol_tail(130, seed=i) for i, c in enumerate(codes)}
    cores = batch_volume_rolling_core(codes, groups)
    calc = VolumeCalculator(None, "daily")
    float_cols = ["ma_vol_5", "pct_vol_rank", "volume_ratio"]
    for code in codes:
        oracle = calc._compute_volume_core(groups[code].copy())
        vector_df = attach_volume_core_to_df(groups[code], cores[code])
        for col in float_cols:
            np.testing.assert_allclose(
                vector_df[col].values, oracle[col].values, rtol=0, atol=1e-9,
                err_msg=f"{code} {col}",
            )


def test_require_trend_target_indices_gate():
    from backend.etl.calc_volume import require_trend_target_indices

    df = pd.DataFrame({"trade_date": ["20260101", "20260102", "20260103"]})
    assert require_trend_target_indices(df, ["20260102"]) == [1]
    assert require_trend_target_indices(df, ["20260101", "20260103"]) == [0, 2]

    with pytest.raises(ValueError, match="requires new_bars"):
        require_trend_target_indices(df, None)
    with pytest.raises(ValueError, match="non-empty"):
        require_trend_target_indices(df, [])
    with pytest.raises(ValueError, match="missing"):
        require_trend_target_indices(df, ["20260199"], ts_code="X.SZ")
    with pytest.raises(ValueError, match="duplicate"):
        require_trend_target_indices(df, ["20260101", "20260101"], ts_code="X.SZ")


def test_compute_volume_trend_series_target_indices_matches_full():
    """M2c+: target_indices bars match full expanding series."""
    from backend.etl.calc_volume import VOLUME_TREND_V2_DAILY, compute_volume_trend_series

    rng = np.random.default_rng(99)
    vol = np.maximum(rng.lognormal(11.0, 0.5, size=245), 100.0)
    full = compute_volume_trend_series(vol, VOLUME_TREND_V2_DAILY)
    multi = compute_volume_trend_series(
        vol, VOLUME_TREND_V2_DAILY, target_indices=[59, 120, 200, 244],
    )
    for idx in (59, 120, 200, 244):
        assert multi[idx] == full[idx], f"multi idx={idx}"
        partial = compute_volume_trend_series(
            vol, VOLUME_TREND_V2_DAILY, target_indices=[idx],
        )
        assert partial[idx] == full[idx], f"idx={idx} partial={partial[idx]!r} full={full[idx]!r}"


def test_compute_volume_trend_series_weekly_target_indices():
    from backend.etl.calc_volume import (
        VOLUME_TREND_V2_WEEKLY,
        VolumeCalculator,
        compute_volume_trend_series,
    )

    rng = np.random.default_rng(77)
    n = 80
    vol = np.maximum(rng.lognormal(11.0, 0.4, size=n), 100.0)
    ad = rng.integers(3, 6, size=n).astype(float)
    vol_scaled = vol * ad / 5.0
    full = compute_volume_trend_series(vol_scaled, VOLUME_TREND_V2_WEEKLY)
    for idx in (29, 50, 79):
        partial = compute_volume_trend_series(
            vol_scaled, VOLUME_TREND_V2_WEEKLY, target_indices=[idx],
        )
        assert partial[idx] == full[idx]

    calc = VolumeCalculator(None, "weekly")
    df = pd.DataFrame({
        "trade_date": [f"2024{i:04d}" for i in range(n)],
        "vol": vol,
        "close_qfq": 10.0 + np.arange(n) * 0.01,
        "active_days": ad,
    })
    full_df = calc._compute_indicators(df.copy())
    new_dates = [df["trade_date"].iloc[-1]]
    append_df = calc._compute_indicators_append(df.copy(), new_dates)
    assert append_df.iloc[-1]["trend"] == full_df.iloc[-1]["trend"]


def test_volume_trend_last_bar_matches_expanding_series():
    """M2c+: APPEND only needs last-bar trend; must match expanding series at tail."""
    from backend.etl.calc_volume import VOLUME_TREND_V2_DAILY, compute_volume_trend_series
    from scripts.profile_volume_trend_v2 import _synthetic_vol_series

    for seed in range(20):
        vol = _synthetic_vol_series(245, seed)
        full = compute_volume_trend_series(vol, VOLUME_TREND_V2_DAILY)
        last = compute_volume_trend_series(
            vol, VOLUME_TREND_V2_DAILY, target_indices=[len(vol) - 1],
        )
        assert full[-1] == last[-1], f"seed={seed} full={full[-1]!r} last={last[-1]!r}"


def test_volume_append_multi_bar_trend():
    from backend.etl.calc_volume import VolumeCalculator

    df = _make_vol_tail(300, seed=42)
    calc = VolumeCalculator(None, "daily")
    full_df = calc._compute_indicators(df.copy())
    new_dates = full_df["trade_date"].iloc[-3:].astype(str).tolist()
    tail = df.iloc[-140:].reset_index(drop=True)
    zone_seed = full_df.iloc[-141]["zone"]
    append_df = calc._compute_indicators_append(
        tail.copy(), new_dates, zone_seed=zone_seed,
    )
    for td in new_dates:
        assert append_df.loc[append_df["trade_date"] == td, "trend"].iloc[0] == (
            full_df.loc[full_df["trade_date"] == td, "trend"].iloc[0]
        ), f"trend mismatch on {td}"


def _make_vol_tail_weekly(n: int = 130, seed: int = 0) -> pd.DataFrame:
    df = _make_vol_tail(n, seed=seed)
    df["active_days"] = 5.0
    return df


def test_vector_batch_volume_matches_per_stock_append(monkeypatch):
    """Vector batch trend matches per-stock append (M2c+ contract; trend-only)."""
    import importlib

    import duckdb

    import backend.config as cfg
    from backend.db.schema import create_all_tables
    from backend.etl import calc_batch_append as ba_mod
    from backend.etl.calc_batch_append import batch_append_volume
    from backend.etl.calc_volume import VolumeCalculator

    codes = ["VV0.SZ", "VV1.SZ"]
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    groups = {}
    new_bars_map = {}
    calc = VolumeCalculator(con, "daily")
    calc_date = None

    for i, code in enumerate(codes):
        df = _make_vol_tail(140, seed=20 + i)
        groups[code] = df
        new_td = df["trade_date"].iloc[-1]
        new_bars_map[code] = [new_td]
        calc_date = new_td

    per_stock = {}
    for code in codes:
        out = calc._compute_indicators_append(
            groups[code].copy(), new_bars_map[code],
        )
        per_stock[code] = out[out["trade_date"] == new_bars_map[code][0]].iloc[0]

    monkeypatch.setenv("CALC_VECTOR_APPEND", "1")
    importlib.reload(cfg)
    assert cfg.CALC_VECTOR_APPEND is True
    monkeypatch.setattr(ba_mod, "load_zone_seeds_batch", lambda *_a, **_k: {})

    _, stock_rows = batch_append_volume(
        con, "daily", codes, calc_date, groups, new_bars_map, {},
    )
    for code, out, _fp, _w0, _w1 in stock_rows:
        batch_row = out[out["trade_date"] == new_bars_map[code][0]].iloc[0]
        single_row = per_stock[code]
        assert batch_row["trend"] == single_row["trend"], (
            f"{code} trend: batch={batch_row['trend']!r} append={single_row['trend']!r}"
        )

    con.close()


def test_vector_batch_volume_weekly_trend(monkeypatch):
    """Weekly vector batch APPEND: trend matches per-stock append at new bar."""
    import importlib

    import duckdb

    import backend.config as cfg
    from backend.etl import calc_batch_append as ba_mod
    from backend.etl.calc_batch_append import batch_append_volume
    from backend.etl.calc_volume import VolumeCalculator

    codes = ["WV0.SZ"]
    con = duckdb.connect(":memory:")
    from backend.db.schema import create_all_tables
    create_all_tables(con)

    groups = {}
    new_bars_map = {}
    calc = VolumeCalculator(con, "weekly")
    df = _make_vol_tail_weekly(140, seed=31)
    code = codes[0]
    groups[code] = df
    new_td = df["trade_date"].iloc[-1]
    new_bars_map[code] = [new_td]
    calc_date = new_td

    append_out = calc._compute_indicators_append(
        df.copy(), new_bars_map[code], ts_code=code,
    )
    expected_trend = append_out.loc[
        append_out["trade_date"] == new_td, "trend"
    ].iloc[0]

    monkeypatch.setenv("CALC_VECTOR_APPEND", "1")
    importlib.reload(cfg)
    monkeypatch.setattr(ba_mod, "load_zone_seeds_batch", lambda *_a, **_k: {})

    _, stock_rows = batch_append_volume(
        con, "weekly", codes, calc_date, groups, new_bars_map, {},
    )
    batch_row = stock_rows[0][1]
    batch_trend = batch_row.loc[batch_row["trade_date"] == new_td, "trend"].iloc[0]
    assert batch_trend == expected_trend

    con.close()
