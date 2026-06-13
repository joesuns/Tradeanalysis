"""Tests for RecalcSpec registry and resolve_recalc_bars aggregation."""
from backend.etl.recalc_spec import (
    RecalcSpec,
    _WEEKLY_WARMUP_GATE,
    collect_specs,
    resolve_recalc_bars,
    resolve_weekly_warmup_weeks,
)


def test_recalc_spec_total():
    spec = RecalcSpec(lookback=60, seed=26, event_tail=5)
    assert spec.total == 91


def test_resolve_recalc_bars_daily_current_registry():
    specs = collect_specs("daily")
    # MACD lookback=250 seed=26 event_tail=10 → total=286 dominates registry
    assert resolve_recalc_bars(specs, safety=5) == 291


def test_resolve_recalc_bars_weekly_current_registry():
    specs = collect_specs("weekly")
    assert resolve_recalc_bars(specs, safety=5) == 291


def test_resolve_recalc_bars_empty():
    assert resolve_recalc_bars([], safety=5) == 5


def test_collect_specs_includes_all_calculators():
    daily = collect_specs("daily")
    weekly = collect_specs("weekly")
    assert len(daily) == 6
    assert len(weekly) == 6


def test_warmup_from_registry():
    """warmup = max(min_rows, lookback) across daily specs → >= 250."""
    specs = collect_specs("daily")
    warmup = max(max(s.min_rows, s.lookback) for s in specs)
    assert warmup >= 250


def test_resolve_weekly_warmup_weeks_current_registry():
    """Volume pct_rank (120w) drives gate; PP 250w must not inflate fetch warmup."""
    assert resolve_weekly_warmup_weeks() == _WEEKLY_WARMUP_GATE


def test_resolve_weekly_warmup_weeks_empty_gate_specs(monkeypatch):
    """All lookbacks > gate → fallback to gate threshold, not ValueError."""

    def _high_only(_freq):
        return [RecalcSpec(lookback=250), RecalcSpec(lookback=200)]

    monkeypatch.setattr("backend.etl.recalc_spec.collect_specs", _high_only)
    assert resolve_weekly_warmup_weeks() == _WEEKLY_WARMUP_GATE
