from backend.db.schema import create_all_tables
from backend.etl.build_dim import build_dim_stock, build_dim_date
from backend.etl.build_dwd import (
    build_dwd_daily_quote,
    build_dwd_weekly_quote,
    build_dwd_daily_moneyflow,
    rebuild_dwd_incremental,
    rebuild_dwd_for_stale,
    find_adj_changed_codes,
    find_stocks_needing_full_daily_rebuild,
    find_qfq_drift_codes,
    _find_dwd_gap_stocks,
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


def test_build_dwd_excludes_null_adj_factor(temp_db):
    """Rows with NULL adj_factor must be excluded — never produce NULL close_qfq."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('X.SZ','X','X')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES ('20260101',1),('20260102',1)"
    )
    # 20260101 has NULL adj_factor (would yield NULL close_qfq); 20260102 is valid.
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor) "
        "VALUES ('X.SZ','20260101',10.0,NULL),('X.SZ','20260102',12.0,2.0)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["X.SZ"])

    null_qfq = temp_db.execute(
        "SELECT COUNT(*) FROM dwd_daily_quote "
        "WHERE ts_code='X.SZ' AND close_qfq IS NULL"
    ).fetchone()[0]
    assert null_qfq == 0, "NULL-adj row must not produce a NULL close_qfq row"

    rows = temp_db.execute(
        "SELECT trade_date, close_qfq FROM dwd_daily_quote "
        "WHERE ts_code='X.SZ' ORDER BY trade_date"
    ).fetchall()
    # Only the valid 20260102 row survives (latest_adj=2.0 → 12*2/2 = 12.0)
    assert rows == [("20260102", 12.0)], f"expected only valid row, got {rows}"


def test_suspension_detection(temp_db):
    """Internal gap (a trading day with no ods_daily row, between two traded days)
    must be filled as is_suspended=1, vol=0, OHLCV carried from previous close.

    Note: the fill only covers gaps up to the stock's last traded day
    (cal.trade_date <= max(ods date)); trailing gaps are NOT filled by design.
    """
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) "
        "VALUES ('20260101',1),('20260102',1),('20260103',1)"
    )
    # Traded on 0101 and 0103; 0102 is an internal gap -> suspension.
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor) "
        "VALUES ('TEST.SZ','20260101',10.0,1.0),('TEST.SZ','20260103',10.5,1.0)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    rows = temp_db.execute(
        "SELECT trade_date, is_suspended, vol, close_qfq FROM dwd_daily_quote "
        "WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()
    assert len(rows) == 3, f"0102 internal gap should be filled, got {rows}"
    assert rows[1][0] == "20260102"
    assert rows[1][1] == 1       # is_suspended
    assert rows[1][2] == 0       # vol=0
    assert abs(rows[1][3] - 10.0) < 0.01  # close carried from 0101


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

    # Now build weekly — produces one week-to-date bar PER trading day
    cnt = build_dwd_weekly_quote(temp_db, ["TEST.SZ"])
    assert cnt >= 1

    # Inspect the week-end (Friday) bar: full-week aggregates
    row = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, amount, pct_chg, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date='20260109'"
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


def test_weekly_aggregation_cross_year_week(temp_db):
    """跨年自然周必须聚合进同一根周线 bar（而非被 %Y-%W 切成两段）。

    2025-12-29(周一)~2026-01-02(周五) 属同一自然周；周五 bar 的 open 应为
    周一开盘、active_days=5、high/low 覆盖整周。
    """
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES "
        "('20251229',1),('20251230',1),('20251231',1),('20260101',1),('20260102',1)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,adj_factor) "
        "VALUES "
        "('TEST.SZ','20251229',10.0,11.0,9.0,10.5,100,1000,0.05,1.0),"
        "('TEST.SZ','20251230',10.5,12.0,10.0,11.0,200,2000,0.03,1.0),"
        "('TEST.SZ','20251231',11.0,11.5,10.5,10.8,150,1500,-0.02,1.0),"
        "('TEST.SZ','20260101',10.8,12.5,10.5,12.0,180,1800,0.10,1.0),"
        "('TEST.SZ','20260102',12.0,13.0,11.5,12.5,220,2200,0.04,1.0)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    build_dwd_weekly_quote(temp_db, ["TEST.SZ"])

    row = temp_db.execute(
        "SELECT open_qfq, high_qfq, low_qfq, close_qfq, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date='20260102'"
    ).fetchone()
    assert row is not None
    assert abs(row[0] - 10.0) < 0.01   # open = 周一(12-29)开盘
    assert abs(row[1] - 13.0) < 0.01   # high = 全周最高
    assert abs(row[2] - 9.0) < 0.01    # low = 全周最低
    assert abs(row[3] - 12.5) < 0.01   # close = 周五收盘
    assert row[4] == 5                 # 整周 5 个交易日聚合进同一 bar


def test_suspension_fill_skipped_when_dwd_already_complete(temp_db):
    """Second full rebuild must not re-detect ODS gaps — only DWD calendar gaps."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) "
        "VALUES ('20260101',1),('20260102',1),('20260103',1)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor) "
        "VALUES ('TEST.SZ','20260101',10.0,1.0),('TEST.SZ','20260103',10.5,1.0)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    assert len(_find_dwd_gap_stocks(temp_db, ["TEST.SZ"])) == 0

    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    rows = temp_db.execute(
        "SELECT trade_date, is_suspended FROM dwd_daily_quote "
        "WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()
    assert len(rows) == 3
    assert rows[1][1] == 1


def test_incremental_daily_inserts_single_day(temp_db):
    """Tail rebuild adds one day without wiping prior DWD rows."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) "
        "VALUES ('20260101',1),('20260102',1),('20260103',1)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor,open,high,low,vol,amount,pct_chg) "
        "VALUES "
        "('TEST.SZ','20260101',10.0,1.0,10.0,10.5,9.5,100,1000,0.01),"
        "('TEST.SZ','20260102',10.2,1.0,10.1,10.3,10.0,110,1100,0.02)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) "
        "VALUES ('TEST.SZ','20260101',1000),('TEST.SZ','20260102',1020)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])

    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor,open,high,low,vol,amount,pct_chg) "
        "VALUES ('TEST.SZ','20260103',10.5,1.0,10.2,10.6,10.1,120,1200,0.03)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) "
        "VALUES ('TEST.SZ','20260103',1050)"
    )
    build_dwd_daily_quote(temp_db, ["TEST.SZ"], incremental_trade_date="20260103")

    rows = temp_db.execute(
        "SELECT trade_date, close_qfq FROM dwd_daily_quote "
        "WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()
    assert len(rows) == 3
    assert rows[-1] == ("20260103", 10.5)


def test_rebuild_dwd_incremental_tail_path(temp_db):
    """rebuild_dwd_incremental uses tail INSERT when adj is stable."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES ('20260101',1),('20260102',1)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor,open,high,low,vol,amount,pct_chg) "
        "VALUES ('TEST.SZ','20260101',10.0,1.0,10.0,10.5,9.5,100,1000,0.01)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) "
        "VALUES ('TEST.SZ','20260101',1000)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])

    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor,open,high,low,vol,amount,pct_chg) "
        "VALUES ('TEST.SZ','20260102',10.2,1.0,10.1,10.3,10.0,110,1100,0.02)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) "
        "VALUES ('TEST.SZ','20260102',1020)"
    )
    res = rebuild_dwd_incremental(temp_db, ["TEST.SZ"], "20260102")
    assert res["daily_quote"] == 1
    n = temp_db.execute(
        "SELECT COUNT(*) FROM dwd_daily_quote WHERE ts_code='TEST.SZ'"
    ).fetchone()[0]
    assert n == 2


