import duckdb

from scripts.health_check import (
    Checker,
    legacy_mature_volume_weekly_fill_sql,
    mature_volume_fill_minimum,
    mature_volume_weekly_fill_sql,
)


def test_checker_expect_min():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (v INTEGER)")
    con.execute("INSERT INTO t VALUES (5)")
    c = Checker(con)
    c.expect_min("five", "SELECT v FROM t", minimum=3)
    assert c.failures == 0
    c.expect_min("too low", "SELECT v FROM t", minimum=10)
    assert c.failures == 1
    con.close()


def test_mature_volume_fill_minimum():
    assert mature_volume_fill_minimum(5000) == 4000
    assert mature_volume_fill_minimum(6000) == 4800
    assert mature_volume_fill_minimum(100) == 80
    assert mature_volume_fill_minimum(0) == 4000


def _setup_section_i_fixtures(con):
    """1 成熟股：历史 50 根有 pct_vol_rank，最新 week-end 为 NULL。"""
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY,
            is_trade_day INTEGER,
            is_week_end INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY,
            list_date TEXT,
            delist_date TEXT
        )
    """)
    con.execute("""
        CREATE TABLE dws_volume_weekly (
            ts_code TEXT,
            trade_date TEXT,
            calc_date TEXT,
            pct_vol_rank DOUBLE,
            zone TEXT
        )
    """)
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    con.execute("""
        CREATE VIEW v_dws_volume_weekly_latest AS
        SELECT * FROM dws_volume_weekly
    """)
    con.execute("INSERT INTO dim_stock VALUES ('OLD.SZ', '20200101', NULL)")

    week_ends = []
    for i in range(130):
        td = f"2024{(i // 4) + 1:02d}{(i % 4) * 7 + 5:02d}"
        week_ends.append(td)
        con.execute(
            "INSERT INTO dim_date VALUES (?, 1, 1)", [td]
        )
    latest_we = week_ends[-1]
    con.execute("INSERT INTO ods_daily VALUES ('OLD.SZ', ?)", [latest_we])

    for td in week_ends[:-1]:
        con.execute(
            "INSERT INTO dws_volume_weekly VALUES (?, ?, '20260605', 50.0, 'normal')",
            ["OLD.SZ", td],
        )
    con.execute(
        "INSERT INTO dws_volume_weekly VALUES (?, ?, '20260605', NULL, NULL)",
        ["OLD.SZ", latest_we],
    )
    return latest_we


def test_mature_volume_sql_latest_cross_section_not_history_rows():
    """旧 SQL 计全历史行；新 SQL 只看最新 week-end 截面。"""
    con = duckdb.connect(":memory:")
    latest_we = _setup_section_i_fixtures(con)

    legacy = con.execute(
        legacy_mature_volume_weekly_fill_sql("pct_vol_rank")
    ).fetchone()[0]
    current = con.execute(
        mature_volume_weekly_fill_sql("pct_vol_rank")
    ).fetchone()[0]

    assert legacy == 129
    assert current == 0

    c = Checker(con)
    c.expect_min(
        "pct_vol_rank latest",
        mature_volume_weekly_fill_sql("pct_vol_rank"),
        minimum=1,
    )
    assert c.failures == 1

    con.execute(
        "UPDATE dws_volume_weekly SET pct_vol_rank=60.0, zone='high' "
        "WHERE ts_code='OLD.SZ' AND trade_date=?",
        [latest_we],
    )
    fixed = con.execute(
        mature_volume_weekly_fill_sql("pct_vol_rank")
    ).fetchone()[0]
    assert fixed == 1
    con.close()


def test_latest_week_end_anchors_to_ods_max_not_future_calendar():
    """_latest_week_end_sql 不得取 dim_date 中超出 ODS 的未来 weekend。"""
    from scripts.health_check import _latest_week_end_sql

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    for td in ("20260102", "20260109", "20260605", "20261231"):
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
    con.execute("INSERT INTO ods_daily VALUES ('X.SZ', '20260605')")

    latest = con.execute(_latest_week_end_sql()).fetchone()[0]
    assert latest == "20260605"
    con.close()
