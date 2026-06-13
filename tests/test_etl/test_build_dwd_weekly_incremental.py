from backend.etl.dwd_weekly_sql import weekly_insert_select_sql


def test_weekly_sql_contains_date_trunc_partition():
    sql = weekly_insert_select_sql(
        ts_code_filter="AND d.ts_code IN (?)",
        week_filter="",
    )
    assert "date_trunc('week'" in sql
    assert "FIRST_VALUE(d.open_qfq) OVER w" in sql


def _seed_two_weeks(temp_db):
    from backend.db.schema import create_all_tables
    from backend.etl.build_dim import build_dim_stock, build_dim_date
    from backend.etl.build_dwd import build_dwd_daily_quote, build_dwd_weekly_quote

    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES "
        "('20260105',1),('20260106',1),('20260107',1),('20260108',1),('20260109',1),"
        "('20260112',1),('20260113',1)"
    )
    rows = [
        ("20260105", 10.0, 11.0, 9.0, 10.5, 100),
        ("20260106", 10.5, 12.0, 10.0, 11.0, 200),
        ("20260107", 11.0, 11.5, 10.5, 10.8, 150),
        ("20260108", 10.8, 12.5, 10.5, 12.0, 180),
        ("20260109", 12.0, 13.0, 11.5, 12.5, 220),
        ("20260112", 12.5, 13.5, 12.0, 13.0, 210),
    ]
    for td, o, h, l, c, v in rows:
        temp_db.execute(
            "INSERT INTO ods_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,adj_factor) "
            "VALUES ('TEST.SZ',?,?,?,?,?,?,?,0.01,1.0)",
            [td, o, h, l, c, v, float(v * 10)],
        )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    build_dwd_weekly_quote(temp_db, ["TEST.SZ"])


def test_weekly_incremental_preserves_prior_weeks(temp_db):
    from backend.etl.build_dwd import build_dwd_daily_quote, build_dwd_weekly_quote

    _seed_two_weeks(temp_db)
    before = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date <= '20260109' "
        "ORDER BY trade_date"
    ).fetchall()

    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,adj_factor) "
        "VALUES ('TEST.SZ','20260113',13.1,13.8,12.8,13.5,230,2300,0.02,1.0)"
    )
    build_dwd_daily_quote(temp_db, ["TEST.SZ"], incremental_trade_date="20260113")
    build_dwd_weekly_quote(temp_db, ["TEST.SZ"], incremental_trade_date="20260113")

    after_frozen = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date <= '20260109' "
        "ORDER BY trade_date"
    ).fetchall()
    assert before == after_frozen

    row = temp_db.execute(
        "SELECT active_days FROM dwd_weekly_quote "
        "WHERE ts_code='TEST.SZ' AND trade_date='20260113'"
    ).fetchone()
    assert row is not None
    assert row[0] == 2


def test_weekly_incremental_matches_full_oracle(temp_db):
    from backend.etl.build_dwd import build_dwd_daily_quote, build_dwd_weekly_quote

    _seed_two_weeks(temp_db)
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,adj_factor) "
        "VALUES ('TEST.SZ','20260113',13.1,13.8,12.8,13.5,230,2300,0.02,1.0)"
    )
    build_dwd_daily_quote(temp_db, ["TEST.SZ"], incremental_trade_date="20260113")

    build_dwd_weekly_quote(temp_db, ["TEST.SZ"], incremental_trade_date="20260113")
    inc_rows = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date >= '20260112' "
        "ORDER BY trade_date"
    ).fetchall()

    temp_db.execute(
        "DELETE FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date >= '20260112'"
    )
    build_dwd_weekly_quote(temp_db, ["TEST.SZ"])
    full_rows = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date >= '20260112' "
        "ORDER BY trade_date"
    ).fetchall()

    assert len(inc_rows) == len(full_rows)
    for a, b in zip(inc_rows, full_rows):
        assert a[0] == b[0]
        for i in range(1, len(a)):
            assert abs(float(a[i]) - float(b[i])) < 1e-6
