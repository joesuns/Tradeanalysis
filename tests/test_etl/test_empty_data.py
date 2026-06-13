"""Tests for empty-data handling: SkipReason, CalcResult, calc skip behavior."""
import pytest
import duckdb
from backend.etl.base import SkipReason, CalcResult


class TestCalcResult:
    """Test the CalcResult dataclass."""

    def test_empty_result(self):
        r = CalcResult()
        assert r.calculated == 0
        assert r.total_skipped == 0
        assert r.total_input == 0

    def test_all_calculated(self):
        r = CalcResult()
        r.calculated = 100
        assert r.total_skipped == 0
        assert r.total_input == 100

    def test_mixed_result(self):
        r = CalcResult()
        r.calculated = 80
        r.add_skip(SkipReason.INSUFFICIENT_ROWS, "000001.SZ", "DWD rows=15, min=27")
        r.add_skip(SkipReason.NO_DWD_DATA, "000002.SZ", "DWD returned 0 rows")
        r.add_skip(SkipReason.INSUFFICIENT_ROWS, "000003.SZ", "DWD rows=10, min=27")
        assert r.calculated == 80
        assert r.total_skipped == 3
        assert r.total_input == 83
        assert len(r.skipped[SkipReason.INSUFFICIENT_ROWS]) == 2
        assert len(r.skipped[SkipReason.NO_DWD_DATA]) == 1

    def test_delisted_skip(self):
        r = CalcResult()
        r.add_skip(SkipReason.DELISTED, "000666.SZ", "delisted=20231231, DWS exists, skip")
        assert r.total_skipped == 1
        assert SkipReason.DELISTED in r.skipped
        ts_code, detail = r.skipped[SkipReason.DELISTED][0]
        assert ts_code == "000666.SZ"
        assert "delisted" in detail

    def test_source_unavailable_skip(self):
        r = CalcResult()
        r.add_skip(SkipReason.SOURCE_UNAVAILABLE, "920001.BJ",
                   "BSE stocks have no moneyflow data from tushare")
        assert r.total_skipped == 1
        assert SkipReason.SOURCE_UNAVAILABLE in r.skipped

    def test_multiple_reasons_grouped(self):
        r = CalcResult()
        r.add_skip(SkipReason.SOURCE_UNAVAILABLE, "920001.BJ", "BSE no moneyflow")
        r.add_skip(SkipReason.SOURCE_UNAVAILABLE, "920002.BJ", "BSE no moneyflow")
        r.add_skip(SkipReason.INSUFFICIENT_ROWS, "688001.SH", "DWD rows=5, min=10")
        assert set(r.skipped.keys()) == {SkipReason.SOURCE_UNAVAILABLE, SkipReason.INSUFFICIENT_ROWS}
        assert r.total_skipped == 3


@pytest.fixture
def db():
    """In-memory DuckDB with minimal DWD schema for testing calculators."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL,
            open_qfq REAL, high_qfq REAL, low_qfq REAL,
            vol REAL, pct_chg REAL, total_mv REAL, circ_mv REAL,
            is_suspended INTEGER DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT, is_week_end INTEGER, is_trade_day INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT, is_st INTEGER DEFAULT 0, list_date TEXT, delist_date TEXT
        )
    """)
    yield con
    con.close()


