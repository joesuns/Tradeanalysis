import pytest


def test_apply_net_amount_dc_patch_null_only(db_with_schema):
    import pandas as pd
    from backend.fetch.ods_daily import _apply_net_amount_dc_patch

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code, trade_date, net_mf_amount, net_amount_dc, fetched_at) VALUES "
        "('Y.SZ','20240102',1.0,NULL,now()), "
        "('Y.SZ','20240103',2.0,5.0,now())"
    )
    patch = pd.DataFrame([
        {"ts_code": "Y.SZ", "trade_date": "20240102", "net_amount_dc": 10.0},
        {"ts_code": "Y.SZ", "trade_date": "20240103", "net_amount_dc": 99.0},
    ])
    n = _apply_net_amount_dc_patch(con, patch)
    assert n == 1
    assert con.execute(
        "SELECT net_amount_dc FROM ods_moneyflow "
        "WHERE ts_code='Y.SZ' AND trade_date='20240103'"
    ).fetchone()[0] == 5.0


def test_apply_circ_mv_patch_insert_and_update(db_with_schema):
    import pandas as pd
    from backend.fetch.ods_daily import _apply_circ_mv_patch

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_daily VALUES "
        "('Z.SZ','20240102',10,12,9,11,100,1000,0,1.0,now())"
    )
    con.execute(
        "INSERT INTO ods_daily_basic "
        "(ts_code, trade_date, circ_mv, fetched_at) "
        "VALUES ('Z.SZ','20240102',NULL,now())"
    )
    patch = pd.DataFrame([
        {"ts_code": "Z.SZ", "trade_date": "20240102", "circ_mv": 123456.0},
    ])
    n = _apply_circ_mv_patch(con, patch)
    assert n == 1
    assert con.execute(
        "SELECT circ_mv FROM ods_daily_basic WHERE ts_code='Z.SZ'"
    ).fetchone()[0] == pytest.approx(123456.0)


def test_list_days_needing_dc_backfill(db_with_schema):
    from backend.fetch.backfill_dde_meta import list_days_needing_dc_backfill

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow (ts_code,trade_date,net_mf_amount,net_amount_dc,fetched_at) VALUES "
        "('A.SZ','20240102',1.0,NULL,now()), "
        "('B.SZ','20240102',1.0,9.0,now()), "
        "('A.SZ','20240103',1.0,8.0,now())"
    )
    days = list_days_needing_dc_backfill(con, "20240102", "20240103")
    assert days == ["20240102"]


def test_list_days_needing_circ_backfill(db_with_schema):
    from backend.fetch.backfill_dde_meta import list_days_needing_circ_backfill

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_daily VALUES "
        "('C1.SZ','20240102',10,12,9,11,100,1000,0,1.0,now()), "
        "('C2.SZ','20240103',10,12,9,11,100,1000,0,1.0,now())"
    )
    con.execute(
        "INSERT INTO ods_daily_basic (ts_code, trade_date, circ_mv, fetched_at) VALUES "
        "('C1.SZ','20240102',NULL,now()), "
        "('C2.SZ','20240103',500.0,now())"
    )
    days = list_days_needing_circ_backfill(con, "20240102", "20240103")
    assert days == ["20240102"]


def test_backfill_net_amount_dc_by_date(db_with_schema):
    from backend.fetch.ods_daily import _backfill_net_amount_dc_by_date

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code,trade_date,net_mf_amount,net_amount_dc,fetched_at) VALUES "
        "('D1.SZ','20240102',1.0,NULL,now()), "
        "('D2.SZ','20240102',1.0,NULL,now())"
    )

    class FakeClient:
        def call(self, api, **kwargs):
            assert api == "moneyflow_dc"
            assert kwargs.get("trade_date") == "20240102"
            return [
                {"ts_code": "D1.SZ", "trade_date": "20240102", "net_amount": 11.0},
                {"ts_code": "D2.SZ", "trade_date": "20240102", "net_amount": 22.0},
            ]

    n = _backfill_net_amount_dc_by_date(con, FakeClient(), "20240102")
    assert n == 2


