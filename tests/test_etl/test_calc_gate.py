import duckdb
import pytest

from backend.db.schema import create_all_tables
from backend.etl.calc_gate import assert_calc_date_ready, resolve_effective_calc_date


def test_resolve_effective_calc_date_caps_to_ods_max():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
        "VALUES ('000001.SZ', '20260608', 1, 1, 1, 1, 1, 1)"
    )
    eff = resolve_effective_calc_date(con, requested="20260609")
    assert eff == "20260608"


def test_assert_calc_date_ready_raises_when_ahead_of_ods():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
        "VALUES ('000001.SZ', '20260608', 1, 1, 1, 1, 1, 1)"
    )
    with pytest.raises(ValueError, match="calc_date.*20260609.*ods_max.*20260608"):
        assert_calc_date_ready(con, "20260609", strict=True)


def test_assert_calc_date_ready_allows_when_ods_empty():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    assert_calc_date_ready(con, "20260609", strict=True)


def test_run_calc_rejects_calc_date_ahead_of_ods():
    import backend.etl.orchestrator as orch

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
        "VALUES ('000001.SZ', '20260608', 1, 1, 1, 1, 1, 1)"
    )
    with pytest.raises(ValueError, match="ods_max"):
        orch.run_calc(con, calc_date="20260609", auto_fetch=False)
