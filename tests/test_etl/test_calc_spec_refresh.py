"""calc --refresh-spec narrow FULL for stale spec_version rows."""
import duckdb
import pytest

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.calc_spec_refresh import (
    cmd_refresh_spec,
    run_auto_spec_refresh_if_needed,
    run_refresh_spec,
)
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


def test_run_refresh_spec_dry_run_no_writes(monkeypatch):
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "ma", "20260612", "fp", "20260612",
        spec_version="v1",
    )

    batch_called = []

    def fake_batch_full(*args, **kwargs):
        batch_called.append(True)
        return {}

    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh.run_batch_full_phase", fake_batch_full,
    )

    summary = run_refresh_spec(con, "20260612", ["ma"], dry_run=True)
    assert summary.get("dry_run") is True
    assert summary.get("refreshed", 0) == 0
    assert summary.get("stale_groups")
    assert batch_called == []
    con.close()


def test_run_refresh_spec_detects_dws_stale_when_state_fresh(monkeypatch):
    """refresh-spec uses merged stale (state ∪ DWS), not state alone."""
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "dde", "20260616", "fp", "20260616",
        spec_version="v3",
    )
    con.execute(
        """
        INSERT INTO dws_dde_daily (
            ts_code, trade_date, ddx, ddx2, divergence, trend,
            trend_strength, alert, calc_date, input_fingerprint, spec_version
        ) VALUES ('000001.SZ', '20260616', 0.01, 0.02, NULL, 'flat', 0.0, NULL, '20260616', 'fp', 'v1')
        """
    )

    calls = []

    def fake_execute(con, calc_date, stale_groups, dry_run=False):
        calls.append(stale_groups)
        return {"refreshed": 1, "full_by_indicator": {"dde_daily": 1}, "calculated": 1}

    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh._execute_spec_stale_batch_full", fake_execute,
    )
    summary = run_refresh_spec(con, "20260616", ["dde"], ts_codes=["000001.SZ"])
    assert summary["refreshed"] == 1
    assert calls[0].get(("dde", "daily")) == ["000001.SZ"]
    con.close()


def test_auto_spec_refresh_respects_indicator_filter(monkeypatch):
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "dde", "20260616", "fp", "20260616",
        spec_version="v1",
    )
    upsert_calc_state(
        con, "000001.SZ", "daily", "ma", "20260616", "fp", "20260616",
        spec_version="v2",
    )
    monkeypatch.setattr("backend.config.CALC_AUTO_SPEC_REFRESH", True)
    calls = []

    def fake_execute(con, calc_date, stale_groups):
        calls.append(stale_groups)
        return {"refreshed": 1, "full_by_indicator": {"dde_daily": 1}, "calculated": 1}

    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh._execute_spec_stale_batch_full", fake_execute,
    )
    run_auto_spec_refresh_if_needed(
        con, "20260616", ["000001.SZ"], indicator_filter=["dde"],
    )
    merged_inds = {ind for ind, _ in calls[0]}
    assert merged_inds == {"dde"}
    con.close()


def test_auto_spec_refresh_includes_state_stale_outside_filter(monkeypatch):
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "ma", "20260616", "fp", "20260616",
        spec_version="v1",
    )
    monkeypatch.setattr("backend.config.CALC_AUTO_SPEC_REFRESH", True)
    calls = []

    def fake_execute(con, calc_date, stale_groups):
        calls.append(stale_groups)
        return {"refreshed": 1, "full_by_indicator": {"ma_daily": 1}, "calculated": 1}

    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh._execute_spec_stale_batch_full", fake_execute,
    )
    run_auto_spec_refresh_if_needed(
        con, "20260616", ["000001.SZ"], indicator_filter=["dde"],
    )
    assert ("ma", "daily") in calls[0]
    con.close()


def test_auto_spec_refresh_warns_when_batch_full_disabled(monkeypatch, caplog):
    import logging

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "ma", "20260616", "fp", "20260616",
        spec_version="v1",
    )
    monkeypatch.setattr("backend.config.CALC_AUTO_SPEC_REFRESH", True)
    monkeypatch.setattr("backend.config.CALC_BATCH_FULL", False)
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh._execute_spec_stale_batch_full",
        lambda *a, **k: {"refreshed": 0, "full_by_indicator": {}, "calculated": 0},
    )
    run_auto_spec_refresh_if_needed(con, "20260616", ["000001.SZ"])
    assert any("CALC_BATCH_FULL=0" in r.getMessage() for r in caplog.records)
    con.close()


def test_run_auto_spec_refresh_skips_when_fresh(monkeypatch):
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    monkeypatch.setattr("backend.config.CALC_AUTO_SPEC_REFRESH", True)
    summary = run_auto_spec_refresh_if_needed(con, "20260616", ["000001.SZ"])
    assert summary["skipped"] is True
    assert summary["reason"] == "fresh"
    con.close()


def test_run_auto_spec_refresh_invokes_batch_full(monkeypatch):
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "ma", "20260616", "fp", "20260616",
        spec_version="v1",
    )
    monkeypatch.setattr("backend.config.CALC_AUTO_SPEC_REFRESH", True)
    calls = []

    def fake_execute(con, calc_date, stale_groups):
        calls.append(stale_groups)
        return {"refreshed": 1, "full_by_indicator": {"ma_daily": 1}, "calculated": 1}

    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh._execute_spec_stale_batch_full", fake_execute,
    )
    summary = run_auto_spec_refresh_if_needed(con, "20260616", ["000001.SZ"])
    assert summary["skipped"] is False
    assert summary["refreshed"] == 1
    assert ("ma", "daily") in calls[0]
    con.close()