def test_incremental_daily_returns_tail_row_count(temp_db):
    """Tail INSERT return value is rows on trade_date, not total history."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) "
        "VALUES ('20260101',1),('20260102',1),('20260103',1)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor,open,high,low,vol,amount,pct_chg) "
        "VALUES "
        "('TEST.SZ','20260101',10.0,1.0,10.0,10.5,9.5,100,1000,0.01),"
        "('TEST.SZ','20260102',10.2,1.0,10.1,10.3,10.0,110,1100,0.02)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) "
        "VALUES ('TEST.SZ','20260101',1000),('TEST.SZ','20260102',1020)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])

    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor,open,high,low,vol,amount,pct_chg) "
        "VALUES ('TEST.SZ','20260103',10.5,1.0,10.2,10.6,10.1,120,1200,0.03)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) "
        "VALUES ('TEST.SZ','20260103',1050)"
    )
    n = build_dwd_daily_quote(temp_db, ["TEST.SZ"], incremental_trade_date="20260103")
    assert n == 1


def test_find_adj_changed_codes_on_latest_day(temp_db):
    """Latest-day adj change triggers full rebuild list."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,adj_factor) "
        "VALUES ('TEST.SZ','20260101',1.0),('TEST.SZ','20260102',2.0)"
    )
    changed = find_adj_changed_codes(temp_db, ["TEST.SZ"], "20260102")
    assert changed == ["TEST.SZ"]


