"""Tests for dc_theme fetch module — TTL, enrichment query, snapshot freshness."""
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timedelta


class TestThemeSnapshotFreshness:
    """TTL gate tests for dc_theme source."""

    def test_theme_fresh_within_ttl(self):
        """Snapshot fetched today -> fresh."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.execute.return_value.fetchone.return_value = [now_str, 613]

        assert _is_snapshot_fresh(
            con, "20260620", "dc_theme", "题材", ttl_days=7,
        ) is True

    def test_theme_expired_beyond_ttl(self):
        """Snapshot fetched 10 days ago -> stale."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        stale = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute.return_value.fetchone.return_value = [stale, 613]

        assert _is_snapshot_fresh(
            con, "20260620", "dc_theme", "题材", ttl_days=7,
        ) is False

    def test_theme_no_snapshot(self):
        """No snapshot row -> stale."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        con.execute.return_value.fetchone.return_value = None

        assert _is_snapshot_fresh(
            con, "20260620", "dc_theme", "题材", ttl_days=7,
        ) is False

    def test_theme_zero_boards_is_stale(self):
        """n_boards=0 must be stale regardless of fetched_at."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.execute.return_value.fetchone.return_value = [now_str, 0]

        assert _is_snapshot_fresh(
            con, "20260620", "dc_theme", "题材", ttl_days=7,
        ) is False


class TestLoadThemeEnrichment:
    """Theme enrichment query tests."""

    def test_theme_enrichment_single_stock(self):
        """Stock in 2 themes -> comma-separated."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        # Side effects: TDX, DC concept, DC theme
        con.execute.return_value.fetchall.side_effect = [
            [("000001.SZ", "银行")],                    # TDX
            [("000001.SZ", "央企改革,沪深300")],         # DC concept
            [("000001.SZ", "雄安新区,跨境支付")],         # DC theme
        ]

        result = load_plate_enrichment(con, "20260620")
        assert result["000001.SZ"]["tdx_industry_board"] == "银行"
        assert result["000001.SZ"]["dc_concept_board"] == "央企改革,沪深300"
        assert result["000001.SZ"]["dc_theme_board"] == "雄安新区,跨境支付"

    def test_theme_enrichment_stock_no_theme(self):
        """Stock with TDX/DC concept but no theme data."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        con.execute.return_value.fetchall.side_effect = [
            [("830001.BJ", "制造业")],  # TDX
            [],                        # DC concept: none
            [],                        # DC theme: none
        ]

        result = load_plate_enrichment(con, "20260620")
        assert result["830001.BJ"]["tdx_industry_board"] == "制造业"
        assert "dc_concept_board" not in result["830001.BJ"]
        assert "dc_theme_board" not in result["830001.BJ"]

    def test_theme_enrichment_empty_when_no_data(self):
        """No data at all -> empty dict."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        con.execute.return_value.fetchall.return_value = []

        result = load_plate_enrichment(con, "20260620")
        assert result == {}
