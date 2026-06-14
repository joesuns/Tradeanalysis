import pytest


def test_sync_dwd_dde_meta_updates_drifted_dc_and_circ(db_with_schema):
    from backend.etl.sync_dwd_dde_meta import sync_dwd_dde_meta

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code,trade_date,net_mf_amount,net_amount_dc,"
        "buy_lg_vol,sell_lg_vol,buy_elg_vol,sell_elg_vol,fetched_at) "
        "VALUES ('S1.SZ','20240102',1.0,-99.0,1,1,1,1,now())"
    )
    con.execute(
        "INSERT INTO dwd_daily_moneyflow "
        "(ts_code,trade_date,net_mf_amount,net_amount_dc,"
        "buy_lg_vol,sell_lg_vol,buy_elg_vol,sell_elg_vol,total_vol) "
        "VALUES ('S1.SZ','20240102',1.0,NULL,1,1,1,1,4)"
    )
    con.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,circ_mv,fetched_at) "
        "VALUES ('S1.SZ','20240102',555.0,now())"
    )
    con.execute(
        "INSERT INTO dwd_daily_quote "
        "(ts_code,trade_date,open_qfq,high_qfq,low_qfq,close_qfq,vol,"
        "circ_mv,is_suspended) "
        "VALUES ('S1.SZ','20240102',1,1,1,1,1,NULL,0)"
    )

    out = sync_dwd_dde_meta(con, ts_codes=["S1.SZ"], since="20240101")
    assert out["moneyflow_dc_updated"] >= 1
    assert out["quote_circ_updated"] >= 1

    dc = con.execute(
        "SELECT net_amount_dc FROM dwd_daily_moneyflow WHERE ts_code='S1.SZ'"
    ).fetchone()[0]
    circ = con.execute(
        "SELECT circ_mv FROM dwd_daily_quote WHERE ts_code='S1.SZ'"
    ).fetchone()[0]
    assert dc == pytest.approx(-99.0)
    assert circ == pytest.approx(555.0)
