import duckdb

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.calc_state_backfill import backfill_calc_state, find_missing_state_keys


def test_find_missing_state_keys_empty_when_all_present():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS
    for ind, freq, _, _, _ in CALC_ROUTE_SPECS:
        con.execute("""
            INSERT INTO dws_calc_state
                (ts_code, freq, indicator, last_trade_date, history_fp, updated_calc_date)
            VALUES ('A.SZ', ?, ?, '20260601', 'fp', '20260605')
        """, [freq, ind])
    missing = find_missing_state_keys(con, ["A.SZ"])
    assert missing == {}


def test_find_missing_state_keys_detects_gaps():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    missing = find_missing_state_keys(con, ["A.SZ"])
    assert "A.SZ" in missing
    assert ("macd", "daily") in missing["A.SZ"]
    assert len(missing["A.SZ"]) == 12


def test_backfill_calc_state_skips_when_no_gaps(monkeypatch):
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(
        "backend.etl.orchestrator.calc_stock_pipeline_selective", boom,
    )
    for ind, freq, _, _, _ in __import__(
        "backend.etl.calc_indicators", fromlist=["CALC_ROUTE_SPECS"]
    ).CALC_ROUTE_SPECS:
        con.execute("""
            INSERT INTO dws_calc_state
                (ts_code, freq, indicator, last_trade_date, history_fp, updated_calc_date)
            VALUES ('A.SZ', ?, ?, '20260601', 'fp', '20260605')
        """, [freq, ind])

    result = backfill_calc_state(con, ["A.SZ"], "20260605")
    assert result["stocks"] == 0
    assert called["n"] == 0
