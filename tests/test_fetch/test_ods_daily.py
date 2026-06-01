"""Tests for ods_daily.py — date-based batch fetch."""
import pytest
from unittest.mock import patch, MagicMock


def test_fetch_by_date_range(db_with_schema, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test")

    with patch("backend.fetch.client.ts.pro_api") as mock_pro:
        mock = MagicMock()

        # Mock _get_trading_days: return 2 dates
        mock.trade_cal.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"cal_date": "20260101"}, {"cal_date": "20260102"},
        ])
        # Mock adj_factor
        mock.adj_factor.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"ts_code": "000001.SZ", "trade_date": "20260101", "adj_factor": 1.5},
        ])
        # Mock daily
        mock.daily.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"ts_code": "000001.SZ", "trade_date": "20260101",
             "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
             "vol": 1000000, "amount": 10000000, "pct_chg": 1.0},
        ])
        # Mock daily_basic
        mock.daily_basic.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"ts_code": "000001.SZ", "trade_date": "20260101",
             "total_mv": 100000, "pe_ttm": 12.0, "turnover_rate": 2.5, "volume_ratio": 1.0},
        ])
        # Mock moneyflow
        mock.moneyflow.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"ts_code": "000001.SZ", "trade_date": "20260101",
             "buy_sm_vol": 100, "buy_sm_amount": 1000, "sell_sm_vol": 50, "sell_sm_amount": 500,
             "buy_md_vol": 200, "buy_md_amount": 2000, "sell_md_vol": 100, "sell_md_amount": 1000,
             "buy_lg_vol": 300, "buy_lg_amount": 3000, "sell_lg_vol": 150, "sell_lg_amount": 1500,
             "buy_elg_vol": 400, "buy_elg_amount": 4000, "sell_elg_vol": 200, "sell_elg_amount": 2000,
             "net_mf_vol": 500, "net_mf_amount": 5000},
        ])
        mock_pro.return_value = mock

        from backend.fetch.ods_daily import fetch_by_date_range
        from backend.fetch.client import TushareClient
        from backend.db.schema import create_all_tables
        create_all_tables(db_with_schema)

        total = fetch_by_date_range(TushareClient(), db_with_schema, "20260101", "20260102")
        assert total > 0

        # Verify ods_daily has adj_factor from lookup
        row = db_with_schema.execute(
            "SELECT close, adj_factor FROM ods_daily WHERE ts_code='000001.SZ' AND trade_date='20260101'"
        ).fetchone()
        assert row[0] == pytest.approx(10.2)
        assert row[1] == pytest.approx(1.5)  # adj_factor from lookup map


def test_get_all_active_codes_filters_delisted(db_with_schema):
    from backend.db.schema import create_all_tables
    create_all_tables(db_with_schema)
    db_with_schema.execute("INSERT INTO ods_stock_basic (ts_code, symbol, name) VALUES ('ACTIVE.SZ','ACTIVE','Active')")
    db_with_schema.execute("INSERT INTO ods_stock_basic (ts_code, symbol, name, delist_date) VALUES ('DELISTED.SZ','DEL','Delisted','20200101')")
    from backend.fetch.ods_daily import get_all_active_codes
    codes = get_all_active_codes(db_with_schema)
    assert "ACTIVE.SZ" in codes
    assert "DELISTED.SZ" not in codes
