"""Calc-R1: chunk_codes must not retain hot-path stale membership after cold merge."""
import importlib

import duckdb

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.calc_batch_append import _compute_chunk_codes, run_batch_append_phase
from backend.etl.calc_preflight_context import CalcPreflightContext


def test_compute_chunk_codes_preflight_fail_only():
    codes = ["A.SZ", "B.SZ", "C.SZ"]
    stock_modes = {
        "A.SZ": {("macd", "daily"): ("SKIP", [])},
        "B.SZ": {("macd", "daily"): ("SKIP", [])},
    }
    full_items = [("B.SZ", ("dde", "weekly"))]
    assert _compute_chunk_codes(codes, stock_modes, full_items) == ["B.SZ", "C.SZ"]


def test_compute_chunk_codes_all_skip_no_chunk():
    codes = ["A.SZ", "B.SZ"]
    stock_modes = {
        "A.SZ": {("macd", "daily"): ("SKIP", [])},
        "B.SZ": {("macd", "daily"): ("SKIP", [])},
    }
    assert _compute_chunk_codes(codes, stock_modes, []) == []


def test_hot_path_cold_merge_not_in_chunk_codes(monkeypatch):
    """PATCH cold-merged with all-SKIP must not stay in chunk_codes (7c090546 bug)."""
    quote_calls = []
    preflight_calls = []

    def patch_quote_tails(con, codes, freq, columns):
        quote_calls.append((list(codes), freq))
        return {c: None for c in codes}

    def patch_preflight(ts_code, *args, **kwargs):
        preflight_calls.append(ts_code)
        return {("macd", "daily"): ("SKIP", [])}, {("macd", "daily"): "fp_patch"}

    monkeypatch.setenv("CALC_APPEND", "1")
    monkeypatch.setenv("CALC_BATCH_APPEND", "1")
    monkeypatch.setenv("CALC_REUSE_REFRESH_CTX", "1")
    import backend.config as cfg
    importlib.reload(cfg)

    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_quote_tails",
        patch_quote_tails,
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_dde_tails",
        lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.preflight_stock_modes_with_fps",
        patch_preflight,
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.load_calc_state_batch",
        lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.upsert_calc_state_batch",
        lambda *a, **k: 0,
    )

    codes = ["KEEP.SZ", "PATCH.SZ"]
    ctx = CalcPreflightContext(
        calc_date="20260611",
        source="refresh_state",
        stale_codes=["KEEP.SZ"],
        state_map={},
        daily_tails={"KEEP.SZ": None},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={"KEEP.SZ": {("macd", "daily"): ("SKIP", [])}},
        fp_cache_by_stock={"KEEP.SZ": {("macd", "daily"): "fp_keep"}},
    )

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    result = run_batch_append_phase(con, codes, "20260611", preflight_ctx=ctx)
    assert result is not None
    assert result["preflight_source"] == "refresh"
    assert preflight_calls == ["PATCH.SZ"]
    assert result["chunk_codes"] == []
    con.close()
