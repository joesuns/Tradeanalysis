import logging

from backend.etl.build_dwd import rebuild_all_dwd


def test_rebuild_all_dwd_logs_substeps(db_with_schema, caplog):
    con = db_with_schema
    con.execute("""
        INSERT INTO ods_daily
        (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor, fetched_at)
        VALUES ('000001.SZ','20260101',1,1,1,1,1,1,1,1, now())
    """)
    con.execute(
        "INSERT INTO dim_stock (ts_code, symbol, name, exchange, list_date) "
        "VALUES ('000001.SZ','000001','测试','SZSE','20200101')"
    )
    con.execute("INSERT INTO dim_date (trade_date, is_trade_day) VALUES ('20260101', 1)")

    with caplog.at_level(logging.INFO, logger="backend.etl.build_dwd"):
        rebuild_all_dwd(con, ["000001.SZ"])

    msgs = [r.getMessage() for r in caplog.records]
    assert any("progress dwd.daily_quote:" in m for m in msgs)
    assert any("progress dwd.weekly_quote:" in m for m in msgs)
    assert any("progress dwd.moneyflow:" in m for m in msgs)
    assert any("progress dwd.rebuild:" in m for m in msgs)
