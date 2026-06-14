"""Calc-R2: DWS fingerprint gate downgrades spurious FULL to SKIP."""
import importlib

import duckdb
import pandas as pd

from backend.etl.calc_dde import DDECalculator
from backend.etl.calc_dwd_fp_gate import apply_dwd_fp_gate, build_dwd_fp_cache
from backend.etl.calc_router import classify_calc_mode_detail, state_signature
from backend.etl.base import compute_input_fingerprint


def _make_dde_tail():
    rows = []
    for i in range(5):
        rows.append({
            "trade_date": f"2026060{i + 1}",
            "buy_lg_vol": 100.0 + i,
            "sell_lg_vol": 90.0,
            "buy_elg_vol": 50.0,
            "sell_elg_vol": 40.0,
            "total_vol": 1000.0,
            "net_mf_amount": 10.0,
            "net_amount_dc": 5.0,
            "circ_mv": 1e6,
            "close_qfq": 10.0 + i * 0.1,
        })
    return pd.DataFrame(rows)


def test_apply_dwd_fp_gate_downgrades_stale_history_fp():
    os_env = __import__("os").environ
    os_env["CALC_DWD_FP_GATE"] = "1"
    import backend.config as cfg
    importlib.reload(cfg)

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_dde_weekly (
            ts_code VARCHAR, trade_date VARCHAR, calc_date VARCHAR,
            input_fingerprint VARCHAR, spec_version VARCHAR,
            ddx DOUBLE, ddx2 DOUBLE
        )
    """)
    df = _make_dde_tail()
    recalc_start = "20260601"
    input_fp = compute_input_fingerprint(df, recalc_start=recalc_start)
    con.execute(
        "INSERT INTO dws_dde_weekly VALUES (?, ?, ?, ?, ?, ?, ?)",
        ["T.SZ", "20260605", "20260610", input_fp, "v2", 0.1, 0.2],
    )

    stale_fp = "deadbeef00000000"
    state = {"last_trade_date": "20260605", "history_fp": stale_fp, "spec_version": "v2"}
    mode, new_bars, cur_fp = classify_calc_mode_detail(
        df, state, DDECalculator.SIGNATURE_COLS, expected_spec_version="v2",
    )
    assert mode == "FULL"
    assert not new_bars

    cache = {
        ("dde", "weekly"): {
            "recalc_start": recalc_start,
            "latest_fps": {"T.SZ": input_fp},
            "spec_version": "v2",
            "latest_specs": {"T.SZ": "v2"},
        },
    }
    mode2, new_bars2, _ = apply_dwd_fp_gate(
        mode, new_bars, cur_fp,
        con=con, ts_code="T.SZ", CalcCls=DDECalculator, freq="weekly", df=df,
        dwd_fp_cache=cache, indicator_name="dde",
    )
    assert mode2 == "SKIP"
    assert new_bars2 == []
    con.close()


def test_apply_dwd_fp_gate_keeps_full_when_dws_changed():
    os_env = __import__("os").environ
    os_env["CALC_DWD_FP_GATE"] = "1"
    import backend.config as cfg
    importlib.reload(cfg)

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_dde_weekly (
            ts_code VARCHAR, trade_date VARCHAR, calc_date VARCHAR,
            input_fingerprint VARCHAR, spec_version VARCHAR,
            ddx DOUBLE, ddx2 DOUBLE
        )
    """)
    df = _make_dde_tail()
    con.execute(
        "INSERT INTO dws_dde_weekly VALUES (?, ?, ?, ?, ?, ?, ?)",
        ["T.SZ", "20260605", "20260610", "old_fp_mismatch", "v2", 0.1, 0.2],
    )

    state = {
        "last_trade_date": "20260605",
        "history_fp": "deadbeef00000000",
        "spec_version": "v2",
    }
    mode, new_bars, cur_fp = classify_calc_mode_detail(
        df, state, DDECalculator.SIGNATURE_COLS, expected_spec_version="v2",
    )
    cache = {
        ("dde", "weekly"): {
            "recalc_start": "20260601",
            "latest_fps": {"T.SZ": "old_fp_mismatch"},
            "spec_version": "v2",
            "latest_specs": {"T.SZ": "v2"},
        },
    }
    mode2, _, _ = apply_dwd_fp_gate(
        mode, new_bars, cur_fp,
        con=con, ts_code="T.SZ", CalcCls=DDECalculator, freq="weekly", df=df,
        dwd_fp_cache=cache, indicator_name="dde",
    )
    assert mode2 == "FULL"
    con.close()