def test_backfill_dde_meta_ods_by_date_dry_run(db_with_schema):
    from backend.fetch.backfill_dde_meta import backfill_dde_meta_ods_by_date

    con = db_with_schema
    con.execute(
        "INSERT INTO dim_date (trade_date,is_trade_day,is_week_end) "
        "VALUES ('20240102',1,0),('20240103',1,0)"
    )
    con.execute(
        "INSERT INTO ods_moneyflow (ts_code,trade_date,net_mf_amount,net_amount_dc,fetched_at) "
        "VALUES ('T.SZ','20240102',1.0,NULL,now())"
    )

    class FakeClient:
        def call(self, api, **kwargs):
            raise AssertionError("dry_run must not call API")

    stats = backfill_dde_meta_ods_by_date(
        con, FakeClient(), None, "20240102", "20240103",
        dry_run=True, workers=1,
    )
    assert stats["mode"] == "date"
    assert stats["dc_api_calls"] == 1
    assert stats["days_work"] >= 1


def test_backfill_circ_mv_stock_updates_ods_daily_basic(db_with_schema):
    from backend.fetch.ods_daily import _backfill_circ_mv_stock

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_daily VALUES "
        "('CV.SZ','20240102',10,12,9,11,100,1000,0,1.0,now())"
    )
    con.execute(
        "INSERT INTO ods_daily_basic "
        "(ts_code, trade_date, circ_mv, fetched_at) "
        "VALUES ('CV.SZ','20240102',NULL,now())"
    )

    class FakeClient:
        def call(self, api, **kwargs):
            assert api == "daily_basic"
            return [{"trade_date": "20240102", "circ_mv": 123456.0, "total_mv": 999.0}]

    n = _backfill_circ_mv_stock(
        con, FakeClient(), "CV.SZ", "20240102", "20240102",
    )
    assert n == 1
    circ = con.execute(
        "SELECT circ_mv FROM ods_daily_basic WHERE ts_code='CV.SZ'"
    ).fetchone()[0]
    assert circ == pytest.approx(123456.0)


def test_resolve_backfill_range_respects_moneyflow_dc_min():
    from datetime import datetime, timedelta

    from backend.fetch.backfill_dde_meta import resolve_backfill_range

    start, end = resolve_backfill_range("20260612", days=900, since="20230911")
    assert end == "20260612"
    assert start >= "20230911"
    expected = (
        datetime.strptime("20260612", "%Y%m%d") - timedelta(days=900)
    ).strftime("%Y%m%d")
    assert start == max("20230911", expected)


def test_backfill_dde_meta_ods_dry_run_counts(db_with_schema):
    from backend.fetch.backfill_dde_meta import backfill_dde_meta_ods

    con = db_with_schema
    con.execute(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) "
        "VALUES ('20240101', 0, 0), ('20240102', 1, 0)"
    )
    con.execute(
        "INSERT INTO ods_stock_basic (ts_code, list_date) VALUES ('T1.SZ','20200101')"
    )
    con.execute(
        "INSERT INTO ods_daily VALUES "
        "('T1.SZ','20240102',10,12,9,11,100,1000,0,1.0,now())"
    )
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code,trade_date,net_mf_amount,net_amount_dc,fetched_at) "
        "VALUES ('T1.SZ','20240102',1.0,NULL,now())"
    )

    class FakeClient:
        def call(self, api, **kwargs):
            raise AssertionError("dry_run must not call API")

    stats = backfill_dde_meta_ods(
        con, FakeClient(), ["T1.SZ"], "20240102", "20240102", dry_run=True,
    )
    assert stats["stocks"] == 1
    assert stats["dc_null_days"] >= 1
    assert stats["dc_api_calls"] == 1
