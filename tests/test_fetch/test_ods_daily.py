"""Tests for ods_daily.py — daily OHLCV + daily_basic fetch and get_all_active_codes."""

from unittest.mock import patch, MagicMock


def test_fetch_daily_batch_writes_to_both_tables(db_with_schema, monkeypatch):
    """Mock tushare to return 2 days of daily data + 2 days of daily_basic for a single stock."""
    monkeypatch.setenv("TUSHARE_TOKEN", "test")

    with patch("backend.fetch.client.ts.pro_api") as mock_pro:
        mock = MagicMock()
        mock.daily.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"ts_code": "000001.SZ", "trade_date": "20260101", "open": 10.0, "high": 11.0,
             "low": 9.5, "close": 10.8, "vol": 1000000.0, "amount": 10800000.0,
             "pct_chg": 2.5, "adj_factor": 1.0},
            {"ts_code": "000001.SZ", "trade_date": "20260102", "open": 10.8, "high": 11.2,
             "low": 10.5, "close": 11.0, "vol": 1200000.0, "amount": 13200000.0,
             "pct_chg": 1.85, "adj_factor": 1.0},
        ])
        mock.daily_basic.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"ts_code": "000001.SZ", "trade_date": "20260101", "total_mv": 500000000.0,
             "pe_ttm": 8.5, "turnover_rate": 0.5, "volume_ratio": 1.2},
            {"ts_code": "000001.SZ", "trade_date": "20260102", "total_mv": 510000000.0,
             "pe_ttm": 8.7, "turnover_rate": 0.6, "volume_ratio": 1.1},
        ])
        mock_pro.return_value = mock

        from backend.fetch.ods_daily import fetch_daily_batch
        from backend.fetch.client import TushareClient
        from backend.db.schema import create_all_tables
        create_all_tables(db_with_schema)

        rows, failed = fetch_daily_batch(
            TushareClient(), db_with_schema, ["000001.SZ"], "20260101", "20260105"
        )

        # 2 daily + 2 daily_basic = 4 rows
        assert rows == 4
        assert failed == []

        # Verify ods_daily
        daily_rows = db_with_schema.execute(
            "SELECT trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor "
            "FROM ods_daily WHERE ts_code='000001.SZ' ORDER BY trade_date"
        ).fetchall()
        assert len(daily_rows) == 2
        assert daily_rows[0][0] == "20260101"
        assert daily_rows[0][1] == 10.0  # open
        assert daily_rows[1][0] == "20260102"
        assert daily_rows[1][4] == 11.0  # close

        # Verify ods_daily_basic
        basic_rows = db_with_schema.execute(
            "SELECT trade_date, total_mv, pe_ttm, turnover_rate, volume_ratio "
            "FROM ods_daily_basic WHERE ts_code='000001.SZ' ORDER BY trade_date"
        ).fetchall()
        assert len(basic_rows) == 2
        assert basic_rows[0][0] == "20260101"
        assert basic_rows[0][1] == 500000000.0  # total_mv
        assert basic_rows[0][2] == 8.5  # pe_ttm
        assert basic_rows[1][0] == "20260102"
        assert abs(basic_rows[1][3] - 0.6) < 0.001  # turnover_rate (float32)


def test_fetch_daily_batch_failed_stock(db_with_schema, monkeypatch):
    """Test that a stock that fails API call is returned in the failed list
    while the successful stock still gets written."""
    monkeypatch.setenv("TUSHARE_TOKEN", "test")

    # Use a mock client where call raises for a specific ts_code
    mock_client = MagicMock()

    def mock_call(func_name, **kwargs):
        ts_code = kwargs.get("ts_code", "")
        if ts_code == "000002.SZ":
            raise Exception("tushare API error")
        if func_name == "daily":
            return [{"ts_code": ts_code, "trade_date": "20260101", "open": 10.0,
                     "high": 11.0, "low": 9.5, "close": 10.8, "vol": 1000000.0,
                     "amount": 10800000.0, "pct_chg": 2.5, "adj_factor": 1.0}]
        if func_name == "daily_basic":
            return [{"ts_code": ts_code, "trade_date": "20260101",
                     "total_mv": 500000000.0, "pe_ttm": 8.5,
                     "turnover_rate": 0.5, "volume_ratio": 1.2}]
        return []

    mock_client.call.side_effect = mock_call

    from backend.fetch.ods_daily import fetch_daily_batch
    from backend.db.schema import create_all_tables
    create_all_tables(db_with_schema)

    rows, failed = fetch_daily_batch(
        mock_client, db_with_schema, ["000001.SZ", "000002.SZ"], "20260101", "20260105"
    )

    # Only 000001.SZ succeeded: 1 daily + 1 daily_basic = 2 rows
    assert rows == 2
    assert failed == ["000002.SZ"]

    # Verify that the good stock was inserted into both tables
    daily_count = db_with_schema.execute(
        "SELECT COUNT(*) FROM ods_daily WHERE ts_code='000001.SZ'"
    ).fetchone()[0]
    assert daily_count == 1

    basic_count = db_with_schema.execute(
        "SELECT COUNT(*) FROM ods_daily_basic WHERE ts_code='000001.SZ'"
    ).fetchone()[0]
    assert basic_count == 1

    # Verify failed stock has no data
    failed_count = db_with_schema.execute(
        "SELECT COUNT(*) FROM ods_daily WHERE ts_code='000002.SZ'"
    ).fetchone()[0]
    assert failed_count == 0


def test_get_all_active_codes_filters_delisted(db_with_schema):
    """Test that get_all_active_codes returns only non-delisted stocks."""
    from backend.db.schema import create_all_tables
    create_all_tables(db_with_schema)

    # Insert mix of active (empty delist_date), delisted (has date), and NULL delist_date
    db_with_schema.execute(
        "INSERT INTO ods_stock_basic (ts_code, symbol, name, area, industry, exchange, list_date, delist_date) "
        "VALUES ('000001.SZ', '000001', 'Stock A', '', '', '', '20200101', '')"
    )
    db_with_schema.execute(
        "INSERT INTO ods_stock_basic (ts_code, symbol, name, area, industry, exchange, list_date, delist_date) "
        "VALUES ('000002.SZ', '000002', 'Stock B', '', '', '', '20200101', '20250101')"
    )
    db_with_schema.execute(
        "INSERT INTO ods_stock_basic (ts_code, symbol, name, area, industry, exchange, list_date, delist_date) "
        "VALUES ('000003.SZ', '000003', 'Stock C', '', '', '', '20200101', NULL)"
    )

    from backend.fetch.ods_daily import get_all_active_codes
    codes = get_all_active_codes(db_with_schema)

    assert len(codes) == 2
    assert "000001.SZ" in codes   # delist_date = ''  -> active
    assert "000003.SZ" in codes   # delist_date NULL  -> active
    assert "000002.SZ" not in codes  # delist_date has value -> delisted
