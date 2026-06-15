"""Force same-day batch shortcut must respect spec_version."""
import importlib
import json

import duckdb
import pytest

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.calc_batch_append import (
    _modes_from_state_only,
    run_batch_append_phase,
    try_force_same_day_batch_shortcut,
)
from backend.etl.calc_indicators import CALC_ROUTE_SPECS, INDICATOR_SPEC_VERSIONS
from backend.etl.calc_state import upsert_calc_state


def _seed_state(con, ts_code, calc_date, spec_overrides=None):
    """Seed all 12 routes; spec_overrides maps (indicator,freq) -> spec_version."""
    spec_overrides = spec_overrides or {}
    for indicator_name, freq, CalcCls, _, _ in CALC_ROUTE_SPECS:
        key = (indicator_name, freq)
        spec = spec_overrides.get(key, getattr(CalcCls, "SPEC_VERSION", "v1"))
        upsert_calc_state(
            con, ts_code, freq, indicator_name,
            calc_date, "fp", calc_date, spec_version=spec,
        )


def test_modes_from_state_only_requires_spec_match():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    _seed_state(con, "A.SZ", "20260608", {("ma", "daily"): "v1", ("ma", "weekly"): "v1"})
    from backend.etl.calc_state import load_calc_state_batch
    state_map = load_calc_state_batch(con, ["A.SZ"])
    modes = _modes_from_state_only("A.SZ", state_map, "20260608")
    assert modes is None
    con.close()


def test_modes_from_state_only_all_spec_ok():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    _seed_state(con, "A.SZ", "20260608")
    from backend.etl.calc_state import load_calc_state_batch
    state_map = load_calc_state_batch(con, ["A.SZ"])
    modes = _modes_from_state_only("A.SZ", state_map, "20260608")
    assert modes is not None
    assert all(m == "SKIP" for m, _ in modes.values())
    con.close()


def test_force_shortcut_spec_stale_fallthrough(monkeypatch):
    import backend.config as cfg

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
    codes = ["A.SZ"]
    _seed_state(con, "A.SZ", "20260608", {("ma", "daily"): "v1", ("ma", "weekly"): "v1"})

    shortcut = try_force_same_day_batch_shortcut(con, codes, "20260608", force=True)
    assert shortcut is not None
    assert "A.SZ" in shortcut["chunk_codes"]


def test_force_shortcut_spec_ok_skips_tails(monkeypatch):
    import backend.config as cfg

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
    for ts in codes:
        _seed_state(con, ts, "20260608")

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
