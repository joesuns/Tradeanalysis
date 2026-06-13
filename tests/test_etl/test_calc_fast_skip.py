"""Tests for chunk-level calc fast-skip preflight."""
import duckdb
import numpy as np
import pandas as pd

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.base import compute_history_signature
from backend.etl.calc_router import SIG_WINDOW, classify_calc_mode, state_signature
from backend.etl.calc_state import load_calc_state_batch, upsert_calc_state
from backend.etl.calc_fast_skip import (
    batch_load_quote_tails,
    batch_load_dde_tails,
    preflight_stock_modes,
    preflight_stock_modes_v2,
    preflight_stock_modes_with_fps,
    partition_preflight_modes,
    stock_can_fast_skip,
)
from backend.etl.calc_router import state_signature
from backend.etl.calc_state import upsert_calc_state
from backend.etl.calc_indicators import CALC_ROUTE_SPECS, quote_sig_col_union, quote_tail_columns
from backend.etl.calc_dde import DDECalculator
from backend.etl.base import load_quote_groups
from backend.etl.orchestrator import calc_stock_pipeline

TS = "FAST.SZ"
TS2 = "MISS.SZ"


def _seed_daily(con, ts_code, dates, closes):
    rows = []
    for i, d in enumerate(dates):
        c = float(closes[i])
        rows.append((ts_code, d, c, c + 0.1, c - 0.1, c, 1000.0 + i, 0))
    con.executemany(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _seed_week_end(con, ts_code, dates, closes):
    for i, d in enumerate(dates):
        c = float(closes[i])
        con.execute(
            "UPDATE dim_date SET is_week_end = 1 WHERE trade_date = ?", [d],
        )
        con.execute(
            "INSERT INTO dwd_weekly_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, "
            "pct_chg, active_days) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 5)",
            [ts_code, d, c, c + 0.1, c - 0.1, c, 2000.0 + i],
        )


def _minimal_db(con, n_daily=260):
    create_all_tables(con)
    ensure_calc_state_table(con)
    # Valid YYYYMMDD strings (DDE weekly LAG/date math requires real calendar dates).
    dates = [(pd.Timestamp("2020-01-01") + pd.Timedelta(days=i)).strftime("%Y%m%d")
             for i in range(n_daily)]
    for d in dates:
        con.execute(
            "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
            [d],
        )
    rng = np.random.default_rng(9)
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n_daily))
    _seed_daily(con, TS, dates, close)
    # moneyflow for DDE daily
    for i, d in enumerate(dates):
        con.execute(
            "INSERT INTO dwd_daily_moneyflow "
            "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, sell_elg_vol, "
            " total_vol, net_mf_amount) VALUES (?, ?, 10, 5, 3, 2, 1000, 1.5)",
            [TS, d],
        )
    week_dates = dates[::5][:60]
    _seed_week_end(con, TS, week_dates, close[: len(week_dates)])


def _all_states_skip(con, ts_code, last_td, df_daily, df_weekly):
    state_map = {}
    for indicator_name, freq, CalcCls, sig_cols, source in CALC_ROUTE_SPECS:
        if source == "quote":
            df = df_daily if freq == "daily" else df_weekly
        else:
            df = batch_load_dde_tails(con, [ts_code], freq).get(ts_code)
        fp = state_signature(df, last_td, sig_cols)
        spec_ver = getattr(CalcCls, "SPEC_VERSION", "v1")
        upsert_calc_state(con, ts_code, freq, indicator_name,
                          last_trade_date=last_td, history_fp=fp, calc_date="20260602",
                          spec_version=spec_ver)
        state_map[(ts_code, freq, indicator_name)] = {
            "last_trade_date": last_td,
            "history_fp": fp,
            "quote_latest_adj": None,
            "updated_calc_date": "20260602",
            "spec_version": spec_ver,
        }
    return state_map


def test_stock_can_fast_skip_all_skip():
    modes = {("macd", "daily"): ("SKIP", []), ("ma", "weekly"): ("SKIP", [])}
    assert stock_can_fast_skip(modes) is True
    modes[("macd", "daily")] = ("APPEND", ["20260103"])
    assert stock_can_fast_skip(modes) is False


def test_fallthrough_on_missing_stock():
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    state_map = load_calc_state_batch(con, [TS])
    modes = preflight_stock_modes(TS, state_map, None, None, None, None)
    assert modes is None
    con.close()


def test_fallthrough_on_empty_dde():
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    daily = batch_load_quote_tails(con, [TS], "daily", quote_tail_columns("daily"))
    weekly = batch_load_quote_tails(con, [TS], "weekly", quote_tail_columns("weekly"))
    state_map = {}
    modes = preflight_stock_modes(TS, state_map, daily.get(TS), weekly.get(TS),
                                  None, None)
    assert modes is None
    con.close()


