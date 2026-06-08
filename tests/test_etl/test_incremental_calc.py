"""Golden-master tests: window incremental calc vs full-history oracle."""
import random

import duckdb
import numpy as np
import pandas as pd

from backend.etl.base import (
    ema, insert_dws_batch, load_quote_groups, load_ema_seed,
    resolve_ema_anchor_date, resolve_ema_seeds,
    compute_price_signal_divergence,
)
from backend.etl.calc_macd import MACDCalculator
from backend.etl.calc_ma import MACalculator
from backend.etl.calc_volume import VolumeCalculator
from backend.etl.calc_price_position import PricePositionCalculator
from backend.etl.calc_dde import DDECalculator
from backend.etl.orchestrator import resolve_recalc_start
from backend.etl.recalc_spec import resolve_load_start
from backend.etl.recalc_spec import resolve_recalc_bars, collect_specs, resolve_warmup_tdays


def _seed_dim_date(con, dates, create_table=True):
    if create_table:
        con.execute("""
            CREATE TABLE dim_date (
                trade_date TEXT PRIMARY KEY,
                is_trade_day INTEGER,
                is_week_end INTEGER,
                is_month_end INTEGER,
                is_year_end INTEGER,
                year INTEGER,
                quarter INTEGER,
                month INTEGER,
                week_of_year INTEGER
            )
        """)
    for i, d in enumerate(dates):
        con.execute(
            "INSERT INTO dim_date VALUES (?,1,?,?,?,?,?,?,?)",
            [d, 1 if i % 5 == 4 else 0, 0, 0, 2024, 1, 1, 1],
        )


def _seed_daily_quote(con, codes, n_bars=320, create_table=True):
    if create_table:
        con.execute("""
            CREATE TABLE dwd_daily_quote (
                ts_code TEXT, trade_date TEXT,
                open_qfq REAL, high_qfq REAL, low_qfq REAL, close_qfq REAL,
                vol REAL, amount REAL, pct_chg REAL,
                total_mv REAL, pe_ttm REAL, turnover_rate REAL, volume_ratio REAL,
                is_suspended INTEGER
            )
        """)
    dates = [f"2023{(i // 30) + 1:02d}{(i % 30) + 1:02d}" for i in range(n_bars)]
    random.seed(42)
    for code in codes:
        price = 10.0 + random.random() * 5
        for i, d in enumerate(dates):
            price += random.uniform(-0.5, 0.5)
            vol = 1000 + random.randint(0, 500)
            con.execute(
                "INSERT INTO dwd_daily_quote VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (code, d, price - 0.2, price + 0.3, price - 0.5, price,
                 vol, vol * price, 0, 1e8, 15, 1.0, 1.0),
            )
    return dates


def _compare_window(full_df, win_df, float_cols, recalc_start, rtol=1e-9):
    """Window-path output must match full-path for trade_date >= recalc_start."""
    full_mask = full_df["trade_date"] >= recalc_start
    win_mask = win_df["trade_date"] >= recalc_start
    f = full_df.loc[full_mask].reset_index(drop=True)
    w = win_df.loc[win_mask].reset_index(drop=True)
    assert list(f["trade_date"]) == list(w["trade_date"])
    for col in float_cols:
        if col not in f.columns:
            continue
        fv = f[col].values.astype(float)
        wv = w[col].values.astype(float)
        both_nan = np.isnan(fv) & np.isnan(wv)
        either_nan = np.isnan(fv) | np.isnan(wv)
        assert np.array_equal(either_nan, both_nan), f"{col}: NaN mismatch"
        m = ~either_nan
        # EMA/trend_strength chain may accumulate ~1e-7 float noise at window edge
        np.testing.assert_allclose(wv[m], fv[m], rtol=rtol, atol=1e-7, err_msg=col)


def test_resolve_recalc_start_daily_backtracks_registry_bars():
    con = duckdb.connect(":memory:")
    dates = [f"2024{(i // 30) + 1:02d}{(i % 30) + 1:02d}" for i in range(400)]
    _seed_dim_date(con, dates)
    calc_date = dates[-1]
    n = resolve_recalc_bars(collect_specs("daily"), safety=5)
    got = resolve_recalc_start(con, calc_date, "daily")
    assert got == dates[-n]
    con.close()


