"""Integration: run_calc step 3a auto spec refresh hook."""
import duckdb
from unittest.mock import patch

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.calc_state import upsert_calc_state


def test_run_calc_passes_indicator_filter_to_auto_refresh(monkeypatch):
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "ma", "20260616", "fp", "20260616",
        spec_version="v1",
    )

    captured = {}

    def fake_auto(con, calc_date, ts_codes, indicator_filter=None):
        captured["filter"] = indicator_filter
        captured["codes"] = ts_codes
        return {"skipped": False, "refreshed": 1, "full_by_indicator": {"ma_daily": 1}}

    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh.run_auto_spec_refresh_if_needed",
        fake_auto,
    )
    monkeypatch.setattr(
        "backend.etl.orchestrator._should_skip_calc_idempotent",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "backend.etl.orchestrator.check_data_completeness",
        lambda *a, **k: {"ok": ["000001.SZ"], "missing": {}, "weekly_fetch": {}},
    )
    monkeypatch.setattr("backend.config.CALC_AUTO_SPEC_REFRESH", True)
    monkeypatch.setattr("backend.config.CALC_BATCH_APPEND", False)
    monkeypatch.setattr(
        "backend.etl.calc_spec_gate.count_dws_spec_stale_on_trade_date",
        lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.orchestrator.resolve_recalc_start",
        lambda *a, **k: "20250601",
    )
    monkeypatch.setattr("backend.etl.orchestrator._calc_stock_chunk", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.orchestrator.resolve_calc_workers", lambda: 1)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start", lambda *a: (1, 0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.orchestrator.run_checkpoint", lambda *a: None)

    from backend.etl.orchestrator import run_calc

    run_calc(
        con, ts_codes=["000001.SZ"], auto_fetch=False,
        calc_date="20260616", skip_stale_fetch=True,
        indicator_filter=["dde"],
    )
    assert captured.get("filter") == ["dde"]
    con.close()
