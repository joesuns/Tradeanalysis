import duckdb
from backend.db.schema import create_all_tables, ensure_calc_state_table


def test_ensure_calc_state_table_creates_on_bare_db_and_is_idempotent():
    """Mirrors a production DB created before dws_calc_state existed: the guard
    must create it, and calling twice must not error (CREATE IF NOT EXISTS)."""
    con = duckdb.connect(":memory:")
    exists = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'dws_calc_state'"
    ).fetchone()[0]
    assert exists == 0
    ensure_calc_state_table(con)
    ensure_calc_state_table(con)  # idempotent
    cols = {r[1] for r in con.execute("PRAGMA table_info('dws_calc_state')").fetchall()}
    assert {"ts_code", "freq", "indicator", "history_fp"} <= cols
    con.close()


def test_dws_calc_state_table_exists_with_pk():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info('dws_calc_state')").fetchall()}
    assert {"ts_code", "freq", "indicator", "last_trade_date", "history_fp",
            "quote_latest_adj", "spec_version", "updated_calc_date"} <= cols

    # 同 (ts_code, freq, indicator) 二次 INSERT OR REPLACE → 不增行
    con.execute("INSERT OR REPLACE INTO dws_calc_state "
                "(ts_code, freq, indicator, last_trade_date, history_fp, updated_calc_date) "
                "VALUES ('A.SZ','daily','macd','20260101','fp1','20260101')")
    con.execute("INSERT OR REPLACE INTO dws_calc_state "
                "(ts_code, freq, indicator, last_trade_date, history_fp, updated_calc_date) "
                "VALUES ('A.SZ','daily','macd','20260102','fp2','20260202')")
    n = con.execute(
        "SELECT COUNT(*) FROM dws_calc_state "
        "WHERE ts_code='A.SZ' AND freq='daily' AND indicator='macd'"
    ).fetchone()[0]
    assert n == 1, f"Expected 1 row for same PK, got {n}"

    # 不同 indicator 各占一行
    con.execute("INSERT OR REPLACE INTO dws_calc_state "
                "(ts_code, freq, indicator, last_trade_date, history_fp, updated_calc_date) "
                "VALUES ('A.SZ','daily','ma','20260101','fp3','20260101')")
    total = con.execute(
        "SELECT COUNT(*) FROM dws_calc_state WHERE ts_code='A.SZ' AND freq='daily'"
    ).fetchone()[0]
    assert total == 2, f"Expected 2 rows for macd+ma, got {total}"

    con.close()