def test_load_quote_groups_start_date_filters():
    con = duckdb.connect(":memory:")
    _seed_daily_quote(con, ["A.SZ"], n_bars=50)
    full = load_quote_groups(con, "dwd_daily_quote", "daily",
                             ["trade_date", "close_qfq"], ["A.SZ"])
    win = load_quote_groups(con, "dwd_daily_quote", "daily",
                            ["trade_date", "close_qfq"], ["A.SZ"],
                            start_date="20230115")
    assert len(full["A.SZ"]) == 50
    assert all(win["A.SZ"]["trade_date"] >= "20230115")
    assert len(win["A.SZ"]) < len(full["A.SZ"])
    con.close()


def test_insert_dws_batch_write_range():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_test (
            ts_code TEXT, trade_date TEXT, val REAL,
            calc_date TEXT, input_fingerprint TEXT, spec_version TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    df = pd.DataFrame({
        "trade_date": ["20260101", "20260102", "20260103"],
        "val": [1.0, 2.0, 3.0],
    })
    insert_dws_batch(
        con, "dws_test", df, "A.SZ", "20260605",
        ["ts_code", "trade_date", "val", "calc_date", "input_fingerprint", "spec_version"],
        ["val"],
        write_start="20260102", write_end="20260102",
    )
    rows = con.execute("SELECT trade_date FROM dws_test ORDER BY trade_date").fetchall()
    assert [r[0] for r in rows] == ["20260102"]
    con.close()


def test_insert_dws_batch_returns_zero_on_empty_write_range():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_test (
            ts_code TEXT, trade_date TEXT, val REAL,
            calc_date TEXT, input_fingerprint TEXT, spec_version TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    df = pd.DataFrame({"trade_date": ["20260101"], "val": [1.0]})
    cols = ["ts_code", "trade_date", "val", "calc_date", "input_fingerprint", "spec_version"]
    n = insert_dws_batch(
        con, "dws_test", df, "A.SZ", "20260605", cols, ["val"],
        write_start="20260901", write_end="20260901",
    )
    assert n == 0
    assert con.execute("SELECT COUNT(*) FROM dws_test").fetchone()[0] == 0
    con.close()


def test_warmup_tdays_from_registry():
    assert resolve_warmup_tdays() >= 250


def test_macd_window_matches_full_oracle():
    con = duckdb.connect(":memory:")
    codes = [f"{i:06d}.SZ" for i in range(1, 11)]
    dates = _seed_daily_quote(con, codes, n_bars=600)
    _seed_dim_date(con, dates)
    calc_date = dates[-1]
    recalc_start = resolve_recalc_start(con, calc_date, "daily")
    calc = MACDCalculator(con, "daily")
    float_cols = ["ema_12", "ema_26", "dif", "dea", "macd_bar", "trend_strength"]
    for code in codes:
        full_g = load_quote_groups(con, "dwd_daily_quote", "daily",
                                   ["trade_date", "close_qfq"], [code])
        load_start = resolve_load_start(con, recalc_start, "daily")
        win_g = load_quote_groups(con, "dwd_daily_quote", "daily",
                                  ["trade_date", "close_qfq"], [code],
                                  start_date=load_start)
        full_out = calc._compute_indicators(full_g[code])
        win_out = calc._compute_indicators(win_g[code])
        _compare_window(full_out, win_out, float_cols, recalc_start)
    con.close()


def test_ma_window_matches_full_oracle():
    con = duckdb.connect(":memory:")
    codes = [f"{i:06d}.SZ" for i in range(1, 11)]
    dates = _seed_daily_quote(con, codes, n_bars=600)
    _seed_dim_date(con, dates)
    calc_date = dates[-1]
    recalc_start = resolve_recalc_start(con, calc_date, "daily")
    calc = MACalculator(con, "daily")
    float_cols = ["ma_5", "ma_10", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope"]
    for code in codes:
        full_g = load_quote_groups(con, "dwd_daily_quote", "daily",
                                   ["trade_date", "close_qfq"], [code])
        load_start = resolve_load_start(con, recalc_start, "daily")
        win_g = load_quote_groups(con, "dwd_daily_quote", "daily",
                                  ["trade_date", "close_qfq"], [code],
                                  start_date=load_start)
        full_out = calc._compute_indicators(full_g[code])
        win_out = calc._compute_indicators(win_g[code])
        _compare_window(full_out, win_out, float_cols, recalc_start)
    con.close()


def test_volume_window_matches_full_oracle():
    con = duckdb.connect(":memory:")
    codes = [f"{i:06d}.SZ" for i in range(1, 11)]
    dates = _seed_daily_quote(con, codes, n_bars=600)
    _seed_dim_date(con, dates)
    calc_date = dates[-1]
    recalc_start = resolve_recalc_start(con, calc_date, "daily")
    calc = VolumeCalculator(con, "daily")
    float_cols = ["ma_vol_5", "pct_vol_rank", "volume_ratio", "trend_strength"]
    for code in codes:
        full_g = load_quote_groups(con, "dwd_daily_quote", "daily",
                                   ["trade_date", "close_qfq", "vol"], [code])
        load_start = resolve_load_start(con, recalc_start, "daily")
        win_g = load_quote_groups(con, "dwd_daily_quote", "daily",
                                  ["trade_date", "close_qfq", "vol"], [code],
                                  start_date=load_start)
        full_out = calc._compute_indicators(full_g[code])
        win_out = calc._compute_indicators(win_g[code])
        _compare_window(full_out, win_out, float_cols, recalc_start)
    con.close()


def test_price_position_window_matches_full_oracle():
    con = duckdb.connect(":memory:")
    codes = [f"{i:06d}.SZ" for i in range(1, 11)]
    dates = _seed_daily_quote(con, codes, n_bars=600)
    _seed_dim_date(con, dates)
    calc_date = dates[-1]
    recalc_start = resolve_recalc_start(con, calc_date, "daily")
    calc = PricePositionCalculator(con, "daily")
    float_cols = ["price_position_60d", "price_position_120d", "price_position_250d"]
    for code in codes:
        full_g = load_quote_groups(con, "dwd_daily_quote", "daily",
                                   ["trade_date", "close_qfq"], [code])
        load_start = resolve_load_start(con, recalc_start, "daily")
        win_g = load_quote_groups(con, "dwd_daily_quote", "daily",
                                  ["trade_date", "close_qfq"], [code],
                                  start_date=load_start)
        full_out = calc._compute_positions(full_g[code])
        win_out = calc._compute_positions(win_g[code])
        _compare_window(full_out, win_out, float_cols, recalc_start)
    con.close()


# ── Task 4: single pipeline vs serial 12-pass ──

_DAILY_DWS_SNAPSHOTS = [
    ("dws_macd_daily", ["ema_12", "ema_26", "dif", "dea", "macd_bar", "trend_strength"]),
    ("dws_ma_daily", ["ma_5", "ma_10", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope"]),
    ("dws_volume_daily", ["ma_vol_5", "pct_vol_rank", "volume_ratio", "trend_strength"]),
    ("dws_price_position_daily", ["price_position_60d", "price_position_120d", "price_position_250d"]),
]


def _snapshot_daily_dws(con, codes, calc_date):
    out = {}
    for table, float_cols in _DAILY_DWS_SNAPSHOTS:
        ph = ",".join(["?"] * len(codes))
        df = con.execute(
            f"SELECT ts_code, trade_date, {', '.join(float_cols)} "
            f"FROM {table} WHERE ts_code IN ({ph}) AND calc_date = ? "
            f"ORDER BY ts_code, trade_date",
            codes + [calc_date],
        ).df()
        out[table] = df
    return out


def _assert_dws_snapshots_equal(serial, pipeline):
    for table in serial:
        s = serial[table].sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        p = pipeline[table].sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        assert list(s.columns) == list(p.columns)
        assert list(s["ts_code"]) == list(p["ts_code"])
        assert list(s["trade_date"]) == list(p["trade_date"])
        for col in s.columns:
            if col in ("ts_code", "trade_date"):
                continue
            sv = s[col].values.astype(float)
            pv = p[col].values.astype(float)
            both_nan = np.isnan(sv) & np.isnan(pv)
            either_nan = np.isnan(sv) | np.isnan(pv)
            assert np.array_equal(either_nan, both_nan), f"{table}.{col}: NaN mismatch"
            m = ~either_nan
            np.testing.assert_allclose(pv[m], sv[m], rtol=1e-9, atol=1e-7, err_msg=f"{table}.{col}")


def test_calc_stock_pipeline_matches_serial_daily_quote_calculators(db_with_schema):
    """Pipeline (1 quote load) must match serial 12-pass for daily quote DWS tables."""
    from backend.etl.orchestrator import calc_stock_pipeline, CALCULATORS

    con = db_with_schema
    codes = ["000001.SZ", "000002.SZ"]
    for code in codes:
        con.execute(
            "INSERT INTO dim_stock (ts_code, stock_code, name, is_st) VALUES (?,?,?,0)",
            [code, code[:6], code],
        )
    dates = _seed_daily_quote(con, codes, n_bars=600, create_table=False)
    _seed_dim_date(con, dates, create_table=False)
    calc_date = dates[-1]

    for CalcCls in CALCULATORS:
        for freq in ("daily", "weekly"):
            calc = CalcCls(con, freq)
            calc.calculate(codes, calc_date, recalc_start=None)

    serial_snap = _snapshot_daily_dws(con, codes, calc_date)

    for table, _ in _DAILY_DWS_SNAPSHOTS:
        ph = ",".join(["?"] * len(codes))
        con.execute(
            f"DELETE FROM {table} WHERE ts_code IN ({ph}) AND calc_date = ?",
            codes + [calc_date],
        )

    for code in codes:
        calc_stock_pipeline(con, code, calc_date, daily_recalc=None, weekly_recalc=None)

    pipeline_snap = _snapshot_daily_dws(con, codes, calc_date)
    _assert_dws_snapshots_equal(serial_snap, pipeline_snap)


# ── Task 6: PricePosition deque ──


def _pp_oracle_rolling(close, windows):
    """Frozen pandas rolling oracle for price_position columns."""
    out = {}
    for window in windows:
        s = pd.Series(close)
        roll_min = s.rolling(window, min_periods=2).min().values
        roll_max = s.rolling(window, min_periods=2).max().values
        denom = roll_max - roll_min
        with np.errstate(divide="ignore", invalid="ignore"):
            out[window] = np.where(
                denom > 0,
                (close - roll_min) / denom * 100.0,
                np.nan,
            )
    return out


def test_rolling_window_minmax_deque_matches_pandas():
    from backend.etl.base import rolling_window_minmax_deque

    rng = np.random.default_rng(17)
    for window in [60, 120, 250]:
        close = rng.uniform(5.0, 25.0, size=400)
        got_min, got_max = rolling_window_minmax_deque(close, window, min_periods=2)
        exp_min = pd.Series(close).rolling(window, min_periods=2).min().values
        exp_max = pd.Series(close).rolling(window, min_periods=2).max().values
        np.testing.assert_allclose(got_min, exp_min, rtol=0, atol=1e-9, equal_nan=True)
        np.testing.assert_allclose(got_max, exp_max, rtol=0, atol=1e-9, equal_nan=True)


def test_rolling_window_minmax_deque_extreme_eviction():
    """When the sliding-out bar was the window min, deque must pick next min."""
    from backend.etl.base import rolling_window_minmax_deque

    close = np.array([1.0, 10.0, 10.0, 10.0, 2.0, 15.0], dtype=float)
    window = 3
    got_min, got_max = rolling_window_minmax_deque(close, window, min_periods=2)
    exp_min = pd.Series(close).rolling(window, min_periods=2).min().values
    np.testing.assert_allclose(got_min, exp_min, rtol=0, atol=1e-9, equal_nan=True)
    assert got_min[4] == 2.0
    assert got_min[3] == 10.0


def test_pp_deque_compute_positions_matches_rolling_oracle():
    """PricePositionCalculator._compute_positions must match pandas rolling oracle."""
    calc = PricePositionCalculator.__new__(PricePositionCalculator)
    calc.WINDOWS = [60, 120, 250]

    rng = np.random.default_rng(23)
    n = 400
    dates = [f"2023{(i // 30) + 1:02d}{(i % 30) + 1:02d}" for i in range(n)]
    close = rng.uniform(8.0, 22.0, size=n)
    df = pd.DataFrame({"trade_date": dates, "close_qfq": close})

    result = calc._compute_positions(df)
    oracle = _pp_oracle_rolling(close, calc.WINDOWS)

    for window in calc.WINDOWS:
        col = f"price_position_{window}d"
        np.testing.assert_allclose(
            result[col].values.astype(float),
            oracle[window],
            rtol=0, atol=1e-9, equal_nan=True,
            err_msg=col,
        )


def test_pp_daily_append_one_bar_matches_full_recompute():
    """Incremental daily update: full history vs history+1 new bar tail."""
    calc = PricePositionCalculator.__new__(PricePositionCalculator)
    calc.WINDOWS = [60, 120, 250]

    rng = np.random.default_rng(31)
    n = 300
    dates = [f"2024{(i // 30) + 1:02d}{(i % 30) + 1:02d}" for i in range(n)]
    close = rng.uniform(10.0, 20.0, size=n)

    base_df = pd.DataFrame({"trade_date": dates, "close_qfq": close})
    full = calc._compute_positions(base_df.copy())

    new_close = rng.uniform(10.0, 20.0)
    ext_df = pd.concat([
        base_df,
        pd.DataFrame({"trade_date": [dates[-1][:6] + "31"], "close_qfq": [new_close]}),
    ], ignore_index=True)
    ext = calc._compute_positions(ext_df.copy())

    for window in calc.WINDOWS:
        col = f"price_position_{window}d"
        np.testing.assert_allclose(
            ext[col].iloc[:-1].values.astype(float),
            full[col].values.astype(float),
            rtol=0, atol=1e-9, equal_nan=True,
            err_msg=f"{col} history unchanged after append",
        )


# ── Task 7: EMA seed (MACD + DDE) ──


def test_load_ema_seed_reads_latest_calc_date():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_macd_daily (
            ts_code TEXT, trade_date TEXT, ema_26 REAL,
            calc_date TEXT, input_fingerprint TEXT, spec_version TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    con.execute("""
        INSERT INTO dws_macd_daily VALUES
        ('000001.SZ', '20260101', 10.0, '20260601', 'fp1', 'v1'),
        ('000001.SZ', '20260101', 11.5, '20260605', 'fp2', 'v1')
    """)
    assert load_ema_seed(con, "dws_macd_daily", "000001.SZ", "20260101", "ema_26") == 11.5
    assert load_ema_seed(con, "dws_macd_daily", "000001.SZ", "20260102", "ema_26") is None
    con.close()


def test_ema_with_seed_matches_full_history_tail():
    rng = np.random.default_rng(42)
    close = rng.uniform(10.0, 20.0, size=500)
    for period in (12, 26, 9, 5):
        full = ema(close, period)
        cut = 300
        seeded = ema(close[cut:], period, seed=full[cut - 1])
        np.testing.assert_allclose(
            seeded, full[cut:], rtol=0, atol=1e-9, equal_nan=True,
            err_msg=f"period={period}",
        )


def _seed_macd_dws(con, code, full_out, calc_date):
    con.execute("""
        CREATE TABLE IF NOT EXISTS dws_macd_daily (
            ts_code TEXT, trade_date TEXT,
            ema_12 REAL, ema_26 REAL, dif REAL, dea REAL, macd_bar REAL,
            zone TEXT, trend TEXT, trend_strength REAL,
            divergence TEXT, turning_point TEXT, alert TEXT,
            calc_date TEXT, input_fingerprint TEXT, spec_version TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    dws_cols = [
        "ts_code", "trade_date", "ema_12", "ema_26", "dif", "dea", "macd_bar",
        "zone", "trend", "trend_strength", "divergence", "turning_point", "alert",
        "calc_date", "input_fingerprint", "spec_version",
    ]
    float_cols = ["ema_12", "ema_26", "dif", "dea", "macd_bar", "trend_strength"]
    insert_dws_batch(con, "dws_macd_daily", full_out, code, calc_date,
                     dws_cols, float_cols, input_fingerprint="seed-test")


def test_macd_ema_seed_matches_full_oracle():
    con = duckdb.connect(":memory:")
    codes = [f"{i:06d}.SZ" for i in range(1, 6)]
    dates = _seed_daily_quote(con, codes, n_bars=600)
    _seed_dim_date(con, dates)
    calc_date = dates[-1]
    recalc_start = resolve_recalc_start(con, calc_date, "daily")
    calc = MACDCalculator(con, "daily")
    float_cols = ["ema_12", "ema_26", "dif", "dea", "macd_bar"]
    prior_calc = "20260601"
    for code in codes:
        full_g = load_quote_groups(con, "dwd_daily_quote", "daily",
                                   ["trade_date", "close_qfq"], [code])
        full_out = calc._compute_indicators(full_g[code])
        _seed_macd_dws(con, code, full_out, prior_calc)
        load_start = resolve_load_start(con, recalc_start, "daily")
        win_g = load_quote_groups(con, "dwd_daily_quote", "daily",
                                  ["trade_date", "close_qfq"], [code],
                                  start_date=load_start)
        seeds = resolve_ema_seeds(
            con, calc.dws_table, code, win_g[code], "daily",
            ("ema_12", "ema_26", "dea"), recalc_start,
        )
        assert seeds is not None
        seeded_out = calc._compute_indicators(win_g[code], ema_seeds=seeds)
        _compare_window(full_out, seeded_out, float_cols, recalc_start, rtol=1e-4)
    con.close()


# ── Task 8: divergence vectorization golden-master ──


def _macd_divergence_oracle(df):
    """Frozen loop oracle — MACD _compute_divergence pre-vectorization."""
    result = [None] * len(df)
    w = 59
    for i in range(w, len(df)):
        window_close = df["close_qfq"].iloc[i - w: i + 1]
        window_dif = df["dif"].iloc[i - w: i + 1]
        c_hi = window_close.max()
        c_lo = window_close.min()
        d_hi = window_dif.max()
        d_lo = window_dif.min()
        cur_c = df["close_qfq"].iloc[i]
        cur_d = df["dif"].iloc[i]
        if pd.isna(cur_c) or pd.isna(cur_d):
            continue
        dif_peak_iloc = np.argmax(window_dif.values)
        if (dif_peak_iloc < w and d_hi != 0 and cur_d < d_hi
                and cur_c >= c_hi * 0.98):
            recent = any(result[j] == "top_divergence"
                         for j in range(max(0, i - 5), i))
            if not recent:
                result[i] = "top_divergence"
        dif_valley_iloc = np.argmin(window_dif.values)
        d_recovery = (cur_d - d_lo) / abs(d_lo) if d_lo != 0 else 0
        c_lo_iloc = np.argmin(window_close.values)
        if (dif_valley_iloc < w and d_lo != 0 and cur_d > d_lo
                and d_recovery > 0.1 and (w - c_lo_iloc) >= 3
                and cur_c <= c_lo * 1.02):
            recent = any(result[j] == "bottom_divergence"
                         for j in range(max(0, i - 5), i))
            if not recent:
                result[i] = "bottom_divergence"
    return result


def _dde_divergence_oracle(df):
    """Frozen loop oracle — DDE _compute_divergence pre-vectorization."""
    result = [None] * len(df)
    w = 59
    for i in range(w, len(df)):
        window_close = df["close_qfq"].iloc[i - w: i + 1]
        window_ddx = df["ddx"].iloc[i - w: i + 1]
        if window_ddx.isna().any():
            continue
        c_hi = window_close.max()
        c_lo = window_close.min()
        d_hi = window_ddx.max()
        d_lo = window_ddx.min()
        cur_c = df["close_qfq"].iloc[i]
        cur_d = df["ddx"].iloc[i]
        if pd.isna(cur_c) or pd.isna(cur_d):
            continue
        ddx_peak_iloc = np.argmax(window_ddx.values)
        ddx_peak_val = window_ddx.max()
        neighbors = window_ddx.values[
            max(0, ddx_peak_iloc - 2):min(len(window_ddx), ddx_peak_iloc + 3)
        ]
        is_spike = (neighbors >= ddx_peak_val * 0.8).sum() < 2
        if (ddx_peak_iloc < w and d_hi != 0 and cur_d < d_hi
                and not is_spike and cur_c >= c_hi * 0.98):
            recent = any(result[j] == "top_divergence"
                         for j in range(max(0, i - 5), i))
            if not recent:
                result[i] = "top_divergence"
        ddx_valley_iloc = np.argmin(window_ddx.values)
        d_recovery = (cur_d - d_lo) / abs(d_lo) if d_lo != 0 else 0
        c_lo_iloc = np.argmin(window_close.values)
        if (ddx_valley_iloc < w and d_lo != 0 and cur_d > d_lo
                and d_recovery > 0.1 and (w - c_lo_iloc) >= 3
                and cur_c <= c_lo * 1.02):
            recent = any(result[j] == "bottom_divergence"
                         for j in range(max(0, i - 5), i))
            if not recent:
                result[i] = "bottom_divergence"
    return result


def _volume_divergence_oracle(df):
    """Frozen loop oracle — Volume _compute_divergence pre-vectorization."""
    result = [None] * len(df)
    w = 59
    for i in range(w, len(df)):
        window_close = df["close_qfq"].iloc[i - w:i + 1]
        window_vol = df["vol"].iloc[i - w:i + 1]
        c_hi = window_close.max()
        c_lo = window_close.min()
        v_hi = window_vol.max()
        v_lo = window_vol.min()
        cur_c = df["close_qfq"].iloc[i]
        cur_v = df["vol"].iloc[i]
        if pd.isna(cur_c) or pd.isna(cur_v):
            continue
        vol_peak_iloc = np.argmax(window_vol.values)
        if (vol_peak_iloc < w and v_hi != 0
                and cur_v < window_vol.values[vol_peak_iloc]
                and cur_c >= c_hi * 0.98):
            recent = any(result[j] == "top_divergence"
                         for j in range(max(0, i - 5), i))
            if not recent:
                result[i] = "top_divergence"
        vol_valley_iloc = np.argmin(window_vol.values)
        v_recovery = (cur_v - v_lo) / abs(v_lo) if v_lo != 0 else 0
        c_lo_iloc = np.argmin(window_close.values)
        if (vol_valley_iloc < w and v_lo != 0
                and cur_v > window_vol.values[vol_valley_iloc]
                and v_recovery > 0.1 and (w - c_lo_iloc) >= 3
                and cur_c <= c_lo * 1.02):
            recent = any(result[j] == "bottom_divergence"
                         for j in range(max(0, i - 5), i))
            if not recent:
                result[i] = "bottom_divergence"
    return result


def _assert_divergence_equal(got, oracle, label):
    assert len(got) == len(oracle), label
    assert got == oracle, f"{label}: mismatch at indices " + str(
        [i for i, (a, b) in enumerate(zip(got, oracle)) if a != b][:5]
    )


def test_macd_divergence_vectorized_matches_oracle():
    rng = np.random.default_rng(7)
    calc = MACDCalculator.__new__(MACDCalculator)
    for _ in range(20):
        n = rng.integers(80, 200)
        close = rng.uniform(10.0, 30.0, n)
        dif = rng.uniform(-2.0, 2.0, n)
        if rng.random() < 0.1:
            close[rng.integers(0, n, 3)] = np.nan
        df = pd.DataFrame({"close_qfq": close, "dif": dif})
        _assert_divergence_equal(
            calc._compute_divergence(df), _macd_divergence_oracle(df), "macd-random"
        )


def test_dde_divergence_vectorized_matches_oracle():
    rng = np.random.default_rng(11)
    calc = DDECalculator.__new__(DDECalculator)
    for _ in range(20):
        n = rng.integers(80, 200)
        close = rng.uniform(10.0, 30.0, n)
        ddx = rng.uniform(-0.05, 0.05, n)
        if rng.random() < 0.15:
            ddx[rng.integers(0, n, 5)] = np.nan
        df = pd.DataFrame({"close_qfq": close, "ddx": ddx})
        _assert_divergence_equal(
            calc._compute_divergence(df), _dde_divergence_oracle(df), "dde-random"
        )


def test_volume_divergence_vectorized_matches_oracle():
    rng = np.random.default_rng(13)
    calc = VolumeCalculator.__new__(VolumeCalculator)
    for _ in range(20):
        n = rng.integers(80, 200)
        close = rng.uniform(10.0, 30.0, n)
        vol = rng.uniform(500.0, 5000.0, n)
        df = pd.DataFrame({"close_qfq": close, "vol": vol})
        _assert_divergence_equal(
            calc._compute_divergence(df), _volume_divergence_oracle(df), "vol-random"
        )


def test_divergence_golden_on_seeded_quotes():
    """10 stocks × full _compute_indicators divergence vs loop oracle."""
    con = duckdb.connect(":memory:")
    codes = [f"{i:06d}.SZ" for i in range(1, 11)]
    dates = _seed_daily_quote(con, codes, n_bars=600)
    _seed_dim_date(con, dates)
    macd_calc = MACDCalculator(con, "daily")
    vol_calc = VolumeCalculator(con, "daily")
    for code in codes:
        g = load_quote_groups(con, "dwd_daily_quote", "daily",
                              ["trade_date", "close_qfq", "vol"], [code])
        df = g[code]
        macd_out = macd_calc._compute_indicators(df.copy())
        macd_df = df.copy()
        macd_df["dif"] = macd_out["dif"]
        _assert_divergence_equal(
            macd_calc._compute_divergence(macd_df),
            _macd_divergence_oracle(macd_df),
            code,
        )
        vol_out = vol_calc._compute_indicators(df.copy())
        vol_df = df.copy()
        vol_df["divergence"] = vol_out.get("divergence")
        got = vol_calc._compute_divergence(vol_df)
        _assert_divergence_equal(got, _volume_divergence_oracle(vol_df), code)
    con.close()


def test_count_calc_rows_scoped_by_ts_code():
    """_count_calc_rows 只统计指定 ts_code + calc_date 的行，不含其他股票。"""
    from backend.etl.orchestrator import _count_calc_rows

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_x (ts_code TEXT, trade_date TEXT, calc_date TEXT)
    """)
    con.execute("INSERT INTO dws_x VALUES ('A.SZ','20260101','20260605')")
    con.execute("INSERT INTO dws_x VALUES ('A.SZ','20260102','20260605')")
    con.execute("INSERT INTO dws_x VALUES ('B.SZ','20260101','20260605')")
    con.execute("INSERT INTO dws_x VALUES ('C.SZ','20260101','20260604')")  # 旧 calc_date

    assert _count_calc_rows(con, "dws_x", "20260605", ["A.SZ"]) == 2
    assert _count_calc_rows(con, "dws_x", "20260605", ["A.SZ", "B.SZ"]) == 3
    assert _count_calc_rows(con, "dws_x", "20260605", ["C.SZ"]) == 0
    assert _count_calc_rows(con, "dws_x", "20260605", []) == 0
    con.close()


def test_load_latest_fingerprints_picks_latest_trade_date_on_tie():
    """同一 calc_date 下混有两代指纹时，取最新 trade_date 的那条。"""
    from backend.etl.base import load_latest_fingerprints
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_x (
            ts_code TEXT, trade_date TEXT, calc_date TEXT, input_fingerprint TEXT
        )
    """)
    con.execute("INSERT INTO dws_x VALUES ('A.SZ','20260101','20260605','OLD')")
    con.execute("INSERT INTO dws_x VALUES ('A.SZ','20260605','20260605','NEW')")
    fps = load_latest_fingerprints(con, "dws_x", ["A.SZ"])
    assert fps["A.SZ"] == "NEW"
    con.close()


def test_history_signature_detects_value_change_summary_stats_stable():
    import pandas as pd
    from backend.etl.base import compute_history_signature
    df1 = pd.DataFrame({
        "trade_date": ["20260101", "20260102", "20260103"],
        "close_qfq": [10.0, 20.0, 30.0],
        "vol": [100.0, 200.0, 300.0],
    })
    df2 = pd.DataFrame({
        "trade_date": ["20260101", "20260102", "20260103"],
        "close_qfq": [20.0, 10.0, 30.0],
        "vol": [100.0, 200.0, 300.0],
    })
    cols = ["close_qfq", "vol"]
    assert compute_history_signature(df1, cols) != compute_history_signature(df2, cols)


def test_history_signature_stable_under_float_noise_below_precision():
    import pandas as pd
    from backend.etl.base import compute_history_signature
    df1 = pd.DataFrame({"trade_date": ["20260101"], "close_qfq": [10.0000001], "vol": [100.0]})
    df2 = pd.DataFrame({"trade_date": ["20260101"], "close_qfq": [10.0000002], "vol": [100.0]})
    cols = ["close_qfq", "vol"]
    assert compute_history_signature(df1, cols) == compute_history_signature(df2, cols)


def test_history_signature_changes_on_real_change():
    import pandas as pd
    from backend.etl.base import compute_history_signature
    df1 = pd.DataFrame({"trade_date": ["20260101"], "close_qfq": [10.0], "vol": [100.0]})
    df2 = pd.DataFrame({"trade_date": ["20260101"], "close_qfq": [10.5], "vol": [100.0]})
    assert compute_history_signature(df1, ["close_qfq", "vol"]) != \
           compute_history_signature(df2, ["close_qfq", "vol"])
