import duckdb
from backend.db.schema import create_all_tables
from backend.etl.calc_state import load_calc_state, upsert_calc_state


def test_upsert_and_load_calc_state():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    assert load_calc_state(con, "daily", ["A.SZ"]) == {}
    upsert_calc_state(con, "A.SZ", "daily",
                      last_trade_date="20260605", history_fp="fp1",
                      quote_latest_adj=1.23, calc_date="20260605")
    st = load_calc_state(con, "daily", ["A.SZ", "B.SZ"])
    assert st["A.SZ"]["last_trade_date"] == "20260605"
    assert st["A.SZ"]["history_fp"] == "fp1"
    assert "B.SZ" not in st
    upsert_calc_state(con, "A.SZ", "daily",
                      last_trade_date="20260608", history_fp="fp2",
                      quote_latest_adj=1.24, calc_date="20260608")
    st2 = load_calc_state(con, "daily", ["A.SZ"])
    assert st2["A.SZ"]["history_fp"] == "fp2"
    con.close()
