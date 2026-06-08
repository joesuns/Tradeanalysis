"""Tests for batched per-stock quote loading (B1)."""
import duckdb
import pandas as pd
from backend.etl.base import load_quote_groups


def _make_daily(con):
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT,
            close_qfq REAL, vol REAL, is_suspended INTEGER
        )
    """)


def test_load_quote_groups_daily_groups_and_filters_suspended():
    con = duckdb.connect(":memory:")
    _make_daily(con)
    rows = [
        ("A.SZ", "20260101", 10.0, 100, 0),
        ("A.SZ", "20260102", 11.0, 110, 0),
        ("A.SZ", "20260103", 99.0, 999, 1),   # suspended → filtered out
        ("B.SZ", "20260101", 20.0, 200, 0),
    ]
    for r in rows:
        con.execute("INSERT INTO dwd_daily_quote VALUES (?,?,?,?,?)", r)

    groups = load_quote_groups(con, "dwd_daily_quote", "daily",
                               ["trade_date", "close_qfq", "vol"],
                               ["A.SZ", "B.SZ"])
    assert set(groups.keys()) == {"A.SZ", "B.SZ"}
    a = groups["A.SZ"]
    assert list(a.columns) == ["trade_date", "close_qfq", "vol"]
    assert list(a["trade_date"]) == ["20260101", "20260102"]  # suspended dropped
    assert list(groups["B.SZ"]["trade_date"]) == ["20260101"]
    con.close()


def test_load_quote_groups_matches_legacy_per_stock_query():
    """Batched result must equal the legacy per-stock SELECT, row for row."""
    con = duckdb.connect(":memory:")
    _make_daily(con)
    import random
    random.seed(1)
    codes = ["A.SZ", "B.SZ", "C.SZ"]
    for code in codes:
        for i in range(20):
            con.execute("INSERT INTO dwd_daily_quote VALUES (?,?,?,?,0)",
                        (code, f"202601{i:02d}", 10 + random.random(), 100 + i))

    groups = load_quote_groups(con, "dwd_daily_quote", "daily",
                               ["trade_date", "close_qfq"], codes)
    for code in codes:
        legacy = con.execute(
            "SELECT trade_date, close_qfq FROM dwd_daily_quote "
            "WHERE ts_code = ? AND is_suspended = 0 ORDER BY trade_date",
            (code,)).df()
        pd.testing.assert_frame_equal(groups[code], legacy)
    con.close()


def test_load_quote_groups_missing_stock_absent():
    con = duckdb.connect(":memory:")
    _make_daily(con)
    con.execute("INSERT INTO dwd_daily_quote VALUES ('A.SZ','20260101',10,100,0)")
    groups = load_quote_groups(con, "dwd_daily_quote", "daily",
                               ["trade_date", "close_qfq"], ["A.SZ", "GHOST.SZ"])
    assert "GHOST.SZ" not in groups
    assert "A.SZ" in groups
    con.close()


def test_load_quote_groups_weekly_uses_is_week_end():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL
        )
    """)
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER,
            is_month_end INTEGER, is_year_end INTEGER, year INTEGER,
            quarter INTEGER, month INTEGER, week_of_year INTEGER
        )
    """)
    con.execute("INSERT INTO dwd_weekly_quote VALUES ('A.SZ','20260102',10)")
    con.execute("INSERT INTO dwd_weekly_quote VALUES ('A.SZ','20260109',11)")
    con.execute("INSERT INTO dim_date VALUES ('20260102',1,1,0,0,2026,1,1,1)")
    con.execute("INSERT INTO dim_date VALUES ('20260109',1,0,0,0,2026,1,1,2)")  # not week-end

    groups = load_quote_groups(con, "dwd_weekly_quote", "weekly",
                               ["trade_date", "close_qfq"], ["A.SZ"])
    assert list(groups["A.SZ"]["trade_date"]) == ["20260102"]
    con.close()
