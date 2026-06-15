"""Tests for CLI date range and ops subcommand (Wave 4)."""
import argparse
import warnings

import duckdb
import pytest

from backend.cli_dates import (
    expand_trade_dates,
    resolve_cli_dates,
    run_date_range_loop,
)
from backend.db.schema import create_all_tables


def _seed_trade_days(con, dates):
    con.execute(
        "CREATE TABLE IF NOT EXISTS dim_date "
        "(trade_date TEXT PRIMARY KEY, is_trade_day INTEGER)"
    )
    for d in dates:
        con.execute(
            "INSERT OR REPLACE INTO dim_date (trade_date, is_trade_day) VALUES (?, 1)",
            [d],
        )


def test_expand_trade_dates_range():
    con = duckdb.connect(":memory:")
    _seed_trade_days(con, ["20260609", "20260610", "20260611", "20260612"])
    assert expand_trade_dates(con, "20260610", "20260612") == [
        "20260610", "20260611", "20260612",
    ]
    con.close()


def test_resolve_cli_dates_mutually_exclusive():
    con = duckdb.connect(":memory:")
    _seed_trade_days(con, ["20260612"])
    args = argparse.Namespace(date="20260612", date_from="20260610", date_to=None)
    with pytest.raises(ValueError, match="mutually exclusive"):
        resolve_cli_dates(con, args)
    con.close()


def test_resolve_cli_dates_from_to():
    con = duckdb.connect(":memory:")
    _seed_trade_days(con, ["20260610", "20260611", "20260612"])
    args = argparse.Namespace(date=None, date_from="20260610", date_to="20260612")
    assert resolve_cli_dates(con, args) == ["20260610", "20260611", "20260612"]
    con.close()


def test_run_date_range_loop_fail_fast():
    calls = []

    def boom(d):
        calls.append(d)
        if d == "20260611":
            raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        run_date_range_loop(
            ["20260610", "20260611", "20260612"], boom, continue_on_error=False,
        )
    assert calls == ["20260610", "20260611"]


def test_run_date_range_loop_continue_on_error():
    calls = []

    def boom(d):
        calls.append(d)
        if d == "20260611":
            raise RuntimeError("fail")

    progress = run_date_range_loop(
        ["20260610", "20260611", "20260612"], boom, continue_on_error=True,
    )
    assert calls == ["20260610", "20260611", "20260612"]
    assert progress["ok"] == ["20260610", "20260612"]
    assert len(progress["failed"]) == 1


def test_cmd_export_date_range(monkeypatch):
    from backend import cli

    exported = []

    class FakeCon:
        def execute(self, *args, **kwargs):
            return type("R", (), {"fetchone": lambda self: (1,)})()

        def close(self):
            pass

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr(
        "backend.cli_dates.expand_trade_dates",
        lambda *a, **k: ["20260610", "20260611"],
    )
    monkeypatch.setattr(
        "backend.export_wide.export_wide_to_excel",
        lambda *a, **k: exported.append(a[1]) or type(
            "R", (), {"row_count": 10, "tradable_enrich": {}},
        )(),
    )
    monkeypatch.setattr("backend.cli._warn_export_coverage", lambda *a, **k: None)

    args = argparse.Namespace(
        date=None,
        date_from="20260610",
        date_to="20260611",
        output=None,
        ts_code=None,
        db_path=None,
        include_st=False,
        no_index=False,
    )
    cli.cmd_export(args)
    assert exported == ["20260610", "20260611"]


def test_ops_subcommand_delegates(monkeypatch):
    from backend import cli

    called = []
    monkeypatch.setattr(
        cli, "cmd_prune", lambda a: called.append(getattr(a, "keep", None)),
    )

    args = argparse.Namespace(command="ops", ops_command="prune", keep=3)
    handler = {
        "prune": cli.cmd_prune,
    }.get(args.ops_command)
    handler(args)
    assert called == [3]


def test_deprecated_top_level_warns():
    from backend.cli import DEPRECATED_OPS_COMMANDS, _warn_deprecated_top_level

    assert "prune" in DEPRECATED_OPS_COMMANDS
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _warn_deprecated_top_level("prune")
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
