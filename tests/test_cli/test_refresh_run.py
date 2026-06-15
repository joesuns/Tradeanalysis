"""Tests for cli refresh (Wave 3 R1)."""
import argparse

import pytest

from backend.etl.refresh_pipeline import (
    REFRESH_CONFIRM_ROUTE_THRESHOLD,
    build_refresh_full_groups,
    estimate_refresh_scope,
    parse_indicator_filter,
    resolve_refresh_routes,
    run_refresh_calc,
    run_refresh_pipeline,
)


def test_resolve_refresh_routes_all_12():
    routes = resolve_refresh_routes(None)
    assert len(routes) == 12
    assert ("ma", "daily") in routes
    assert ("ma", "weekly") in routes
    assert ("dde", "weekly") in routes


def test_resolve_refresh_routes_ma_only():
    routes = resolve_refresh_routes(["ma"])
    assert routes == [("ma", "daily"), ("ma", "weekly")]


def test_parse_indicator_filter_invalid():
    with pytest.raises(ValueError, match="Unknown indicator"):
        parse_indicator_filter("ma,not_real")


def test_build_refresh_full_groups_excludes_bse_dde():
    codes = ["000001.SZ", "830001.BJ"]
    groups = build_refresh_full_groups(codes, [("ma", "daily"), ("dde", "daily")])
    assert groups[("ma", "daily")] == codes
    assert groups[("dde", "daily")] == ["000001.SZ"]


def test_refresh_dry_run_no_side_effects(monkeypatch):
    fetch_called = []
    calc_called = []

    monkeypatch.setattr(
        "backend.fetch.ods_daily.get_all_active_codes",
        lambda _c: ["A.SZ", "B.SZ"],
    )
    monkeypatch.setattr(
        "backend.fetch.ods_daily.fetch_by_date_range_parallel",
        lambda *a, **k: fetch_called.append(True),
    )
    monkeypatch.setattr(
        "backend.etl.refresh_pipeline.run_refresh_calc",
        lambda *a, **k: calc_called.append(True) or {},
    )

    import duckdb
    from backend.db.schema import create_all_tables

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    summary = run_refresh_pipeline(
        con, "20260612", dry_run=True,
    )
    con.close()

    assert summary["dry_run"] is True
    assert summary["n_stocks"] == 2
    assert summary["n_routes"] == 12
    assert summary["est_route_count"] == 2 * 12
    assert fetch_called == []
    assert calc_called == []


def test_refresh_requires_confirm_for_large_scope(monkeypatch):
    big = ["C%04d.SZ" % i for i in range(6000)]
    monkeypatch.setattr(
        "backend.fetch.ods_daily.get_all_active_codes",
        lambda _c: big,
    )

    import duckdb
    from backend.db.schema import create_all_tables

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    scope = estimate_refresh_scope(["20260612"], big, resolve_refresh_routes(None))
    assert scope["est_route_count"] > REFRESH_CONFIRM_ROUTE_THRESHOLD

    with pytest.raises(ValueError, match="--confirm"):
        run_refresh_pipeline(con, "20260612", confirmed=False)
    con.close()


def test_run_refresh_calc_ma_only_groups(monkeypatch):
    captured = {}

    def fake_batch_full(con, calc_date, full_groups, batch_ctx):
        captured["full_groups"] = full_groups
        return {
            "batch_full_items": 2,
            "full_by_indicator": {"ma_daily": 2, "ma_weekly": 2},
            "agg_by_key": {},
            "completed_keys": set(),
        }

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
    monkeypatch.setattr(
        "backend.etl.calc_batch_append.run_batch_full_phase",
        fake_batch_full,
    )
    monkeypatch.setattr(
        "backend.etl.orchestrator._filter_delisted",
        lambda con, codes, d: (codes, {}),
    )
    monkeypatch.setattr(
        "backend.etl.error_handler.log_etl_start",
        lambda *a, **k: ("lid", 0.0),
    )
    monkeypatch.setattr(
        "backend.etl.error_handler.log_etl_end",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "backend.db.connection.run_checkpoint",
        lambda *a, **k: None,
    )

    import duckdb
    from backend.db.schema import create_all_tables

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    summary = run_refresh_calc(
        con, "20260612", ["A.SZ", "B.SZ"], indicator_filter=["ma"],
    )
    con.close()

    assert set(captured["full_groups"].keys()) == {("ma", "daily"), ("ma", "weekly")}
    assert captured["full_groups"][("ma", "daily")] == ["A.SZ", "B.SZ"]
    assert summary["routes"] == ["ma_daily", "ma_weekly"]
    assert summary["force_scope"] is True


def test_cmd_refresh_dry_run_cli(monkeypatch):
    from backend import cli

    printed = []

    class FakeCon:
        def execute(self, *args, **kwargs):
            return type("R", (), {"fetchone": lambda self: ("20260612",)})()

        def close(self):
            pass

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr(
        "backend.etl.refresh_pipeline.run_refresh_pipeline",
        lambda *a, **k: {
            "dry_run": True,
            "dates": ["20260612"],
            "n_stocks": 100,
            "indicators": ["ma"],
            "est_route_count": 200,
        },
    )
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))

    args = argparse.Namespace(
        date="20260612",
        ts_code=None,
        indicator="ma",
        export=False,
        dry_run=True,
        confirm=False,
        output=None,
        db_path=None,
    )
    cli.cmd_refresh(args)
    assert any("dry-run" in line for line in printed)


def test_run_refresh_calc_all_12_routes(monkeypatch):
    captured = {}

    def fake_batch_full(con, calc_date, full_groups, batch_ctx):
        captured["n_groups"] = len(full_groups)
        return {
            "batch_full_items": 12,
            "full_by_indicator": {f"k_{i}": 1 for i in range(12)},
            "agg_by_key": {},
            "completed_keys": set(),
        }

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
    monkeypatch.setattr(
        "backend.etl.calc_batch_append.run_batch_full_phase",
        fake_batch_full,
    )
    monkeypatch.setattr(
        "backend.etl.orchestrator._filter_delisted",
        lambda con, codes, d: (codes, {}),
    )
    monkeypatch.setattr(
        "backend.etl.error_handler.log_etl_start",
        lambda *a, **k: ("lid", 0.0),
    )
    monkeypatch.setattr(
        "backend.etl.error_handler.log_etl_end",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "backend.db.connection.run_checkpoint",
        lambda *a, **k: None,
    )

    import duckdb
    from backend.db.schema import create_all_tables

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    summary = run_refresh_calc(con, "20260612", ["A.SZ"], indicator_filter=None)
    con.close()

    assert captured["n_groups"] == 12
    assert len(summary["routes"]) == 12
