import subprocess
import sys
import os


def test_cli_check_runs():
    """CLI 'check' command should run and produce DuckDB output."""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "check"],
        capture_output=True, text=True, env=env,
    )
    # 0 = ok, 1 = possible API error with test token (expected)
    assert result.returncode in (0, 1)
    assert "DuckDB" in (result.stdout + result.stderr)


def test_cli_help():
    """CLI --help should print available commands."""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "check" in result.stdout


def test_cli_help_subcommand():
    """CLI subcommand --help should show subcommand-specific options."""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "fetch", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "--ts-code" in result.stdout


# ── date resolution ──

import pytest
from backend.cli import _resolve_trade_date, _ensure_trade_date


def test_resolve_trade_date_with_date():
    """指定日期 → 直接返回。"""
    assert _resolve_trade_date(None, "20260604") == "20260604"


def test_resolve_trade_date_default_today():
    """不指定 → 用今天日期。"""
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    result = _resolve_trade_date(None, None)
    assert result == today


def test_ensure_trade_date_is_trade_day():
    """20260604 是周四，应该原样返回。"""
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_date (trade_date TEXT PRIMARY KEY, is_trade_day INTEGER)
    """)
    for d in ["20260601", "20260602", "20260603", "20260604", "20260605"]:
        con.execute("INSERT INTO dim_date VALUES (?, 1)", (d,))
    result = _ensure_trade_date(con, "20260604")
    assert result == "20260604"
    con.close()


def test_ensure_trade_date_weekend_rollback():
    """20260607 是周日，应回退到最近交易日。"""
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_date (trade_date TEXT PRIMARY KEY, is_trade_day INTEGER)
    """)
    for d in ["20260601", "20260602", "20260603", "20260604", "20260605"]:
        con.execute("INSERT INTO dim_date VALUES (?, 1)", (d,))
    result = _ensure_trade_date(con, "20260607")
    assert result == "20260605", f"Expected 20260605, got {result}"
    con.close()


# ── parameter cleanup ──


def test_cli_fetch_help_omits_all_param():
    """fetch help should NOT mention --all (removed)."""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "fetch", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "--all" not in result.stdout


def test_cli_calc_help_omits_no_auto_fetch():
    """calc help should NOT mention --no-auto-fetch (removed)."""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "calc", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "--no-auto-fetch" not in result.stdout


def test_cli_export_help_omits_recalc():
    """export help should NOT mention --recalc or --no-auto-fetch (removed)."""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "export", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "--recalc" not in result.stdout
    assert "--no-auto-fetch" not in result.stdout


# ── run command ──


def test_cli_run_help_shows():
    """run 命令应有帮助信息。"""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "run", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "--date" in result.stdout
    assert "--ts-code" in result.stdout


def test_cli_help_lists_run():
    """主 help 应列出 run 命令。"""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "run" in result.stdout


def test_default_export_path_under_exports():
    """run/export 默认 Excel 路径应在 exports/ 下。"""
    from backend.export_wide import default_export_path

    path = default_export_path("20260605")
    assert path.startswith("exports/")
    assert "analysis_20260605_gen" in path
    assert path.endswith(".xlsx")
    assert default_export_path("20260605", "custom/out.xlsx") == "custom/out.xlsx"


def test_cmd_run_closes_date_connection_before_calc(monkeypatch):
    """cmd_run: resolve date → fetch → calc → export; short-lived conns."""
    import argparse
    from backend import cli

    events = []

    class FakeCon:
        def execute(self, *args, **kwargs):
            return type("R", (), {"fetchone": lambda self: ("20260605",)})()

        def close(self):
            events.append("con_closed")

    def fake_fetch(*_a, **_k):
        events.append("fetch_done")
        return 100

    def fake_rebuild(con, codes):
        events.append("dwd_rebuilt")

    def fake_calc(_args, skip_stale_fetch=False):
        events.append("calc_started")
        assert skip_stale_fetch is True

    monkeypatch.setattr(
        "backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr(
        "backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ"])
    monkeypatch.setattr(
        "backend.fetch.ods_daily.fetch_by_date_range_parallel", fake_fetch)
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_all_dwd", fake_rebuild)
    monkeypatch.setattr(cli, "cmd_calc", fake_calc)
    monkeypatch.setattr(
        "backend.export_wide.export_wide_to_excel", lambda *a, **k: 5000)
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605",
        ts_code=None,
        output="out.xlsx",
        include_st=False,
        no_index=False,
        db_path=None,
    )
    cli.cmd_run(args)

    assert events.count("con_closed") >= 2
    assert "fetch_done" in events
    assert "dwd_rebuilt" in events
    assert "calc_started" in events
    assert events.index("fetch_done") < events.index("calc_started")
