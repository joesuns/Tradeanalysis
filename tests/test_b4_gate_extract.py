import duckdb

from backend.b4_gate.extract import resolve_week_end


def test_resolve_week_end_before_analysis_date():
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE dim_date(trade_date VARCHAR, is_trade_day INTEGER, is_week_end INTEGER);
        INSERT INTO dim_date VALUES
          ('20260602',1,1),('20260603',1,0),('20260604',1,0),('20260605',1,1);
    """)
    assert resolve_week_end(con, "20260604") == "20260602"
    assert resolve_week_end(con, "20260605") == "20260605"
