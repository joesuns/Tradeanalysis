"""Tests for ods_moneyflow.py — moneyflow data fetch."""

from unittest.mock import patch, MagicMock


def test_fetch_moneyflow_batch_writes_all_columns(db_with_schema, monkeypatch):
    """Mock moneyflow with buy/sell data and verify all 20 data columns are written correctly."""
    monkeypatch.setenv("TUSHARE_TOKEN", "test")

    with patch("backend.fetch.client.ts.pro_api") as mock_pro:
        mock = MagicMock()
        mock.moneyflow.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"ts_code": "000001.SZ", "trade_date": "20260101",
             "buy_sm_vol": 1000.0, "buy_sm_amount": 10000.0,
             "sell_sm_vol": 800.0, "sell_sm_amount": 8000.0,
             "buy_md_vol": 2000.0, "buy_md_amount": 20000.0,
             "sell_md_vol": 1500.0, "sell_md_amount": 15000.0,
             "buy_lg_vol": 5000.0, "buy_lg_amount": 50000.0,
             "sell_lg_vol": 3000.0, "sell_lg_amount": 30000.0,
             "buy_elg_vol": 10000.0, "buy_elg_amount": 100000.0,
             "sell_elg_vol": 8000.0, "sell_elg_amount": 80000.0,
             "net_mf_vol": 500.0, "net_mf_amount": 5000.0},
        ])
        mock_pro.return_value = mock

        from backend.fetch.ods_moneyflow import fetch_moneyflow_batch
        from backend.fetch.client import TushareClient
        from backend.db.schema import create_all_tables
        create_all_tables(db_with_schema)

        rows, failed = fetch_moneyflow_batch(
            TushareClient(), db_with_schema, ["000001.SZ"], "20260101", "20260105"
        )

        assert rows == 1
        assert failed == []

        # Verify all 20 data columns (excluding fetched_at which is auto-generated)
        row = db_with_schema.execute(
            "SELECT ts_code, trade_date, "
            "buy_sm_vol, buy_sm_amount, sell_sm_vol, sell_sm_amount, "
            "buy_md_vol, buy_md_amount, sell_md_vol, sell_md_amount, "
            "buy_lg_vol, buy_lg_amount, sell_lg_vol, sell_lg_amount, "
            "buy_elg_vol, buy_elg_amount, sell_elg_vol, sell_elg_amount, "
            "net_mf_vol, net_mf_amount "
            "FROM ods_moneyflow WHERE ts_code='000001.SZ'"
        ).fetchone()

        assert row is not None
        assert row[0] == "000001.SZ"      # ts_code
        assert row[1] == "20260101"       # trade_date
        # Small order
        assert row[2] == 1000.0           # buy_sm_vol
        assert row[3] == 10000.0          # buy_sm_amount
        assert row[4] == 800.0            # sell_sm_vol
        assert row[5] == 8000.0           # sell_sm_amount
        # Medium order
        assert row[6] == 2000.0           # buy_md_vol
        assert row[7] == 20000.0          # buy_md_amount
        assert row[8] == 1500.0           # sell_md_vol
        assert row[9] == 15000.0          # sell_md_amount
        # Large order
        assert row[10] == 5000.0          # buy_lg_vol
        assert row[11] == 50000.0         # buy_lg_amount
        assert row[12] == 3000.0          # sell_lg_vol
        assert row[13] == 30000.0         # sell_lg_amount
        # Extra-large order
        assert row[14] == 10000.0         # buy_elg_vol
        assert row[15] == 100000.0        # buy_elg_amount
        assert row[16] == 8000.0          # sell_elg_vol
        assert row[17] == 80000.0         # sell_elg_amount
        # Net flow
        assert row[18] == 500.0           # net_mf_vol
        assert row[19] == 5000.0          # net_mf_amount


def test_fetch_moneyflow_batch_failed_stock(db_with_schema, monkeypatch):
    """Test that a stock failing the moneyflow API call is returned in the failed list."""
    monkeypatch.setenv("TUSHARE_TOKEN", "test")

    mock_client = MagicMock()

    def mock_call(func_name, **kwargs):
        ts_code = kwargs.get("ts_code", "")
        if ts_code == "000002.SZ":
            raise Exception("tushare moneyflow error")
        return [{"ts_code": ts_code, "trade_date": "20260101",
                 "buy_sm_vol": 1000.0, "buy_sm_amount": 10000.0,
                 "sell_sm_vol": 800.0, "sell_sm_amount": 8000.0,
                 "buy_md_vol": 2000.0, "buy_md_amount": 20000.0,
                 "sell_md_vol": 1500.0, "sell_md_amount": 15000.0,
                 "buy_lg_vol": 5000.0, "buy_lg_amount": 50000.0,
                 "sell_lg_vol": 3000.0, "sell_lg_amount": 30000.0,
                 "buy_elg_vol": 10000.0, "buy_elg_amount": 100000.0,
                 "sell_elg_vol": 8000.0, "sell_elg_amount": 80000.0,
                 "net_mf_vol": 500.0, "net_mf_amount": 5000.0}]

    mock_client.call.side_effect = mock_call

    from backend.fetch.ods_moneyflow import fetch_moneyflow_batch
    from backend.db.schema import create_all_tables
    create_all_tables(db_with_schema)

    rows, failed = fetch_moneyflow_batch(
        mock_client, db_with_schema, ["000001.SZ", "000002.SZ"], "20260101", "20260105"
    )

    assert rows == 1  # Only one stock succeeded
    assert failed == ["000002.SZ"]

    # Verify successful stock is in the table
    count = db_with_schema.execute(
        "SELECT COUNT(*) FROM ods_moneyflow WHERE ts_code='000001.SZ'"
    ).fetchone()[0]
    assert count == 1


def test_fetch_moneyflow_batch_multiple_stocks(db_with_schema, monkeypatch):
    """Mock moneyflow for two stocks and verify both are inserted."""
    monkeypatch.setenv("TUSHARE_TOKEN", "test")

    mock_client = MagicMock()

    def mock_call(func_name, **kwargs):
        ts_code = kwargs.get("ts_code", "")
        base_data = {"trade_date": "20260101",
                     "buy_sm_vol": 1000.0, "buy_sm_amount": 10000.0,
                     "sell_sm_vol": 800.0, "sell_sm_amount": 8000.0,
                     "buy_md_vol": 2000.0, "buy_md_amount": 20000.0,
                     "sell_md_vol": 1500.0, "sell_md_amount": 15000.0,
                     "buy_lg_vol": 5000.0, "buy_lg_amount": 50000.0,
                     "sell_lg_vol": 3000.0, "sell_lg_amount": 30000.0,
                     "buy_elg_vol": 10000.0, "buy_elg_amount": 100000.0,
                     "sell_elg_vol": 8000.0, "sell_elg_amount": 80000.0,
                     "net_mf_vol": 500.0, "net_mf_amount": 5000.0}
        return [dict(ts_code=ts_code, **base_data)]

    mock_client.call.side_effect = mock_call

    from backend.fetch.ods_moneyflow import fetch_moneyflow_batch
    from backend.db.schema import create_all_tables
    create_all_tables(db_with_schema)

    rows, failed = fetch_moneyflow_batch(
        mock_client, db_with_schema, ["000001.SZ", "000003.SZ"], "20260101", "20260105"
    )

    assert rows == 2
    assert failed == []

    codes = [r[0] for r in db_with_schema.execute(
        "SELECT DISTINCT ts_code FROM ods_moneyflow ORDER BY ts_code"
    ).fetchall()]
    assert codes == ["000001.SZ", "000003.SZ"]
