"""Tests for ods_plate fetch module — TTL logic, enrichment query, snapshot freshness."""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta


class TestSnapshotFreshness:
    """_is_snapshot_fresh TTL gate tests."""

    def test_fresh_within_ttl(self):
        """Snapshot fetched today → fresh."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.execute.return_value.fetchone.return_value = [now_str]

        assert _is_snapshot_fresh(con, "20260620", "tdx", "行业板块") is True

    def test_expired_beyond_ttl(self):
        """Snapshot fetched 10 days ago → stale."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        stale = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute.return_value.fetchone.return_value = [stale]

        assert _is_snapshot_fresh(con, "20260620", "tdx", "行业板块") is False

    def test_no_snapshot_exists(self):
        """No snapshot row → stale."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        con.execute.return_value.fetchone.return_value = None

        assert _is_snapshot_fresh(con, "20260620", "dc", "概念板块") is False

    def test_each_source_independent(self):
        """TDX fresh + DC stale → independent TTL."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stale = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

        # First call → fresh, second call → stale
        con.execute.return_value.fetchone.side_effect = [[now_str], [stale]]

        assert _is_snapshot_fresh(con, "20260620", "tdx", "行业板块") is True
        assert _is_snapshot_fresh(con, "20260620", "dc", "概念板块") is False

    def test_custom_ttl_days(self):
        """Custom ttl_days: DC 3d TTL — 4-day-old snapshot is stale."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        stale_4d = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute.return_value.fetchone.return_value = [stale_4d]

        assert _is_snapshot_fresh(con, "20260620", "dc", "概念板块", ttl_days=3) is False

    def test_custom_ttl_still_fresh(self):
        """Custom ttl_days: DC 3d TTL — 2-day-old snapshot is still fresh."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        fresh_2d = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute.return_value.fetchone.return_value = [fresh_2d]

        assert _is_snapshot_fresh(con, "20260620", "dc", "概念板块", ttl_days=3) is True

    def test_per_source_ttl_in_config(self):
        """_PLATE_SOURCES has per-source ttl_days."""
        from backend.fetch.ods_plate import _PLATE_SOURCES

        assert _PLATE_SOURCES["tdx"]["ttl_days"] == 7
        assert _PLATE_SOURCES["dc"]["ttl_days"] == 3


class TestLoadPlateEnrichment:
    """load_plate_enrichment export query tests."""

    def test_enrichment_empty_when_no_data(self):
        """No plate data → empty dict."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        con.execute.return_value.fetchall.return_value = []

        result = load_plate_enrichment(con, "20260620")
        assert result == {}

    def test_enrichment_single_stock_multi_board(self):
        """Stock in 2 boards → comma-separated."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        # First call: TDX, second call: DC
        con.execute.return_value.fetchall.side_effect = [
            [("000001.SZ", "银行,金融")],   # TDX
            [("000001.SZ", "央企改革,深证100")],  # DC
        ]

        result = load_plate_enrichment(con, "20260620")
        assert result["000001.SZ"]["tdx_industry_board"] == "银行,金融"
        assert result["000001.SZ"]["dc_concept_board"] == "央企改革,深证100"

    def test_enrichment_stock_no_dc_concept(self):
        """BSE stock → TDX board but no DC concept."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        con.execute.return_value.fetchall.side_effect = [
            [("830001.BJ", "制造业")],  # TDX
            [],                         # DC: no data for BSE stocks
        ]

        result = load_plate_enrichment(con, "20260620")
        assert result["830001.BJ"]["tdx_industry_board"] == "制造业"
        assert "dc_concept_board" not in result["830001.BJ"]

    def test_enrichment_multiple_stocks(self):
        """Multiple stocks with different board counts."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        con.execute.return_value.fetchall.side_effect = [
            [("000001.SZ", "银行"), ("000002.SZ", "房地产")],  # TDX
            [("000001.SZ", "央企改革,沪深300"), ("000002.SZ", "物业管理")],  # DC
        ]

        result = load_plate_enrichment(con, "20260620")
        assert len(result) == 2
        assert result["000001.SZ"]["tdx_industry_board"] == "银行"
        assert result["000002.SZ"]["dc_concept_board"] == "物业管理"
