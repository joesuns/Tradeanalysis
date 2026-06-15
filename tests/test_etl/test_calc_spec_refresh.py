"""calc --refresh-spec narrow FULL for stale spec_version rows."""
import duckdb
import pytest

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.calc_spec_refresh import cmd_refresh_spec, run_refresh_spec
from backend.etl.calc_state import upsert_calc_state


def test_run_refresh_spec_no_stale_returns_zero():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "ma", "20260612", "fp", "20260612",
        spec_version="v2",
    )
    summary = run_refresh_spec(con, "20260612", ["ma"])
    assert summary["refreshed"] == 0
    assert summary["full_by_indicator"] == {}
    con.close()


def test_run_refresh_spec_invokes_batch_full_when_stale(monkeypatch):
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "ma", "20260612", "fp", "20260612",
        spec_version="v1",
    )

    calls = []

    def fake_batch_full(con, calc_date, stale_groups, batch_ctx):
        calls.append({"calc_date": calc_date, "groups": stale_groups})
        return {
            "batch_full_items": 1,
            "full_by_indicator": {"ma_daily": 1},
            "agg_by_key": {},
        }

    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh.run_batch_full_phase", fake_batch_full,
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_quote_tails",
        lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_dde_tails",
        lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.load_calc_state_batch",
        lambda *a, **k: {},
    )

    summary = run_refresh_spec(con, "20260612", ["ma"], ts_codes=["000001.SZ"])
    assert summary["refreshed"] == 1
    assert summary["full_by_indicator"] == {"ma_daily": 1}
    assert len(calls) == 1
    assert calls[0]["groups"].get(("ma", "daily")) == ["000001.SZ"]
    con.close()


def test_cmd_refresh_spec_logs_etl_step(monkeypatch):
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)

    logged = []

    def fake_log_start(con, step):
        logged.append(("start", step))
        return ("lid", 0.0)

    def fake_log_end(con, lid, step, t0, status, **kw):
        logged.append(("end", step, status, kw.get("data_completeness")))

    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh.log_etl_start", fake_log_start,
    )
    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh.log_etl_end", fake_log_end,
    )
    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh.run_refresh_spec",
        lambda *a, **k: {"refreshed": 0, "full_by_indicator": {}, "calculated": 0},
    )

    cmd_refresh_spec(con, "20260612", "ma")
    assert logged[0] == ("start", "calc_refresh_spec")
    assert logged[1][0:3] == ("end", "calc_refresh_spec", "success")
    assert logged[1][3]["calc_date"] == "20260612"
    con.close()


def test_cmd_refresh_spec_rejects_empty_indicator_list():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    with pytest.raises(ValueError, match="at least one indicator"):
        cmd_refresh_spec(con, "20260612", "  , ")
    con.close()
