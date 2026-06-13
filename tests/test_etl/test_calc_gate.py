import duckdb
import pytest

from backend.db.schema import create_all_tables
from backend.etl.calc_gate import assert_calc_date_ready, resolve_effective_calc_date


def test_resolve_effective_calc_date_caps_to_ods_max():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
        "VALUES ('000001.SZ', '20260608', 1, 1, 1, 1, 1, 1)"
    )
    eff = resolve_effective_calc_date(con, requested="20260609")
    assert eff == "20260608"


def test_assert_calc_date_ready_raises_when_ahead_of_ods():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
        "VALUES ('000001.SZ', '20260608', 1, 1, 1, 1, 1, 1)"
    )
    with pytest.raises(ValueError, match="calc_date.*20260609.*ods_max.*20260608"):
        assert_calc_date_ready(con, "20260609", strict=True)


def test_assert_calc_date_ready_allows_when_ods_empty():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    assert_calc_date_ready(con, "20260609", strict=True)


def test_data_mutated_since_last_calc_detects_fetch_after_calc():
    import json
    import duckdb
    from backend.db.schema import create_all_tables
    from backend.etl.calc_gate import data_mutated_since_last_calc

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    comp = json.dumps({"calc_date": "20260608", "ods_max": "20260608"})
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('1', 'calc_dws', '2026-06-08T10:00:00', 't1', 'success', 1, '', ?)""",
        [comp],
    )
    assert data_mutated_since_last_calc(con, "20260608") is False
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('2', 'run_fetch', '2026-06-08T11:00:00', 't2', 'success', 1, '', '{}')"""
    )
    assert data_mutated_since_last_calc(con, "20260608") is True
    con.close()


def test_try_force_same_day_batch_shortcut_skips_tail_loads(monkeypatch):
    import importlib
    import json
    import duckdb

    import backend.config as cfg
    from backend.db.schema import create_all_tables, ensure_calc_state_table
    from backend.etl.calc_batch_append import run_batch_append_phase
    from backend.etl.calc_state import upsert_calc_state

    monkeypatch.setenv("CALC_APPEND", "1")
    monkeypatch.setenv("CALC_BATCH_APPEND", "1")
    monkeypatch.setenv("CALC_FORCE_BATCH_REUSE", "1")
    importlib.reload(cfg)

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    comp = json.dumps({"calc_date": "20260608", "ods_max": "20260608"})
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('1', 'calc_dws', '2026-06-08T10:00:00', 't1', 'success', 1, '', ?)""",
        [comp],
    )
    codes = ["A.SZ", "B.SZ"]
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS
    for ts in codes:
        for indicator_name, freq, _cls, _sig, _src in CALC_ROUTE_SPECS:
            upsert_calc_state(con, ts, freq, indicator_name, "20260608", "fp", "20260608")

    tail_called = {"n": 0}

    def boom_tails(*a, **k):
        tail_called["n"] += 1
        raise AssertionError("batch tail load should be skipped")

    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_quote_tails", boom_tails,
    )
    ctx = run_batch_append_phase(con, codes, "20260608", force=True)
    assert tail_called["n"] == 0
    assert ctx is not None
    assert ctx["chunk_codes"] == []
    assert len(ctx["stock_modes"]) == 2
    con.close()


def test_run_calc_rejects_calc_date_ahead_of_ods():
    import backend.etl.orchestrator as orch

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
        "VALUES ('000001.SZ', '20260608', 1, 1, 1, 1, 1, 1)"
    )
    with pytest.raises(ValueError, match="ods_max"):
        orch.run_calc(con, calc_date="20260609", auto_fetch=False)