def test_preflight_stock_modes_v2_returns_fp_cache_on_skip():
    """All-SKIP preflight must cache fingerprints for skip_refresh reuse."""
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    daily = batch_load_quote_tails(con, [TS], "daily", quote_tail_columns("daily"))
    weekly = batch_load_quote_tails(con, [TS], "weekly", quote_tail_columns("weekly"))
    dde_d = batch_load_dde_tails(con, [TS], "daily")
    dde_w = batch_load_dde_tails(con, [TS], "weekly")
    last_td = daily[TS]["trade_date"].max()
    state_map = _all_states_skip(con, TS, last_td, daily[TS], weekly.get(TS))

    modes, fp_cache = preflight_stock_modes_with_fps(
        TS, state_map, daily[TS], weekly.get(TS), dde_d.get(TS), dde_w.get(TS),
    )
    assert modes is not None
    assert all(m == "SKIP" for m, _ in modes.values())
    assert ("macd", "daily") in fp_cache
    assert len(fp_cache[("macd", "daily")]) == 16
    con.close()


def test_fast_skip_state_ahead_of_calc_date():
    """last_td > calc_date: tail load must not cap at calc_date."""
    con = duckdb.connect(":memory:")
    _minimal_db(con, n_daily=270)
    daily = batch_load_quote_tails(con, [TS], "daily", quote_tail_columns("daily"))
    weekly = batch_load_quote_tails(con, [TS], "weekly", quote_tail_columns("weekly"))
    dde_d = batch_load_dde_tails(con, [TS], "daily")
    dde_w = batch_load_dde_tails(con, [TS], "weekly")
    last_td = daily[TS]["trade_date"].max()
    state_map = _all_states_skip(con, TS, last_td, daily[TS], weekly.get(TS))
    modes = preflight_stock_modes(
        TS, state_map, daily[TS], weekly.get(TS), dde_d.get(TS), dde_w.get(TS),
    )
    assert modes is not None
    assert stock_can_fast_skip(modes)
    con.close()


def test_batch_quote_tails_tail_matches_slow_path():
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    tail_cols = quote_tail_columns("daily")
    tails = batch_load_quote_tails(con, [TS], "daily", tail_cols)
    slow = load_quote_groups(con, "dwd_daily_quote", "daily", tail_cols, [TS])
    slow_tail = slow[TS].tail(SIG_WINDOW).reset_index(drop=True)
    pd.testing.assert_frame_equal(tails[TS], slow_tail)
    con.close()


def test_dde_weekly_tail_matches_load_weekly_batch():
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    tails = batch_load_dde_tails(con, [TS], "weekly")
    calc = DDECalculator(con, "weekly")
    slow = calc._load_weekly_batch([TS])[TS].tail(SIG_WINDOW).reset_index(drop=True)
    pd.testing.assert_frame_equal(tails[TS], slow)
    con.close()


def test_dde_daily_tail_matches_load_daily_batch():
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    tails = batch_load_dde_tails(con, [TS], "daily")
    calc = DDECalculator(con, "daily")
    slow = calc._load_daily_batch([TS])[TS].tail(SIG_WINDOW).reset_index(drop=True)
    pd.testing.assert_frame_equal(tails[TS], slow)
    con.close()


def test_load_daily_batch_tail_window_limits_rows():
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    calc = DDECalculator(con, "daily")
    full = calc._load_daily_batch([TS], tail_window=None)[TS]
    tailed = calc._load_daily_batch([TS], tail_window=SIG_WINDOW)[TS]
    assert len(tailed) <= SIG_WINDOW
    pd.testing.assert_frame_equal(tailed, full.tail(SIG_WINDOW).reset_index(drop=True))
    con.close()


def test_fast_skip_unsafe_on_dwd_change():
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    daily = batch_load_quote_tails(con, [TS], "daily", quote_tail_columns("daily"))
    weekly = batch_load_quote_tails(con, [TS], "weekly", quote_tail_columns("weekly"))
    dde_d = batch_load_dde_tails(con, [TS], "daily")
    dde_w = batch_load_dde_tails(con, [TS], "weekly")
    last_td = daily[TS]["trade_date"].max()
    state_map = _all_states_skip(con, TS, last_td, daily[TS], weekly.get(TS))

    # Mutate DWD → signature must change → not fast_skip
    con.execute(
        "UPDATE dwd_daily_quote SET close_qfq = close_qfq + 1 WHERE ts_code = ?",
        [TS],
    )
    daily2 = batch_load_quote_tails(con, [TS], "daily", quote_tail_columns("daily"))
    modes = preflight_stock_modes(
        TS, state_map, daily2[TS], weekly.get(TS), dde_d.get(TS), dde_w.get(TS),
    )
    assert modes is not None
    assert not stock_can_fast_skip(modes)
    con.close()


