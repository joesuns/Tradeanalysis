"""Tests for tradable divergence filtering in combo_eval."""
import os
import tempfile

import duckdb
import pytest


@pytest.fixture
def temp_tradable_combo_db():
    """K-pattern + MACD L1 bottom divergence for tradable filter tests."""
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    con = duckdb.connect(path)

    con.execute(
        """
        CREATE TABLE dws_kpattern_daily (
            ts_code TEXT, trade_date TEXT, calc_date TEXT,
            yang_bao_yin INTEGER, yang_ke_yin INTEGER,
            mu_bei_xian INTEGER, bi_lei_zhen INTEGER,
            gao_kai_chang_yin INTEGER, yin_bao_yang INTEGER,
            yin_ke_yang INTEGER, strength REAL,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
        """
    )
    con.execute(
        """
        CREATE VIEW v_dws_kpattern_daily_latest AS
        SELECT * FROM dws_kpattern_daily d WHERE calc_date = (
            SELECT MAX(calc_date) FROM dws_kpattern_daily
            WHERE ts_code = d.ts_code AND trade_date = d.trade_date
        )
        """
    )
    con.execute(
        """
        CREATE TABLE dws_macd_daily (
            ts_code TEXT, trade_date TEXT, calc_date TEXT,
            ema_12 REAL, ema_26 REAL, dif REAL, dea REAL, macd_bar REAL,
            divergence TEXT, zone TEXT, turning_point TEXT, alert TEXT,
            trend TEXT, trend_strength REAL,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
        """
    )
    con.execute(
        """
        CREATE VIEW v_dws_macd_daily_latest AS
        SELECT * FROM dws_macd_daily d WHERE calc_date = (
            SELECT MAX(calc_date) FROM dws_macd_daily
            WHERE ts_code = d.ts_code AND trade_date = d.trade_date
        )
        """
    )

    con.execute(
        "INSERT INTO dws_kpattern_daily VALUES "
        "('TEST.SZ', '20260115', '20260101', 0, 1, 0, 0, 0, 0, 0, 0.8)"
    )
    con.execute(
        "INSERT INTO dws_macd_daily VALUES "
        "('TEST.SZ', '20260115', '20260101', 1.0, 0.5, 0.5, 0.3, -0.4, "
        "'bottom_divergence', 'bear', NULL, NULL, 'up', 0.1)"
    )

    con.close()
    yield path
    os.unlink(path)
    wal = path + ".wal"
    if os.path.exists(wal):
        os.unlink(wal)


def test_find_combo_use_tradable_excludes_rejected_l1(temp_tradable_combo_db):
    from unittest.mock import patch

    from backend.backtest.combo_eval import find_combo_signals

    def _reject_tradable(df, con, freq="daily"):
        from backend.etl.divergence_tradable import TradableEnrichStats
        out = df.copy()
        out["macd_divergence_tradable"] = None
        out["macd_divergence_reject"] = "skip_peak"
        return out, TradableEnrichStats(freq=freq)

    with patch(
        "backend.etl.divergence_tradable.enrich_tradable_columns",
        side_effect=_reject_tradable,
    ):
        tradable = find_combo_signals(
            temp_tradable_combo_db,
            "20260115",
            patterns=["yang_ke_yin"],
            macd_divergence="bottom_divergence",
            use_tradable=True,
        )
        assert tradable == []

        l1_only = find_combo_signals(
            temp_tradable_combo_db,
            "20260115",
            patterns=["yang_ke_yin"],
            macd_divergence="bottom_divergence",
            use_tradable=False,
        )
        assert len(l1_only) == 1
        assert l1_only[0]["ts_code"] == "TEST.SZ"


def test_find_combo_use_tradable_accepts_passing_label(temp_tradable_combo_db):
    from unittest.mock import patch

    from backend.backtest.combo_eval import find_combo_signals

    def _accept_tradable(df, con, freq="daily"):
        from backend.etl.divergence_tradable import TradableEnrichStats
        out = df.copy()
        out["macd_divergence_tradable"] = "bottom_divergence"
        out["macd_divergence_reject"] = None
        return out, TradableEnrichStats(freq=freq, tradable=1)

    with patch(
        "backend.etl.divergence_tradable.enrich_tradable_columns",
        side_effect=_accept_tradable,
    ):
        signals = find_combo_signals(
            temp_tradable_combo_db,
            "20260115",
            patterns=["yang_ke_yin"],
            macd_divergence="bottom_divergence",
            use_tradable=True,
        )
        assert len(signals) == 1
        assert signals[0]["ts_code"] == "TEST.SZ"
