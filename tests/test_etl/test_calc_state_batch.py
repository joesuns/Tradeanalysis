import duckdb

from backend.db.schema import ensure_calc_state_table
from backend.etl.calc_state import (
    should_refresh_calc_state,
    upsert_calc_state_batch,
)


def test_should_refresh_false_when_fp_and_date_unchanged():
    state = {
        "last_trade_date": "20260608",
        "history_fp": "abc123",
        "updated_calc_date": "20260609",
    }
    assert should_refresh_calc_state(state, "20260609", "abc123") is False


def test_should_refresh_true_when_fp_changes():
    state = {
        "last_trade_date": "20260608",
        "history_fp": "old",
        "updated_calc_date": "20260609",
    }
    assert should_refresh_calc_state(state, "20260609", "new") is True


def test_upsert_calc_state_batch_round_trip():
    con = duckdb.connect(":memory:")
    ensure_calc_state_table(con)
    records = [
        ("000001.SZ", "daily", "macd", "20260608", "fp1", "20260609", None),
        ("000002.SZ", "daily", "macd", "20260608", "fp2", "20260609", None),
    ]
    n = upsert_calc_state_batch(con, records)
    assert n == 2
    cnt = con.execute("SELECT COUNT(*) FROM dws_calc_state").fetchone()[0]
    assert cnt == 2
    con.close()


def test_skip_batch_append_collects_state_records(monkeypatch):
    """SKIP path batches multiple indicators into one upsert_calc_state_batch call."""
    import importlib

    import pandas as pd

    import backend.config as cfg
    from backend.db.schema import create_all_tables, ensure_calc_state_table
    from backend.etl.calc_batch_append import run_batch_append_phase
    from backend.etl.calc_ma import MACalculator
    from backend.etl.calc_macd import MACDCalculator
    from backend.etl.calc_router import state_signature
    from backend.etl import calc_state as calc_state_mod

    monkeypatch.setenv("CALC_APPEND", "1")
    monkeypatch.setenv("CALC_BATCH_APPEND", "1")
    monkeypatch.setenv("CALC_SKIP_STATE_REFRESH", "1")
    importlib.reload(cfg)

    codes = ["SA.SZ", "SB.SZ"]
    calc_date = "20260609"
    last_td = "20260608"
    dates = [f"41{i:06d}" for i in range(260)] + [last_td]
    sig_cols = ["close_qfq"]
    tail_df = pd.DataFrame({"trade_date": dates[-80:], "close_qfq": [10.0] * 80})
    fp = state_signature(tail_df, last_td, sig_cols)

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    for d in dates[-5:]:
        con.execute(
            "INSERT OR IGNORE INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
            [d],
        )
    for code in codes:
        for indicator in ("macd", "ma"):
            spec = "v2" if indicator == "ma" else getattr(
                MACDCalculator if indicator == "macd" else MACalculator,
                "SPEC_VERSION",
                "v1",
            )
            con.execute("""
                INSERT INTO dws_calc_state
                    (ts_code, freq, indicator, last_trade_date, history_fp,
                     quote_latest_adj, spec_version, updated_calc_date)
                VALUES (?, 'daily', ?, ?, ?, NULL, ?, '20260608')
            """, [code, indicator, last_td, fp, spec])

    def fake_preflight(ts_code, state_map, daily_q, weekly_q, daily_dde, weekly_dde, specs=None, **kwargs):
        modes = {
            ("macd", "daily"): ("SKIP", []),
            ("ma", "daily"): ("SKIP", []),
        }
        fps = {
            ("macd", "daily"): fp,
            ("ma", "daily"): fp,
        }
        return modes, fps

    upsert_calls = []
    orig_upsert = calc_state_mod.upsert_calc_state_batch

    def spy_upsert(con, records):
        upsert_calls.append(len(records))
        return orig_upsert(con, records)

    route = [
        ("macd", "daily", MACDCalculator, ["close_qfq"], "quote"),
        ("ma", "daily", MACalculator, ["close_qfq"], "quote"),
    ]
    monkeypatch.setattr(calc_state_mod, "upsert_calc_state_batch", spy_upsert)
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.preflight_stock_modes_with_fps", fake_preflight,
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_quote_tails",
        lambda _c, ts_codes, freq, cols: {
            c: tail_df for c in ts_codes
        },
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_dde_tails",
        lambda _c, ts_codes, freq: {c: pd.DataFrame() for c in ts_codes},
    )
    monkeypatch.setattr("backend.etl.calc_indicators.CALC_ROUTE_SPECS", route)

    ctx = run_batch_append_phase(con, codes, calc_date)
    assert ctx is not None
    assert len(upsert_calls) == 1
    assert upsert_calls[0] == 4
    con.close()
