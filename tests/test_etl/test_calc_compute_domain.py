"""Compute domain indices for batch FULL."""
import pandas as pd

from backend.etl.calc_compute_domain import resolve_compute_indices


def test_resolve_compute_indices_inclusive_range():
    df = pd.DataFrame({
        "trade_date": ["20260101", "20260108", "20260115", "20260122"],
    })
    idx = resolve_compute_indices(df, "20260108", "20260122")
    assert idx == [1, 2, 3]


def test_resolve_compute_indices_none_recalc_returns_all():
    df = pd.DataFrame({"trade_date": ["20260101", "20260108"]})
    idx = resolve_compute_indices(df, None, "20260108")
    assert idx == [0, 1]


def test_resolve_compute_indices_empty_frame():
    df = pd.DataFrame({"trade_date": []})
    assert resolve_compute_indices(df, "20260108", "20260122") == []


def test_resolve_compute_indices_none_frame():
    assert resolve_compute_indices(None, "20260108", "20260122") == []
