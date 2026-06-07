import duckdb
from backend.db.schema import create_all_tables


def test_dws_calc_state_table_exists_with_pk():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info('dws_calc_state')").fetchall()}
    assert {"ts_code", "freq", "last_trade_date", "history_fp",
            "quote_latest_adj", "spec_version", "updated_calc_date"} <= cols
    con.execute("INSERT OR REPLACE INTO dws_calc_state "
                "(ts_code, freq, last_trade_date, history_fp, updated_calc_date) "
                "VALUES ('A.SZ','daily','20260101','fp1','20260101')")
    con.execute("INSERT OR REPLACE INTO dws_calc_state "
                "(ts_code, freq, last_trade_date, history_fp, updated_calc_date) "
                "VALUES ('A.SZ','daily','20260102','fp2','20260202')")
    n = con.execute("SELECT COUNT(*) FROM dws_calc_state WHERE ts_code='A.SZ'").fetchone()[0]
    assert n == 1
    con.close()