def test_find_qfq_drift_detects_historical_adj_correction(temp_db):
    """Historical adj backfill without latest-day change → qfq drift → full rebuild."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) "
        "VALUES ('20260101',1),('20260102',1),('20260103',1)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor,open,high,low,vol,amount,pct_chg) "
        "VALUES "
        "('TEST.SZ','20260101',10.0,1.0,10.0,10.5,9.5,100,1000,0.01),"
        "('TEST.SZ','20260102',10.2,1.0,10.1,10.3,10.0,110,1100,0.02),"
        "('TEST.SZ','20260103',10.5,1.0,10.2,10.6,10.1,120,1200,0.03)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) "
        "VALUES ('TEST.SZ','20260101',1000),('TEST.SZ','20260102',1020),('TEST.SZ','20260103',1050)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])

    temp_db.execute(
        "UPDATE ods_daily SET adj_factor=2.0 WHERE ts_code='TEST.SZ' AND trade_date='20260101'"
    )
    assert find_adj_changed_codes(temp_db, ["TEST.SZ"], "20260103") == []
    assert find_qfq_drift_codes(temp_db, ["TEST.SZ"]) == ["TEST.SZ"]
    assert find_stocks_needing_full_daily_rebuild(temp_db, ["TEST.SZ"], "20260103") == ["TEST.SZ"]


def test_rebuild_dwd_for_stale_uses_incremental_by_default(temp_db, monkeypatch):
    """rebuild_dwd_for_stale routes to incremental when DWD_INCREMENTAL=1."""
    calls = []
    monkeypatch.setattr(
        "backend.etl.build_dwd.rebuild_dwd_incremental",
        lambda con, codes, d: calls.append(("incr", list(codes), d)) or {"daily_quote": 1},
    )
    monkeypatch.setattr("backend.config.DWD_INCREMENTAL", True)
    rebuild_dwd_for_stale(temp_db, ["A.SZ"], "20260102")
    assert calls == [("incr", ["A.SZ"], "20260102")]


def test_rebuild_dwd_for_stale_uses_full_when_disabled(temp_db, monkeypatch):
    """rebuild_dwd_for_stale routes to rebuild_all_dwd when DWD_INCREMENTAL=0."""
    calls = []
    monkeypatch.setattr(
        "backend.etl.build_dwd.rebuild_all_dwd",
        lambda con, codes: calls.append(list(codes)) or {"daily_quote": 1},
    )
    monkeypatch.setattr("backend.config.DWD_INCREMENTAL", False)
    rebuild_dwd_for_stale(temp_db, ["A.SZ"], "20260102")
    assert calls == [["A.SZ"]]


def test_rebuild_dwd_incremental_full_path_on_adj_drift(temp_db, caplog):
    """rebuild_dwd_incremental qfq UPDATE fixes drift without daily DELETE."""
    import logging

    caplog.set_level(logging.INFO)
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES ('20260101',1),('20260102',1)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor,open,high,low,vol,amount,pct_chg) "
        "VALUES "
        "('TEST.SZ','20260101',10.0,1.0,10.0,10.5,9.5,100,1000,0.01),"
        "('TEST.SZ','20260102',10.2,1.0,10.1,10.3,10.0,110,1100,0.02)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) "
        "VALUES ('TEST.SZ','20260101',1000),('TEST.SZ','20260102',1020)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    temp_db.execute(
        "UPDATE ods_daily SET adj_factor=2.0 WHERE ts_code='TEST.SZ' AND trade_date='20260101'"
    )

    rebuild_dwd_incremental(temp_db, ["TEST.SZ"], "20260102")
    close_qfq = temp_db.execute(
        "SELECT close_qfq FROM dwd_daily_quote "
        "WHERE ts_code='TEST.SZ' AND trade_date='20260101'"
    ).fetchone()[0]
    assert abs(close_qfq - 20.0) < 1e-6
    assert any("dwd.qfq_update" in r.message for r in caplog.records)


def test_rebuild_dwd_incremental_qfq_drift_still_tails_new_day(temp_db, caplog):
    """qfq UPDATE must not skip tail INSERT when DWD max is before trade_date."""
    import logging

    caplog.set_level(logging.INFO)
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES "
        "('20260101',1),('20260102',1),('20260103',1)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor,open,high,low,vol,amount,pct_chg) "
        "VALUES "
        "('TEST.SZ','20260101',10.0,1.0,10.0,10.5,9.5,100,1000,0.01),"
        "('TEST.SZ','20260102',10.1,1.0,10.0,10.2,9.9,110,1100,0.02),"
        "('TEST.SZ','20260103',10.2,1.0,10.1,10.3,10.0,120,1200,0.03)"
    )
    temp_db.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) "
        "VALUES "
        "('TEST.SZ','20260101',1000),"
        "('TEST.SZ','20260102',1010),"
        "('TEST.SZ','20260103',1020)"
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    temp_db.execute(
        "DELETE FROM dwd_daily_quote WHERE ts_code='TEST.SZ' AND trade_date='20260103'"
    )
    temp_db.execute(
        "UPDATE ods_daily SET adj_factor=2.0 WHERE ts_code='TEST.SZ' AND trade_date='20260101'"
    )

    rebuild_dwd_incremental(temp_db, ["TEST.SZ"], "20260103")

    max_td = temp_db.execute(
        "SELECT MAX(trade_date) FROM dwd_daily_quote WHERE ts_code='TEST.SZ'"
    ).fetchone()[0]
    assert max_td == "20260103"
    close_qfq = temp_db.execute(
        "SELECT close_qfq FROM dwd_daily_quote "
        "WHERE ts_code='TEST.SZ' AND trade_date='20260101'"
    ).fetchone()[0]
    assert abs(close_qfq - 20.0) < 1e-6


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


def test_dwd_rebuild_row_count_ignores_changed_codes():
    """_dwd_rebuild_row_count sums only numeric fields, skipping changed_codes."""
    from backend.etl.build_dwd import _dwd_rebuild_row_count

    # Normal case
    result = {
        "daily_quote": 10,
        "weekly_quote": 5,
        "moneyflow": 92,
        "changed_codes": ["000001.SZ", "000002.SZ"],
    }
    assert _dwd_rebuild_row_count(result) == 107

    # Empty changed_codes
    result2 = {
        "daily_quote": 0,
        "weekly_quote": 0,
        "moneyflow": 0,
        "changed_codes": [],
    }
    assert _dwd_rebuild_row_count(result2) == 0

    # All zero (daily_basic gate deferred)
    result3 = {
        "daily_quote": 0,
        "weekly_quote": 0,
        "moneyflow": 92,
        "changed_codes": [],
    }
    assert _dwd_rebuild_row_count(result3) == 92

    # Only changed_codes populated (insert + qfq)
    result4 = {
        "daily_quote": 150,
        "weekly_quote": 10,
        "moneyflow": 150,
        "changed_codes": ["A.SZ", "B.SZ"],
    }
    assert _dwd_rebuild_row_count(result4) == 310
