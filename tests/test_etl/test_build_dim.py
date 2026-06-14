from backend.db.schema import create_all_tables


def test_build_dim_stock_exchange_mapping(temp_db):
    create_all_tables(temp_db)
    temp_db.execute("INSERT INTO ods_stock_basic (ts_code,symbol,name,exchange) VALUES ('000001.SZ','000001','平安银行','SZSE')")
    temp_db.execute("INSERT INTO ods_stock_basic (ts_code,symbol,name,exchange) VALUES ('600001.SH','600001','上证测试','SSE')")

    from backend.etl.build_dim import build_dim_stock
    n = build_dim_stock(temp_db)
    assert n == 2
    row = temp_db.execute("SELECT exchange, sector, stock_code FROM dim_stock WHERE ts_code='000001.SZ'").fetchone()
    assert row[0] == "深圳"
    assert row[1] == "主板"
    assert row[2] == "000001"


def test_build_dim_stock_st_detection(temp_db):
    create_all_tables(temp_db)
    temp_db.execute("INSERT INTO ods_stock_basic (ts_code,symbol,name,exchange) VALUES ('000001.SZ','000001','*ST平安','SZSE')")

    from backend.etl.build_dim import build_dim_stock
    build_dim_stock(temp_db)
    row = temp_db.execute("SELECT is_st FROM dim_stock WHERE ts_code='000001.SZ'").fetchone()
    assert row[0] == 1


def test_build_dim_date(temp_db):
    create_all_tables(temp_db)
    temp_db.execute("INSERT INTO ods_trade_cal (cal_date,is_open) VALUES ('20260101',1),('20260102',1)")

    from backend.etl.build_dim import build_dim_date
    n = build_dim_date(temp_db)
    assert n == 2


def test_is_week_end_cross_year_week_single_marker(temp_db):
    """跨年自然周只应有一个 is_week_end=1（落在该周最后交易日）。

    2025-12-29(周一)~2026-01-02(周五) 属同一自然周。错误的 %Y-%W 口径会
    把它切成 2025-52 与 2026-00 两段，产生两个周末标记。
    """
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES "
        "('20251229',1),('20251230',1),('20251231',1),('20260101',1),('20260102',1)"
    )
    from backend.etl.build_dim import build_dim_date
    build_dim_date(temp_db)

    week_ends = [
        r[0] for r in temp_db.execute(
            "SELECT trade_date FROM dim_date WHERE is_week_end = 1 ORDER BY trade_date"
        ).fetchall()
    ]
    assert week_ends == ["20260102"], (
        f"跨年周应只有一个周末标记 20260102，实际为 {week_ends}"
    )


def test_build_dim_concept(temp_db):
    create_all_tables(temp_db)
    temp_db.execute("INSERT INTO dim_stock (ts_code,stock_code,symbol,name,exchange) VALUES ('000001.SZ','000001','000001','平安银行','深圳')")
    temp_db.execute("INSERT INTO ods_concept_detail (concept_name,ts_code) VALUES ('人工智能','000001.SZ')")

    from backend.etl.build_dim import build_dim_concept
    c, m = build_dim_concept(temp_db)
    assert c == 1
    assert m == 1