def test_apply_dwd_fp_gate_never_downgrades_append():
    os_env = __import__("os").environ
    os_env["CALC_DWD_FP_GATE"] = "1"
    import backend.config as cfg
    importlib.reload(cfg)

    df = _make_dde_tail()
    df = pd.concat([df, pd.DataFrame([{
        "trade_date": "20260606",
        "buy_lg_vol": 105.0, "sell_lg_vol": 91.0, "buy_elg_vol": 51.0,
        "sell_elg_vol": 41.0, "total_vol": 1001.0, "net_mf_amount": 11.0,
        "net_amount_dc": 6.0, "circ_mv": 1e6, "close_qfq": 10.6,
    }])], ignore_index=True)
    last_td = "20260605"
    hist_fp = state_signature(df[df["trade_date"] <= last_td].tail(245), last_td, DDECalculator.SIGNATURE_COLS)
    state = {"last_trade_date": last_td, "history_fp": hist_fp, "spec_version": "v2"}
    mode, new_bars, cur_fp = classify_calc_mode_detail(
        df, state, DDECalculator.SIGNATURE_COLS, expected_spec_version="v2",
    )
    assert mode == "APPEND"
    assert new_bars
    mode2, new_bars2, _ = apply_dwd_fp_gate(
        mode, new_bars, cur_fp,
        con=None, ts_code="T.SZ", CalcCls=DDECalculator, freq="weekly", df=df,
        dwd_fp_cache={}, indicator_name="dde",
    )
    assert mode2 == "APPEND"
    assert new_bars2 == new_bars


def test_batch_full_fingerprint_match_aligns_history_fp(monkeypatch):
    """M2B: batch_full skip path must upsert aligned history_fp when DWS unchanged."""
    import duckdb

    from backend.etl.base import compute_input_fingerprint
    from backend.etl.calc_batch_append import _batch_full_loop
    from backend.etl.calc_ma import MACalculator
    from backend.etl.calc_router import state_signature

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_ma_daily (
            ts_code VARCHAR, trade_date VARCHAR, calc_date VARCHAR,
            input_fingerprint VARCHAR, spec_version VARCHAR,
            ma5 DOUBLE, ma10 DOUBLE, bias_rate DOUBLE, slope_pct DOUBLE,
            alignment VARCHAR, near_cross_days INTEGER
        )
    """)
    rows = []
    for i in range(15):
        rows.append({
            "trade_date": f"2026060{i + 1}" if i < 9 else f"202606{i + 1}",
            "close_qfq": 10.0 + i * 0.1,
        })
    df = pd.DataFrame(rows)
    recalc_start = str(df.iloc[0]["trade_date"])
    last_td = str(df.iloc[-1]["trade_date"])
    input_fp = compute_input_fingerprint(df, recalc_start=recalc_start)
    con.execute(
        "INSERT INTO dws_ma_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["T.SZ", last_td, "20260611", input_fp, "v2", 10.0, 10.0, 0.0, 0.0, "sideways", 0],
    )

    stale_fp = "deadbeef00000000"
    state_map = {
        ("T.SZ", "daily", "ma"): {
            "last_trade_date": last_td,
            "history_fp": stale_fp,
            "spec_version": "v2",
        },
    }
    upsert_calls = []

    def capture_upsert(_con, records):
        upsert_calls.append(list(records))
        return len(records)

    monkeypatch.setattr(
        "backend.etl.calc_state.upsert_calc_state_batch", capture_upsert,
    )

    calc = MACalculator(con, "daily")

    def fail_compute(_c, _ts_code, _df):
        raise AssertionError("compute_fn must not run on fingerprint_match skip")

    agg, stock_rows = _batch_full_loop(
        calc, ["T.SZ"], "20260612", recalc_start, {"T.SZ": df},
        fail_compute,
        MACalculator.DWS_COLS, MACalculator.FLOAT_COLS,
        "均线日线", min_rows=11,
        spec_version=MACalculator.SPEC_VERSION, check_spec=True,
        latest_fps={"T.SZ": input_fp},
        latest_specs={"T.SZ": "v2"},
        state_map=state_map,
        indicator_name="ma",
        sig_cols=MACalculator.SIGNATURE_COLS,
    )

    assert agg.calculated == 0
    assert stock_rows == []
    assert len(upsert_calls) == 1
    expected_fp = state_signature(df, last_td, MACalculator.SIGNATURE_COLS)
    rec = upsert_calls[0][0]
    assert rec[0] == "T.SZ"
    assert rec[1] == "daily"
    assert rec[2] == "ma"
    assert rec[3] == last_td
    assert rec[4] == expected_fp
    assert rec[4] != stale_fp
    con.close()
