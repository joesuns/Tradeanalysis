"""Export wide tradable divergence column tests."""
from pathlib import Path

import duckdb
import pytest

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "tradeanalysis.duckdb"


def test_col_names_include_structure_and_tradable():
    from backend.export_wide import _COL_NAMES

    assert _COL_NAMES["macd_divergence"] == "MACD结构背离"
    assert _COL_NAMES["macd_divergence_tradable"] == "MACD可交易背离"
    assert _COL_NAMES["dde_divergence"] == "DDE结构背离"
    assert _COL_NAMES["dde_divergence_tradable"] == "DDE可交易背离"
    assert _COL_NAMES["macd_divergence_reject"] == "MACD背离剔除"
    assert _COL_NAMES["dde_divergence_reject"] == "DDE背离剔除"


def test_event_signal_cols_include_tradable():
    from backend.export_wide import _EVENT_SIGNAL_COLS

    assert "macd_divergence_tradable" in _EVENT_SIGNAL_COLS
    assert "dde_divergence_tradable" in _EVENT_SIGNAL_COLS
    assert "macd_divergence_reject" in _EVENT_SIGNAL_COLS


def test_enrich_caches_verdict_per_ts_code_indicator():
    from unittest.mock import patch

    import pandas as pd

    from backend.etl.divergence_tradable import TradableEnrichStats, TradableVerdict, enrich_tradable_columns

    df = pd.DataFrame(
        {
            "ts_code": ["A.SZ", "A.SZ"],
            "trade_date": ["20260115", "20260115"],
            "macd_divergence": ["top_divergence", "top_divergence"],
            "dde_divergence": ["top_divergence", "top_divergence"],
        }
    )
    calls = {"n": 0}

    def _counting_verdict(con, ts_code, freq, indicator, trade_date, l1_label):
        calls["n"] += 1
        return TradableVerdict(l1_label, None, "skip_peak", "skip_peak", 0, trade_date)

    with patch(
        "backend.etl.divergence_tradable._verdict_for_l1_row",
        side_effect=_counting_verdict,
    ):
        out, _stats = enrich_tradable_columns(df, None, freq="daily")
    assert calls["n"] == 2
    assert out.loc[0, "macd_divergence_reject"] == "skip_peak"
    assert out.loc[0, "dde_divergence_reject"] == "skip_peak"


@pytest.mark.integration
@pytest.mark.skipif(not DB_PATH.exists(), reason="requires local duckdb")
def test_enrich_601518_weekly_tradable_empty_on_skip_peak():
    from backend.etl.divergence_tradable import enrich_tradable_columns

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        df = con.execute(
            """
            SELECT ts_code, trade_date, divergence AS macd_divergence
            FROM v_dws_macd_weekly_latest
            WHERE ts_code = '601518.SH' AND trade_date = '20260612'
            """
        ).df()
        assert len(df) == 1
        enriched, _stats = enrich_tradable_columns(df, con, freq="weekly")
        row = enriched.iloc[0]
        assert row["macd_divergence"] == "top_divergence"
        assert row["macd_divergence_tradable"] is None
        assert row["macd_divergence_reject"] == "skip_peak"
    finally:
        con.close()


@pytest.mark.integration
@pytest.mark.skipif(not DB_PATH.exists(), reason="requires local duckdb")
def test_enrich_601518_daily_reject_skip_peak_not_zone():
    from backend.etl.divergence_tradable import enrich_tradable_columns

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        df = con.execute(
            """
            SELECT ts_code, trade_date, divergence AS macd_divergence
            FROM v_dws_macd_daily_latest
            WHERE ts_code = '601518.SH' AND trade_date = '20240927'
            """
        ).df()
        assert len(df) == 1
        enriched, _stats = enrich_tradable_columns(df, con, freq="daily")
        row = enriched.iloc[0]
        assert row["macd_divergence"] == "top_divergence"
        assert row["macd_divergence_tradable"] is None
        assert row["macd_divergence_reject"] == "skip_peak"
    finally:
        con.close()
