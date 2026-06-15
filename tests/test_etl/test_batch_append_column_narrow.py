"""Tests for run-path indicator filter in batch append (Wave 5 M3)."""
from unittest.mock import MagicMock

import duckdb
import pytest

from backend.db.schema import create_all_tables


def _patch_batch_deps(monkeypatch, preflight_modes=None):
    from backend.etl import calc_batch_append as ba

    if preflight_modes is None:
        def preflight_modes(ts_code, state_map, daily_q, weekly_q, daily_dde, weekly_dde,
                            specs=None, con=None, dwd_fp_cache=None):
            modes = {}
            for ind, freq, *_ in specs or []:
                modes[(ind, freq)] = ("FULL", [])
            return modes, {}
        preflight_modes_fn = preflight_modes
    else:
        preflight_modes_fn = preflight_modes

    quote_tails_called = {"daily": False, "weekly": False}
    dde_tails_called = {"daily": False, "weekly": False}

    def fake_quote_tails(con, codes, freq, cols):
        quote_tails_called[freq] = True
        return {c: MagicMock() for c in codes}

    def fake_dde_tails(con, codes, freq):
        dde_tails_called[freq] = True
        return {c: MagicMock() for c in codes}

    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_quote_tails", fake_quote_tails,
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_dde_tails", fake_dde_tails,
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.preflight_stock_modes_with_fps",
        preflight_modes_fn,
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.load_calc_state_batch", lambda con, codes: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_dwd_fp_gate.build_dwd_fp_cache",
        lambda con, codes, calc_date: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.build_skip_state_records", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.upsert_calc_state_batch", lambda *a, **k: None,
    )
    for ind in list(ba.BATCH_APPEND_FNS.keys()):
        monkeypatch.setitem(
            ba.BATCH_APPEND_FNS, ind,
            lambda *a, **k: (MagicMock(calculated=0), {}),
        )
    return ba, quote_tails_called, dde_tails_called


def test_run_batch_append_phase_respects_indicator_filter(monkeypatch):
    ba, quote_tails_called, dde_tails_called = _patch_batch_deps(monkeypatch)
    seen_route_keys = []

    def spy_full(con, calc_date, full_groups, batch_ctx):
        seen_route_keys.extend(full_groups.keys())
        return {
            "completed_keys": set(),
            "batch_full_items": 0,
            "full_by_indicator": {},
            "agg_by_key": {},
        }

    monkeypatch.setattr(ba, "run_batch_full_phase", spy_full)
    monkeypatch.setattr(ba, "try_force_same_day_batch_shortcut", lambda *a, **k: None)

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    codes = ["000001.SZ", "000002.SZ"]
    result = ba.run_batch_append_phase(
        con, codes, "20260612", indicator_filter=["dde"],
    )
    assert result is not None
    assert all(k[0] == "dde" for k in seen_route_keys)
    assert not any(k[0] == "macd" for k in seen_route_keys)
    assert quote_tails_called == {"daily": False, "weekly": False}
    assert dde_tails_called == {"daily": True, "weekly": True}
    assert result["indicator_filter"] == ["dde"]
    assert result["active_routes"] == ["dde_daily", "dde_weekly"]
    con.close()


def test_run_batch_append_skips_force_shortcut_when_narrowed(monkeypatch):
    ba, _, _ = _patch_batch_deps(
        monkeypatch,
        preflight_modes=lambda *a, **k: ({}, {}),
    )
    called = {"shortcut": False}

    def fake_shortcut(*args, **kwargs):
        called["shortcut"] = True
        return {
            "chunk_codes": [],
            "completed_keys": set(),
            "agg_by_key": {},
            "stock_modes": {},
            "state_map": {},
            "daily_tails": {},
            "weekly_tails": {},
            "dde_daily": {},
            "dde_weekly": {},
            "full_items": [],
            "chunk_work_items": 0,
            "batch_full_items": 0,
            "full_by_indicator": {},
        }

    monkeypatch.setattr(ba, "try_force_same_day_batch_shortcut", fake_shortcut)

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ba.run_batch_append_phase(
        con, ["000001.SZ"], "20260612", indicator_filter=["dde"],
    )
    assert called["shortcut"] is False
    con.close()
