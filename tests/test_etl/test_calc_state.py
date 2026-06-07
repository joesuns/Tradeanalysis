import duckdb
from backend.db.schema import create_all_tables
from backend.etl.calc_state import load_calc_state, upsert_calc_state


def test_upsert_and_load_calc_state():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    assert load_calc_state(con, "daily", "macd", ["A.SZ"]) == {}
    upsert_calc_state(con, "A.SZ", "daily", "macd",
                      last_trade_date="20260605", history_fp="fp1",
                      quote_latest_adj=1.23, calc_date="20260605")
    st = load_calc_state(con, "daily", "macd", ["A.SZ", "B.SZ"])
    assert st["A.SZ"]["last_trade_date"] == "20260605"
    assert st["A.SZ"]["history_fp"] == "fp1"
    assert "B.SZ" not in st

    # 同 indicator 覆盖：更新后应仍只有 1 行
    upsert_calc_state(con, "A.SZ", "daily", "macd",
                      last_trade_date="20260608", history_fp="fp2",
                      quote_latest_adj=1.24, calc_date="20260608")
    st2 = load_calc_state(con, "daily", "macd", ["A.SZ"])
    assert st2["A.SZ"]["history_fp"] == "fp2"

    # 不同 indicator 互不覆盖
    upsert_calc_state(con, "A.SZ", "daily", "ma",
                      last_trade_date="20260608", history_fp="fp_ma",
                      calc_date="20260608")
    st_macd = load_calc_state(con, "daily", "macd", ["A.SZ"])
    st_ma = load_calc_state(con, "daily", "ma", ["A.SZ"])
    assert st_macd["A.SZ"]["history_fp"] == "fp2"
    assert st_ma["A.SZ"]["history_fp"] == "fp_ma"

    # 验证实际行数：macd + ma = 2 行
    n = con.execute(
        "SELECT COUNT(*) FROM dws_calc_state WHERE ts_code='A.SZ' AND freq='daily'"
    ).fetchone()[0]
    assert n == 2, f"Expected 2 indicator rows, got {n}"

    con.close()
