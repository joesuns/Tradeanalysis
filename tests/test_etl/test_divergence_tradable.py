"""Tradable divergence consumer layer (L2) tests."""
import csv
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tradable_divergence_cases.csv"
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "tradeanalysis.duckdb"


def load_tradable_cases():
    with open(FIXTURE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_classify_rejects_skip_peak():
    from backend.etl.divergence_structure import StructureEvent
    from backend.etl.divergence_tradable import classify_tradable

    ev = StructureEvent(0, "top_divergence", "skip_peak", tg_lag_bars=0, zone_ok=True)
    out = classify_tradable(ev)
    assert out.tradable_label is None
    assert out.reject_reason == "skip_peak"


def test_classify_rejects_tg_lag():
    from backend.etl.divergence_structure import StructureEvent
    from backend.etl.divergence_tradable import classify_tradable

    ev = StructureEvent(0, "top_divergence", "direct", tg_lag_bars=2, zone_ok=True)
    out = classify_tradable(ev)
    assert out.tradable_label is None
    assert out.reject_reason == "tg_lag"


def test_classify_rejects_zone_mismatch():
    from backend.etl.divergence_structure import StructureEvent
    from backend.etl.divergence_tradable import classify_tradable

    ev = StructureEvent(0, "bottom_divergence", "direct", tg_lag_bars=0, zone_ok=False)
    out = classify_tradable(ev)
    assert out.tradable_label is None
    assert out.reject_reason == "zone_mismatch"


def test_classify_accepts_direct_fresh_zone_ok():
    from backend.etl.divergence_structure import StructureEvent
    from backend.etl.divergence_tradable import classify_tradable

    ev = StructureEvent(0, "top_divergence", "direct", tg_lag_bars=1, zone_ok=True)
    out = classify_tradable(ev)
    assert out.tradable_label == "top_divergence"
    assert out.reject_reason is None


def test_macd_trace_events_align_with_l1_on_synthetic():
    from backend.etl.divergence_structure import (
        compute_macd_structure_divergence,
        trace_macd_structure_events,
    )
    from tests.test_etl.test_divergence_structure import _synthetic_macd_top_scenario

    close, dif, dea, macd = _synthetic_macd_top_scenario()
    l1 = compute_macd_structure_divergence(close, dif, dea, macd, dedup=10)
    events = trace_macd_structure_events(close, dif, dea, macd, dedup=10)
    tg_idx = [i for i, v in enumerate(l1) if v == "top_divergence"]
    assert tg_idx
    ev_by_idx = {e.index: e for e in events}
    for idx in tg_idx:
        assert idx in ev_by_idx
        assert ev_by_idx[idx].l1_label == l1[idx]


def test_dde_trace_events_align_with_l1_on_synthetic():
    from backend.etl.divergence_structure import (
        compute_dde_structure_divergence,
        trace_dde_structure_events,
    )
    from tests.test_etl.test_divergence_structure import _synthetic_macd_top_scenario

    close, _, _, _ = _synthetic_macd_top_scenario()
    n = len(close)
    ddx = pd.Series([0.1] * n)
    ddx2 = ddx.ewm(span=5, adjust=False).mean().values
    l1 = compute_dde_structure_divergence(close, ddx.values, ddx2, dedup=10)
    events = trace_dde_structure_events(close, ddx.values, ddx2, dedup=10)
    for ev in events:
        assert l1[ev.index] == ev.l1_label


def test_verdict_ignores_trace_event_with_mismatched_l1():
    from backend.etl.divergence_structure import StructureEvent
    from backend.etl.divergence_tradable import _verdict_for_l1_row

    frame = pd.DataFrame(
        {
            "trade_date": ["20260115"],
            "close_qfq": [10.0],
            "dif": [0.5],
            "dea": [0.3],
            "macd_bar": [0.4],
        }
    )
    wrong_ev = StructureEvent(
        0, "bottom_divergence", "direct", tg_lag_bars=0, zone_ok=True, trade_date="20260115",
    )
    with patch("backend.etl.divergence_tradable._load_trace_frame", return_value=frame):
        with patch(
            "backend.etl.divergence_tradable._trace_events_for_indicator",
            return_value=[wrong_ev],
        ):
            verdict = _verdict_for_l1_row(
                None, "TEST.SZ", "daily", "macd", "20260115", "top_divergence",
            )
    assert verdict.l1_label == "top_divergence"
    assert verdict.tradable_label is None
    assert verdict.reject_reason == "skip_peak"


@pytest.mark.integration
@pytest.mark.skipif(not DB_PATH.exists(), reason="requires local duckdb")
@pytest.mark.parametrize(
    "row",
    load_tradable_cases(),
    ids=lambda r: f"{r['ts_code']}_{r['trade_date']}_{r['indicator']}_{r['freq']}",
)
def test_tradable_cases_match_fixture(row):
    from backend.etl.divergence_tradable import evaluate_tradable_for_case

    verdict = evaluate_tradable_for_case(
        str(DB_PATH),
        row["ts_code"],
        row["trade_date"],
        row["freq"],
        row["indicator"],
    )
    assert verdict.l1_label == row["l1_expected"]
    if row["tradable_expected"]:
        assert verdict.tradable_label == row["tradable_expected"]
    else:
        assert verdict.tradable_label is None
        assert verdict.reject_reason == row["reject_reason"]
