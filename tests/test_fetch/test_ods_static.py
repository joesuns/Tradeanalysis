import json

def test_fetch_stock_basic(db_with_schema, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test")
    from unittest.mock import patch, MagicMock

    with patch('backend.fetch.client.ts.pro_api') as mock_pro:
        mock = MagicMock()
        mock.stock_basic.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"ts_code":"000001.SZ","symbol":"000001","name":"平安银行","area":"深圳",
             "industry":"银行","exchange":"SZSE","list_date":"19910403","delist_date":""}])
        mock_pro.return_value = mock

        from backend.fetch.ods_stock_basic import fetch_stock_basic
        from backend.fetch.client import TushareClient
        from backend.db.schema import create_all_tables
        create_all_tables(db_with_schema)
        n = fetch_stock_basic(TushareClient(), db_with_schema)
        assert n == 1

        row = db_with_schema.execute(
            "SELECT name, exchange, raw_json FROM ods_stock_basic WHERE ts_code='000001.SZ'"
        ).fetchone()
        assert row[0] == "平安银行"
        assert row[1] == "SZSE"
        assert json.loads(row[2])["name"] == "平安银行"

def test_fetch_trade_cal(db_with_schema, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test")
    from unittest.mock import patch, MagicMock

    with patch('backend.fetch.client.ts.pro_api') as mock_pro:
        mock = MagicMock()
        mock.trade_cal.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"cal_date":"20260101","is_open":1,"pretrade_date":"20251231"},
            {"cal_date":"20260102","is_open":1,"pretrade_date":"20260101"},
        ])
        mock_pro.return_value = mock

        from backend.fetch.ods_trade_cal import fetch_trade_cal
        from backend.fetch.client import TushareClient
        from backend.db.schema import create_all_tables
        create_all_tables(db_with_schema)
        n = fetch_trade_cal(TushareClient(), db_with_schema, start="20260101", end="20260105")
        assert n == 2

def test_fetch_concept_detail(db_with_schema, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test")
    from unittest.mock import patch, MagicMock

    with patch('backend.fetch.client.ts.pro_api') as mock_pro:
        mock = MagicMock()
        mock.concept_detail.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"concept_name":"人工智能","ts_code":"000001.SZ"},
        ])
        mock_pro.return_value = mock

        from backend.fetch.ods_concept import fetch_concept_detail
        from backend.fetch.client import TushareClient
        from backend.db.schema import create_all_tables
        create_all_tables(db_with_schema)
        n = fetch_concept_detail(TushareClient(), db_with_schema, ts_codes=["000001.SZ"])
        assert n == 1