def test_fast_skip_equivalent_to_slow_path_skip():
    """After baseline calc, preflight SKIP matches per-indicator classify on pipeline frames."""
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    dates = [r[0] for r in con.execute(
        "SELECT trade_date FROM dwd_daily_quote WHERE ts_code = ? ORDER BY trade_date",
        [TS],
    ).fetchall()]
    calc_date = dates[-1]
    calc_stock_pipeline(con, TS, calc_date, daily_recalc=calc_date, weekly_recalc=None)

    state_map = load_calc_state_batch(con, [TS])
    daily = batch_load_quote_tails(con, [TS], "daily", quote_tail_columns("daily"))
    weekly = batch_load_quote_tails(con, [TS], "weekly", quote_tail_columns("weekly"))
    dde_d = batch_load_dde_tails(con, [TS], "daily")
    dde_w = batch_load_dde_tails(con, [TS], "weekly")

    modes = preflight_stock_modes(
        TS, state_map, daily.get(TS), weekly.get(TS),
        dde_d.get(TS), dde_w.get(TS),
    )
    # Only daily indicators have data; weekly/dde weekly may be empty → fallthrough
    if modes is not None:
        for (indicator_name, freq), (mode, _) in modes.items():
            spec = next(s for s in CALC_ROUTE_SPECS
                        if s[0] == indicator_name and s[1] == freq)
            _, _, _, sig_cols_i, source = spec
            if source == "quote":
                df = daily.get(TS) if freq == "daily" else weekly.get(TS)
            else:
                df = dde_d.get(TS) if freq == "daily" else dde_w.get(TS)
            if df is None or len(df) == 0:
                continue
            state = state_map.get((TS, freq, indicator_name))
            slow_mode, _ = classify_calc_mode(df, state, sig_cols_i)
            assert mode == slow_mode
    con.close()


def test_partition_preflight_modes_splits_skip_and_run():
    modes = {
        ("macd", "daily"): ("SKIP", []),
        ("ma", "daily"): ("FULL", []),
        ("dde", "weekly"): ("SKIP", []),
    }
    skip_keys, run_keys = partition_preflight_modes(modes)
    assert skip_keys == {("macd", "daily"), ("dde", "weekly")}
    assert run_keys == {("ma", "daily")}


def test_preflight_bse_empty_dde_treated_as_skip_not_fallthrough():
    """BSE empty moneyflow → per-indicator SKIP, not whole-stock None."""
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    bj = "899999.BJ"
    dates = [r[0] for r in con.execute(
        "SELECT trade_date FROM dim_date WHERE is_trade_day=1 ORDER BY trade_date LIMIT 30"
    ).fetchall()]
    rng = np.random.default_rng(7)
    close = 10.0 + np.cumsum(rng.normal(0, 0.1, len(dates)))
    _seed_daily(con, bj, dates, close)
    week_dates = dates[::5][:6]
    _seed_week_end(con, bj, week_dates, close[: len(week_dates)])

    daily = batch_load_quote_tails(con, [bj], "daily", quote_tail_columns("daily"))
    weekly = batch_load_quote_tails(con, [bj], "weekly", quote_tail_columns("weekly"))
    last_td = daily[bj]["trade_date"].max()
    state_map = {}
    for indicator_name, freq, CalcCls, sig_cols, source in CALC_ROUTE_SPECS:
        if source != "quote":
            continue
        df = daily[bj] if freq == "daily" else weekly.get(bj)
        if df is None or df.empty:
            continue
        fp = state_signature(df, last_td, sig_cols)
        spec_ver = getattr(CalcCls, "SPEC_VERSION", "v1")
        upsert_calc_state(con, bj, freq, indicator_name,
                          last_trade_date=last_td, history_fp=fp, calc_date="20260602",
                          spec_version=spec_ver)
        state_map[(bj, freq, indicator_name)] = {
            "last_trade_date": last_td,
            "history_fp": fp,
            "quote_latest_adj": None,
            "updated_calc_date": "20260602",
            "spec_version": spec_ver,
        }

    modes = preflight_stock_modes_v2(
        bj, state_map, daily.get(bj), weekly.get(bj), None, None,
    )
    assert modes is not None, "BSE must not fallthrough on empty DDE"
    assert modes[("dde", "daily")][0] == "SKIP"
    assert modes[("dde", "weekly")][0] == "SKIP"
    assert stock_can_fast_skip(modes)
    con.close()


def test_load_weekly_batch_tail_window_limits_rows():
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    calc = DDECalculator(con, "weekly")
    full = calc._load_weekly_batch([TS], tail_window=None)[TS]
    tailed = calc._load_weekly_batch([TS], tail_window=SIG_WINDOW)[TS]
    assert len(tailed) <= SIG_WINDOW
    pd.testing.assert_frame_equal(tailed, full.tail(SIG_WINDOW).reset_index(drop=True))
    con.close()


def test_load_calc_state_batch_includes_updated_calc_date():
    con = duckdb.connect(":memory:")
    ensure_calc_state_table(con)
    upsert_calc_state(con, TS, "daily", "macd", "20260605", "abc123", "20260602")
    batch = load_calc_state_batch(con, [TS])
    assert batch[(TS, "daily", "macd")]["updated_calc_date"] == "20260602"
    assert batch[(TS, "daily", "macd")]["last_trade_date"] == "20260605"
    con.close()
