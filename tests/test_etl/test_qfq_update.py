from backend.etl.build_dwd import (
    build_dwd_daily_quote,
    refresh_qfq_prices,
    find_stocks_needing_qfq_refresh,
)


def _seed_adj_stock(temp_db):
    from backend.db.schema import create_all_tables
    from backend.etl.build_dim import build_dim_stock, build_dim_date

    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES "
        "('20260101',1),('20260102',1),('20260103',1)"
    )
    temp_db.executemany(
        "INSERT INTO ods_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,adj_factor) "
        "VALUES ('TEST.SZ',?,?,?,?,?,?,?,0.01,1.0)",
        [
            ("20260101", 10.0, 10.5, 9.5, 10.0, 100, 1000.0),
            ("20260102", 10.2, 10.3, 10.0, 10.2, 110, 1100.0),
            ("20260103", 10.5, 10.6, 10.1, 10.5, 120, 1200.0),
        ],
    )
    temp_db.executemany(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) VALUES ('TEST.SZ',?,1000)",
        [("20260101",), ("20260102",), ("20260103",)],
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])


def test_refresh_qfq_matches_full_rebuild_after_adj_change(temp_db):
    _seed_adj_stock(temp_db)
    oracle = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq "
        "FROM dwd_daily_quote WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    temp_db.execute(
        "UPDATE ods_daily SET adj_factor=2.0 WHERE ts_code='TEST.SZ' AND trade_date='20260101'"
    )
    assert find_stocks_needing_qfq_refresh(temp_db, ["TEST.SZ"], "20260103") == ["TEST.SZ"]

    refresh_qfq_prices(temp_db, ["TEST.SZ"])
    updated = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq "
        "FROM dwd_daily_quote WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    rebuilt = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq "
        "FROM dwd_daily_quote WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    assert len(updated) == len(rebuilt)
    for u, r in zip(updated, rebuilt):
        assert u[0] == r[0]
        for i in range(1, 5):
            assert abs(float(u[i]) - float(r[i])) < 1e-6
    assert abs(float(updated[0][4]) - 20.0) < 1e-6
