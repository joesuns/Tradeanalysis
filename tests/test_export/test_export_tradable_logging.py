"""TDD: export tradable enrich progress logging."""
import logging

from backend.etl.divergence_tradable import TradableEnrichStats
from backend.export_wide import (
    build_export_data_completeness,
    format_tradable_enrich_log,
)


def test_format_tradable_enrich_log_daily():
    stats = TradableEnrichStats(
        freq="daily",
        l1_macd=12,
        l1_dde=3,
        tradable=5,
        reject=10,
        elapsed_sec=8.2,
    )
    msg = format_tradable_enrich_log(stats)
    assert msg.startswith("progress export: tradable enrich daily |")
    assert "l1_macd=12" in msg
    assert "l1_dde=3" in msg
    assert "tradable=5" in msg
    assert "reject=10" in msg
    assert "8.2s" in msg


def test_build_export_data_completeness_includes_tradable_enrich():
    daily = TradableEnrichStats(freq="daily", l1_macd=1, elapsed_sec=0.5).to_dict()
    weekly = TradableEnrichStats(freq="weekly", l1_macd=2, elapsed_sec=0.3).to_dict()
    dc = build_export_data_completeness(
        "20260612",
        {"daily": daily, "weekly": weekly},
    )
    assert dc["analysis_date"] == "20260612"
    assert dc["tradable_enrich"]["daily"]["l1_macd"] == 1
    assert dc["tradable_enrich"]["weekly"]["l1_macd"] == 2


def test_export_wide_logs_tradable_enrich(caplog):
    from backend.export_wide import log_tradable_enrich_progress

    stats = TradableEnrichStats(
        freq="daily", l1_macd=1, l1_dde=0, tradable=0, reject=1, elapsed_sec=0.1,
    )
    with caplog.at_level(logging.INFO, logger="backend.export_wide"):
        log_tradable_enrich_progress(stats)
    assert any("progress export: tradable enrich daily" in r.message for r in caplog.records)
