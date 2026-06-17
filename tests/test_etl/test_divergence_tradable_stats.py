"""TDD: tradable enrich stats for export observability."""
from unittest.mock import patch

import pandas as pd

from backend.etl.divergence_tradable import TradableEnrichStats, TradableVerdict, enrich_tradable_columns


def test_enrich_empty_frame_returns_zero_stats():
    df, stats = enrich_tradable_columns(pd.DataFrame(), None, freq="daily")
    assert df.empty
    assert stats.freq == "daily"
    assert stats.l1_macd == 0
    assert stats.l1_dde == 0
    assert stats.tradable == 0
    assert stats.reject == 0


def test_enrich_returns_stats_counts():
    df = pd.DataFrame(
        {
            "ts_code": ["A.SZ", "B.SZ"],
            "trade_date": ["20260115", "20260115"],
            "macd_divergence": ["top_divergence", "bottom_divergence"],
            "dde_divergence": [None, "top_divergence"],
        }
    )

    def _verdict(con, ts_code, freq, indicator, trade_date, l1_label):
        if indicator == "macd" and l1_label == "top_divergence":
            return TradableVerdict(l1_label, l1_label, None, "direct", 0, trade_date)
        if indicator == "macd":
            return TradableVerdict(l1_label, None, "skip_peak", "skip_peak", 0, trade_date)
        return TradableVerdict(l1_label, None, "zone_mismatch", "direct", 0, trade_date)

    with patch(
        "backend.etl.divergence_tradable._verdict_for_l1_row",
        side_effect=_verdict,
    ):
        out, stats = enrich_tradable_columns(df, None, freq="daily")

    assert out.loc[0, "macd_divergence_tradable"] == "top_divergence"
    assert out.loc[1, "macd_divergence_reject"] == "skip_peak"
    assert stats.l1_macd == 2
    assert stats.l1_dde == 1
    assert stats.tradable == 1
    assert stats.reject == 2
    assert stats.reject_skip_peak == 1
    assert stats.reject_zone_mismatch == 1


def test_enrich_stats_to_dict():
    stats = TradableEnrichStats(
        freq="weekly",
        l1_macd=3,
        l1_dde=1,
        tradable=2,
        reject=2,
        reject_skip_peak=1,
        reject_tg_lag=0,
        reject_zone_mismatch=1,
        elapsed_sec=1.5,
    )
    d = stats.to_dict()
    assert d["freq"] == "weekly"
    assert d["l1_macd"] == 3
    assert d["elapsed_sec"] == 1.5
