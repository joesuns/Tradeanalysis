"""Tests for DuckDB schema: DDL execution + CHECK constraints."""
import pytest


class TestSchemaCreation:
    """Verify all tables, views, and indexes are created."""

    def test_all_tables_created(self, db_with_schema):
        tables = {r[0] for r in db_with_schema.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        # ODS (7)
        assert "ods_stock_basic" in tables
        assert "ods_daily" in tables
        assert "ods_daily_basic" in tables
        assert "ods_moneyflow" in tables
        assert "ods_trade_cal" in tables
        assert "ods_concept_detail" in tables
        assert "ods_etl_log" in tables

        # DIM (4)
        assert "dim_stock" in tables
        assert "dim_date" in tables
        assert "dim_concept" in tables
        assert "dim_concept_stock" in tables

        # DWD (3)
        assert "dwd_daily_quote" in tables
        assert "dwd_weekly_quote" in tables
        assert "dwd_daily_moneyflow" in tables

        # DWS (10)
        for indicator in ["kpattern", "macd", "ma", "dde", "volume"]:
            for freq in ["daily", "weekly"]:
                assert f"dws_{indicator}_{freq}" in tables

    def test_latest_views_exist(self, db_with_schema):
        views = {r[0] for r in db_with_schema.execute(
            "SELECT name FROM sqlite_master WHERE type='view'").fetchall()}

        # 10 latest views
        for indicator in ["kpattern", "macd", "ma", "dde", "volume"]:
            for freq in ["daily", "weekly"]:
                assert f"v_dws_{indicator}_{freq}_latest" in views

        # 4 ADS wide views
        assert "v_ads_analysis_wide_daily" in views
        assert "v_ads_analysis_wide_weekly" in views
        assert "v_ads_index_wide" in views
        assert "v_ads_index_wide_weekly" in views

    def test_indexes_exist(self, db_with_schema):
        indexes = {r[0] for r in db_with_schema.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}

        # DWS indexes — at least the macd ones
        assert "idx_macd_daily_cd" in indexes
        assert "idx_macd_daily_dc" in indexes
        assert "idx_macd_weekly_cd" in indexes
        assert "idx_macd_weekly_dc" in indexes

        # DWD indexes
        assert "idx_dwd_daily_cd" in indexes
        assert "idx_dwd_mf_cd" in indexes
        assert "idx_dwd_weekly_cd" in indexes

        # ODS indexes
        assert "idx_ods_daily_date" in indexes
        assert "idx_ods_daily_basic_date" in indexes
        assert "idx_ods_moneyflow_date" in indexes

        # DIM index
        assert "idx_dim_stock_code" in indexes

    def test_etl_log_columns(self, db_with_schema):
        """ods_etl_log has min_trade_date and max_trade_date columns."""
        cols = {r[0] for r in db_with_schema.execute(
            "DESCRIBE ods_etl_log").fetchall()}
        assert "min_trade_date" in cols, "min_trade_date column missing"
        assert "max_trade_date" in cols, "max_trade_date column missing"

    def test_data_freshness_view_exists(self, db_with_schema):
        """v_data_freshness view is created."""
        views = {r[0] for r in db_with_schema.execute(
            "SELECT name FROM sqlite_master WHERE type='view'").fetchall()}
        assert "v_data_freshness" in views, "v_data_freshness view missing"


class TestCheckConstraints:
    """Verify DWS CHECK constraints reject invalid data and accept valid data."""

    # -- kpattern --

    def test_kpattern_check_rejects_invalid_bool(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_kpattern_daily (ts_code, trade_date, calc_date, yang_bao_yin)
                VALUES ('000001.SZ', '20260101', '20260101', 2)
            """)

    def test_kpattern_check_rejects_invalid_strength(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_kpattern_daily (ts_code, trade_date, calc_date, strength)
                VALUES ('000001.SZ', '20260101', '20260101', 1.5)
            """)

    def test_kpattern_check_accepts_valid(self, db_with_schema):
        db_with_schema.execute("""
            INSERT INTO dws_kpattern_daily (ts_code, trade_date, calc_date, yang_bao_yin, strength)
            VALUES ('000001.SZ', '20260101', '20260101', 1, 0.85)
        """)

    # -- macd --

    def test_macd_check_rejects_invalid_trend(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, trend)
                VALUES ('000001.SZ', '20260101', '20260101', 'invalid')
            """)

    def test_macd_check_rejects_invalid_zone(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, zone)
                VALUES ('000001.SZ', '20260101', '20260101', 'neutral')
            """)

    def test_macd_check_rejects_invalid_divergence(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, divergence)
                VALUES ('000001.SZ', '20260101', '20260101', 'fake_divergence')
            """)

    def test_macd_check_rejects_invalid_turning_point(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, turning_point)
                VALUES ('000001.SZ', '20260101', '20260101', 'super_cross')
            """)

    def test_macd_check_rejects_invalid_alert(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, alert)
                VALUES ('000001.SZ', '20260101', '20260101', 'fake_alert')
            """)

    def test_macd_check_accepts_valid(self, db_with_schema):
        db_with_schema.execute("""
            INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, trend, zone)
            VALUES ('000001.SZ', '20260101', '20260101', 'up', 'bull')
        """)

    def test_macd_check_accepts_null_enum(self, db_with_schema):
        db_with_schema.execute("""
            INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, trend, divergence,
                                        turning_point, alert)
            VALUES ('000001.SZ', '20260101', '20260101', 'flat', NULL, NULL, NULL)
        """)

    # -- ma --

    def test_ma_check_rejects_invalid_alignment(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_ma_daily (ts_code, trade_date, calc_date, alignment)
                VALUES ('000001.SZ', '20260101', '20260101', 'invalid_state')
            """)

    def test_ma_check_rejects_invalid_turning_point(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_ma_daily (ts_code, trade_date, calc_date, turning_point)
                VALUES ('000001.SZ', '20260101', '20260101', 'super_cross')
            """)

    def test_ma_check_accepts_valid(self, db_with_schema):
        db_with_schema.execute("""
            INSERT INTO dws_ma_daily (ts_code, trade_date, calc_date, alignment)
            VALUES ('000001.SZ', '20260101', '20260101', 'bull_strong')
        """)

    def test_ma_check_accepts_null_alignment(self, db_with_schema):
        db_with_schema.execute("""
            INSERT INTO dws_ma_daily (ts_code, trade_date, calc_date, alignment, turning_point)
            VALUES ('000001.SZ', '20260101', '20260101', NULL, NULL)
        """)

    # -- dde --

    def test_dde_check_rejects_invalid_trend(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_dde_daily (ts_code, trade_date, calc_date, trend)
                VALUES ('000001.SZ', '20260101', '20260101', 'sideways')
            """)

    def test_dde_check_rejects_invalid_alert(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_dde_daily (ts_code, trade_date, calc_date, alert)
                VALUES ('000001.SZ', '20260101', '20260101', 'fake_alert')
            """)

    def test_dde_check_rejects_invalid_divergence(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_dde_daily (ts_code, trade_date, calc_date, divergence)
                VALUES ('000001.SZ', '20260101', '20260101', 'bad_divergence')
            """)

    def test_dde_check_accepts_valid(self, db_with_schema):
        db_with_schema.execute("""
            INSERT INTO dws_dde_daily (ts_code, trade_date, calc_date, trend)
            VALUES ('000001.SZ', '20260101', '20260101', 'up')
        """)

    # -- volume --

    def test_volume_check_rejects_invalid_rank_low(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_volume_daily (ts_code, trade_date, calc_date, pct_vol_rank)
                VALUES ('000001.SZ', '20260101', '20260101', -1)
            """)

    def test_volume_check_rejects_invalid_rank_high(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_volume_daily (ts_code, trade_date, calc_date, pct_vol_rank)
                VALUES ('000001.SZ', '20260101', '20260101', 101)
            """)

    def test_volume_check_rejects_invalid_zone(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_volume_daily (ts_code, trade_date, calc_date, zone)
                VALUES ('000001.SZ', '20260101', '20260101', 'medium')
            """)

    def test_volume_check_rejects_invalid_trend(self, db_with_schema):
        with pytest.raises(Exception):
            db_with_schema.execute("""
                INSERT INTO dws_volume_daily (ts_code, trade_date, calc_date, trend)
                VALUES ('000001.SZ', '20260101', '20260101', 'growing')
            """)

    def test_volume_check_accepts_valid(self, db_with_schema):
        db_with_schema.execute("""
            INSERT INTO dws_volume_daily (ts_code, trade_date, calc_date, pct_vol_rank, zone, trend)
            VALUES ('000001.SZ', '20260101', '20260101', 50, 'normal', 'flat')
        """)

    def test_volume_check_accepts_boundary_rank(self, db_with_schema):
        db_with_schema.execute("""
            INSERT INTO dws_volume_daily (ts_code, trade_date, calc_date, pct_vol_rank, zone, trend)
            VALUES ('000001.SZ', '20260102', '20260102', 0, 'low_volume', 'shrinking')
        """)
        db_with_schema.execute("""
            INSERT INTO dws_volume_daily (ts_code, trade_date, calc_date, pct_vol_rank, zone, trend)
            VALUES ('000001.SZ', '20260103', '20260103', 100, 'explosive', 'expanding')
        """)
