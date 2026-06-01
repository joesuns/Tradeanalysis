from backend.db.schema import create_all_tables
from backend.etl.build_dim import build_dim_stock, build_dim_date
from backend.etl.build_dwd import (
    build_dwd_daily_quote,
    build_dwd_weekly_quote,
    build_dwd_daily_moneyflow,
)


def test_qfq_formula(temp_db):
    """前复权: price_qfq = price * adj_factor / latest_adj_factor"""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES ('20260101',1),('20260102',1)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor) "
        "VALUES ('TEST.SZ','20260101',10.0,4.0),('TEST.SZ','20260102',12.0,2.0)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) "
        "VALUES ('TEST.SZ','20260101',1000),('TEST.SZ','20260102',1200)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    rows = temp_db.execute(
        "SELECT trade_date, close_qfq FROM dwd_daily_quote "
        "WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()
    # latest_adj_factor = 2.0 (from 20260102)
    # 20260101: close_qfq = 10.0 * 4.0 / 2.0 = 20.0
    assert abs(rows[0][1] - 20.0) < 0.01
    # 20260102: close_qfq = 12.0 * 2.0 / 2.0 = 12.0
    assert abs(rows[1][1] - 12.0) < 0.01


def test_suspension_detection(temp_db):
    """Trading day with no ods_daily row -> is_suspended=1, OHLCV=previous close."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES ('20260101',1),('20260102',1)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor) "
        "VALUES ('TEST.SZ','20260101',10.0,1.0)"
    )
    # No data for 20260102 -> should be marked as suspended
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date) VALUES ('TEST.SZ','20260101')"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    rows = temp_db.execute(
        "SELECT trade_date, is_suspended, vol FROM dwd_daily_quote "
        "WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()
    assert len(rows) == 2
    assert rows[1][1] == 1  # is_suspended
    assert rows[1][2] == 0  # vol=0


def test_weekly_aggregation(temp_db):
    """Weekly bars aggregate daily data correctly."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    # A full week of trading days (Mon-Fri)
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) "
        "VALUES ('20260105',1),('20260106',1),('20260107',1),('20260108',1),('20260109',1)"
    )
    # Set 20260109 as week end
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,adj_factor) "
        "VALUES "
        "('TEST.SZ','20260105',10.0,11.0,9.0,10.5,100,1000,0.05,1.0),"
        "('TEST.SZ','20260106',10.5,12.0,10.0,11.0,200,2000,0.03,1.0),"
        "('TEST.SZ','20260107',11.0,11.5,10.5,10.8,150,1500,-0.02,1.0),"
        "('TEST.SZ','20260108',10.8,12.5,10.5,12.0,180,1800,0.10,1.0),"
        "('TEST.SZ','20260109',12.0,13.0,11.5,12.5,220,2200,0.04,1.0)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])

    # Now build weekly
    cnt = build_dwd_weekly_quote(temp_db, ["TEST.SZ"])
    assert cnt >= 1

    row = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, amount, pct_chg, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ'"
    ).fetchone()
    assert row is not None
    # Week end date should be 20260109
    assert row[0] == "20260109"
    # open = first day open (10.0)
    assert abs(row[1] - 10.0) < 0.01
    # high = max (13.0)
    assert abs(row[2] - 13.0) < 0.01
    # low = min (9.0)
    assert abs(row[3] - 9.0) < 0.01
    # close = last day close (12.5)
    assert abs(row[4] - 12.5) < 0.01
    # vol = SUM(vol) / 5 * 5 = SUM(vol) = 850
    assert abs(row[5] - 850.0) < 0.01
    # active_days = 5
    assert row[8] == 5


def test_moneyflow_mapping(temp_db):
    """ods_moneyflow maps to dwd_daily_moneyflow with correct total_vol."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO dim_stock (ts_code,stock_code,symbol,name,exchange) "
        "VALUES ('TEST.SZ','TEST','TEST','Test','深圳')"
    )
    temp_db.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code,trade_date,buy_sm_vol,buy_md_vol,buy_lg_vol,buy_elg_vol,"
        "sell_sm_vol,sell_md_vol,sell_lg_vol,sell_elg_vol,"
        "net_mf_vol,net_mf_amount) "
        "VALUES ('TEST.SZ','20260101',100,200,300,400,50,100,150,200,500,5000)"
    )
    build_dwd_daily_moneyflow(temp_db, ["TEST.SZ"])
    row = temp_db.execute(
        "SELECT net_mf_vol, net_mf_amount, buy_lg_vol, sell_lg_vol, "
        "buy_elg_vol, sell_elg_vol, total_vol "
        "FROM dwd_daily_moneyflow WHERE ts_code='TEST.SZ'"
    ).fetchone()
    assert row[0] == 500   # net_mf_vol
    assert row[1] == 5000  # net_mf_amount
    assert row[2] == 300   # buy_lg_vol
    assert row[3] == 150   # sell_lg_vol
    assert row[4] == 400   # buy_elg_vol
    assert row[5] == 200   # sell_elg_vol
    assert row[6] == 1000  # total_vol = 100+200+300+400
