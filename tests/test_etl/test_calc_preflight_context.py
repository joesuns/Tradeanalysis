"""Tests for CalcPreflightContext run-to-calc handoff."""
import pytest

from backend.etl.calc_preflight_context import (
    CalcPreflightContext,
    pop_run_preflight_context,
    set_run_preflight_context,
    slice_context_for_codes,
)


@pytest.fixture(autouse=True)
def _clear_run_ctx():
    pop_run_preflight_context()
    yield
    pop_run_preflight_context()


def test_pop_returns_none_when_unset():
    assert pop_run_preflight_context() is None


def test_set_and_pop_roundtrip():
    ctx = CalcPreflightContext(
        calc_date="20260611",
        source="refresh_state",
        stale_codes=["000001.SZ"],
        state_map={},
        daily_tails={"000001.SZ": None},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={"000001.SZ": {("macd", "daily"): ("APPEND", ["20260611"])}},
        fp_cache_by_stock={"000001.SZ": {("macd", "daily"): "abc123"}},
        refresh_summary={"keys_updated": 1},
    )
    set_run_preflight_context(ctx)
    got = pop_run_preflight_context()
    assert got is ctx
    assert pop_run_preflight_context() is None


def test_slice_for_calc_codes():
    ctx = CalcPreflightContext(
        calc_date="20260611",
        source="refresh_state",
        stale_codes=["A.SZ", "B.SZ"],
        state_map={
            ("A.SZ", "daily", "macd"): {"last_trade_date": "20260610"},
            ("B.SZ", "daily", "macd"): {"last_trade_date": "20260610"},
        },
        daily_tails={"A.SZ": 1, "B.SZ": 2},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={
            "A.SZ": {("macd", "daily"): ("APPEND", ["20260611"])},
            "B.SZ": {("macd", "daily"): ("APPEND", ["20260611"])},
        },
        fp_cache_by_stock={
            "A.SZ": {("macd", "daily"): "fp_a"},
            "B.SZ": {("macd", "daily"): "fp_b"},
        },
        refresh_summary={},
    )
    sliced = slice_context_for_codes(ctx, ["A.SZ"])
    assert list(sliced.daily_tails.keys()) == ["A.SZ"]
    assert "B.SZ" not in sliced.stock_modes
    assert ("A.SZ", "daily", "macd") in sliced.state_map


def test_merge_context_patch_overwrites_patch_codes_only():
    from backend.etl.calc_preflight_context import merge_context_patch

    base = CalcPreflightContext(
        calc_date="20260612",
        source="refresh_state",
        stale_codes=["A.SZ", "B.SZ"],
        state_map={},
        daily_tails={"A.SZ": "old_a", "B.SZ": "old_b"},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={
            "A.SZ": {("macd", "daily"): ("SKIP", [])},
            "B.SZ": {("macd", "daily"): ("SKIP", [])},
        },
        fp_cache_by_stock={"A.SZ": {("macd", "daily"): "fp_a"}},
    )
    patch_bundle = {
        "daily_tails": {"A.SZ": "new_a"},
        "weekly_tails": {"A.SZ": "w_a"},
        "dde_daily": {},
        "dde_weekly": {},
        "stock_modes": {"A.SZ": {("macd", "daily"): ("APPEND", ["20260612"])}},
        "fp_cache_by_stock": {"A.SZ": {("macd", "daily"): "fp_new"}},
        "state_map": {("A.SZ", "macd", "daily"): {"last_trade_date": "20260612"}},
    }
    merged = merge_context_patch(base, ["A.SZ"], patch_bundle, calc_date="20260612")
    assert merged.daily_tails["A.SZ"] == "new_a"
    assert merged.daily_tails["B.SZ"] == "old_b"
    assert merged.stock_modes["A.SZ"][("macd", "daily")][0] == "APPEND"
    assert merged.stock_modes["B.SZ"][("macd", "daily")][0] == "SKIP"
    assert merged.source == "refresh_state"
    assert merged.calc_date == "20260612"
    assert merged.fp_cache_by_stock["A.SZ"][("macd", "daily")] == "fp_new"


def test_merge_context_patch_none_ctx_builds_fresh():
    from backend.etl.calc_preflight_context import merge_context_patch

    patch_bundle = {
        "daily_tails": {"P.SZ": "tail"},
        "weekly_tails": {},
        "dde_daily": {},
        "dde_weekly": {},
        "stock_modes": {"P.SZ": {("macd", "daily"): ("APPEND", ["20260612"])}},
        "fp_cache_by_stock": {"P.SZ": {("macd", "daily"): "fp_p"}},
        "state_map": {},
    }
    merged = merge_context_patch(None, ["P.SZ"], patch_bundle, calc_date="20260612")
    assert merged.calc_date == "20260612"
    assert merged.daily_tails["P.SZ"] == "tail"
    assert merged.source == "refresh_state"