class TestCalcSkipBehavior:
    """Verify that calculators return CalcResult with correct skip classification."""

    def test_macd_empty_df_returns_no_dwd_data_skip(self, db):
        """MACDCalculator with non-existent ts_code should return NO_DWD_DATA skip."""
        from backend.etl.calc_macd import MACDCalculator
        calc = MACDCalculator(db, "daily")
        result = calc.calculate(["999999.ZZ"], "20260604")
        assert result.calculated == 0
        assert result.total_skipped == 1
        assert SkipReason.NO_DWD_DATA in result.skipped
        ts_code, detail = result.skipped[SkipReason.NO_DWD_DATA][0]
        assert ts_code == "999999.ZZ"
        assert "0 rows" in detail

    def test_macd_insufficient_rows(self, db):
        """MACDCalculator with < 27 rows should return INSUFFICIENT_ROWS skip."""
        from backend.etl.calc_macd import MACDCalculator
        for i in range(10):
            db.execute(
                "INSERT INTO dwd_daily_quote VALUES (?, ?, 10.0, 10.0, 10.0, 10.0, 1000, 0, 1e8, 5e7, 0)",
                ("000001.SZ", f"202601{i:02d}"),
            )
        calc = MACDCalculator(db, "daily")
        result = calc.calculate(["000001.SZ"], "20260604")
        assert result.calculated == 0
        assert result.total_skipped == 1
        assert SkipReason.INSUFFICIENT_ROWS in result.skipped
        _, detail = result.skipped[SkipReason.INSUFFICIENT_ROWS][0]
        assert "rows=10" in detail
        assert "min=27" in detail

    def test_ma_empty_df_returns_no_dwd_data_skip(self, db):
        """MACalculator with non-existent ts_code should return NO_DWD_DATA skip."""
        from backend.etl.calc_ma import MACalculator
        calc = MACalculator(db, "daily")
        result = calc.calculate(["999999.ZZ"], "20260604")
        assert result.calculated == 0
        assert result.total_skipped == 1
        assert SkipReason.NO_DWD_DATA in result.skipped

    def test_dde_bse_stock_returns_source_unavailable(self, db):
        """DDECalculator with BSE stock should return SOURCE_UNAVAILABLE skip."""
        from backend.etl.calc_dde import DDECalculator
        # DDE needs moneyflow table, not just daily_quote
        db.execute("""
            CREATE TABLE dwd_daily_moneyflow (
                ts_code TEXT, trade_date TEXT,
                net_mf_vol REAL, net_mf_amount REAL,
                buy_lg_vol REAL, sell_lg_vol REAL,
                buy_elg_vol REAL, sell_elg_vol REAL,
                total_vol REAL, net_amount_dc REAL
            )
        """)
        calc = DDECalculator(db, "daily")
        result = calc.calculate(["920001.BJ"], "20260604")
        assert result.calculated == 0
        # With empty moneyflow table, BSE stock should get SOURCE_UNAVAILABLE
        assert SkipReason.SOURCE_UNAVAILABLE in result.skipped
        _, detail = result.skipped[SkipReason.SOURCE_UNAVAILABLE][0]
        assert "BSE" in detail or "moneyflow" in detail.lower()

    def test_pp_relaxed_min_periods_accepts_small_data(self, db):
        """PricePositionCalculator should accept 40 rows (was previously rejected at 60)."""
        from backend.etl.calc_price_position import PricePositionCalculator
        db.execute("""
            CREATE TABLE dws_price_position_daily (
                ts_code TEXT, trade_date TEXT,
                price_position_60d REAL, price_position_120d REAL, price_position_250d REAL,
                calc_date TEXT, input_fingerprint TEXT, spec_version TEXT DEFAULT 'v1',
                PRIMARY KEY (ts_code, trade_date, calc_date)
            )
        """)
        for i in range(40):
            db.execute(
                "INSERT INTO dwd_daily_quote VALUES (?, ?, ?, 10.0, 10.0, 10.0, 1000, 0, 1e8, 5e7, 0)",
                ("000001.SZ", f"202601{i:02d}", 10.0 + i * 0.1),
            )
        calc = PricePositionCalculator(db, "daily")
        result = calc.calculate(["000001.SZ"], "20260604")
        assert result.calculated == 1
        assert result.total_skipped == 0

    def test_pp_very_small_data_still_skipped(self, db):
        """PricePositionCalculator with 1 row should still be skipped (need >= 2)."""
        from backend.etl.calc_price_position import PricePositionCalculator
        db.execute(
            "INSERT INTO dwd_daily_quote VALUES ('000001.SZ', '20260601', 10.0, 10.0, 10.0, 10.0, 1000, 0, 1e8, 5e7, 0)",
        )
        calc = PricePositionCalculator(db, "daily")
        result = calc.calculate(["000001.SZ"], "20260604")
        assert result.calculated == 0
        assert result.total_skipped == 1
        assert SkipReason.INSUFFICIENT_ROWS in result.skipped

    def test_volume_empty_df_returns_no_dwd_data(self, db):
        """VolumeCalculator with non-existent ts_code should return NO_DWD_DATA skip."""
        from backend.etl.calc_volume import VolumeCalculator
        calc = VolumeCalculator(db, "daily")
        result = calc.calculate(["999999.ZZ"], "20260604")
        assert result.calculated == 0
        assert result.total_skipped == 1
        assert SkipReason.NO_DWD_DATA in result.skipped
