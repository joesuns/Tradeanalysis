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


def _fr(written=0, **kw):
    from backend.fetch.fetch_result import FetchResult
    return FetchResult(rows_written=written, **kw)


def _export(n):
    from backend.export_wide import ExportResult
    return ExportResult(row_count=n, tradable_enrich={})


def _fake_query_result(fetchone=("20260605",), fetchall=None):
    if fetchall is None:
        fetchall = []
    return type(
        "R",
        (),
        {
            "fetchone": lambda self: fetchone,
            "fetchall": lambda self: fetchall,
        },
    )()


class FakeCon:
    """Minimal DuckDB stand-in for cmd_run unit tests."""

    def execute(self, *args, **kwargs):
        sql = args[0] if args else ""
        params = args[1] if len(args) > 1 else None
        if not isinstance(sql, str):
            return _fake_query_result()
        upper = sql.upper()
        if "COUNT(" in upper:
            return _fake_query_result(fetchone=(0,))
        if "ODS_ETL_LOG" in upper and "CALC_DWS" in upper:
            return _fake_query_result(fetchone=None)
        if "MAX(TRADE_DATE)" in upper and "DIM_DATE" in upper:
            trade_date = params[0] if params else "20260605"
            return _fake_query_result(fetchone=(trade_date,))
        if "MAX(TRADE_DATE)" in upper and "ODS_DAILY" in upper:
            return _fake_query_result(fetchone=("20260605",))
        return _fake_query_result()

    def close(self):
        pass


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

    class TrackingFakeCon(FakeCon):
        def close(self):
            events.append("con_closed")

    def fake_fetch(*_a, **_k):
        events.append("fetch_done")
        return _fr(100)

    def fake_rebuild(con, codes):
        events.append("dwd_rebuilt")

    def fake_calc(_args, skip_stale_fetch=False):
        events.append("calc_started")
        assert skip_stale_fetch is True

    monkeypatch.setattr(
        "backend.db.connection.get_connection", lambda: TrackingFakeCon())
    monkeypatch.setattr(
        "backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ"])
    monkeypatch.setattr(
        "backend.fetch.ods_daily.fetch_by_date_range_parallel", fake_fetch)
    monkeypatch.setattr(
        "backend.etl.orchestrator.find_stale_dwd_codes",
        lambda con, codes, date: list(codes),
    )
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_dwd_incremental",
                        lambda con, codes, d: fake_rebuild(con, codes))
    monkeypatch.setattr(cli, "cmd_calc", fake_calc)
    monkeypatch.setattr(
        "backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(5000))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605",
        ts_code=None,
        output="out.xlsx",
        include_st=False,
        no_index=False,
        db_path=None,
        force=False,
        skip_export=False,
    )
    cli.cmd_run(args)

    assert events.count("con_closed") >= 2
    assert "fetch_done" in events
    assert "dwd_rebuilt" in events
    assert "calc_started" in events
    assert events.index("fetch_done") < events.index("calc_started")


def test_cmd_run_skips_rebuild_when_fetch_zero_and_dwd_fresh(monkeypatch):
    """n_fetch=0 且 find_stale_dwd_codes 空 → 不得 rebuild_all_dwd。"""
    import argparse
    from backend import cli

    rebuild_calls = []

    def fake_fetch(*_a, **_k):
        return _fr(0)

    def fake_rebuild(con, codes):
        rebuild_calls.append(list(codes))

    def fake_stale(con, codes, date):
        assert date == "20260605"
        return []

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ", "B.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", fake_fetch)
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_all_dwd", fake_rebuild)
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes", fake_stale)
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(1))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=False,
    )
    cli.cmd_run(args)
    assert rebuild_calls == []


def test_cmd_run_rebuilds_stale_subset_when_fetch_zero(monkeypatch):
    """n_fetch=0 但 stale 非空 → incremental rebuild stale 股（DWD_INCREMENTAL 默认）。"""
    import argparse
    from backend import cli

    rebuild_calls = []

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ", "B.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: _fr(0))
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes",
                        lambda con, codes, date: ["B.SZ"])
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_dwd_incremental",
                        lambda con, codes, d: rebuild_calls.append(list(codes)))
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(1))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=False,
    )
    cli.cmd_run(args)
    assert rebuild_calls == [["B.SZ"]]


def test_cmd_run_n_fetch_zero_uses_full_when_dwd_incremental_disabled(monkeypatch):
    """DWD_INCREMENTAL=0 → n_fetch=0 stale 路径走 rebuild_all_dwd。"""
    import argparse
    from backend import cli

    rebuild_calls = []

    monkeypatch.setattr("backend.config.DWD_INCREMENTAL", False)
    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ", "B.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: _fr(0))
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes",
                        lambda con, codes, date: ["B.SZ"])
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_all_dwd",
                        lambda con, codes: rebuild_calls.append(list(codes)))
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(1))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=False,
    )
    cli.cmd_run(args)
    assert rebuild_calls == [["B.SZ"]]


def test_cmd_run_rebuilds_stale_when_fetch_gt_zero(monkeypatch):
    """n_fetch>0 → rebuild stale 子集（非全市场）。"""
    import argparse
    from backend import cli

    rebuild_calls = []

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ", "B.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: _fr(100))
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes",
                        lambda con, codes, date: ["B.SZ"])
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_dwd_incremental",
                        lambda con, codes, d: rebuild_calls.append(list(codes)))
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(1))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=False,
    )
    cli.cmd_run(args)
    assert rebuild_calls == [["B.SZ"]]


def test_cmd_run_logs_fetch_rebuild_export_steps(monkeypatch):
    """cmd_run 应对 fetch/rebuild/export 各写一条 ods_etl_log。"""
    import argparse
    from backend import cli

    logged_steps = []

    def fake_log_end(con, lid, step_name, t0, status, row_count=0, **kw):
        logged_steps.append((step_name, status, row_count, kw.get("data_completeness")))

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: _fr(0))
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes", lambda *a, **k: [])
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(42))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", fake_log_end)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=False,
    )
    cli.cmd_run(args)
    step_names = [s[0] for s in logged_steps]
    assert step_names == ["run_fetch", "run_rebuild_dwd", "run_export"]
    assert logged_steps[0][2] == 0          # n_fetch
    assert logged_steps[1][3]["skipped"] is True  # rebuild skipped
    assert logged_steps[2][2] == 42         # export rows


def test_cmd_run_pipeline_shortcut_skips_dwd_and_calc(monkeypatch):
    """0 ODS diff + prior calc → skip DWD+calc, still export, pipeline_shortcut logged."""
    import argparse
    from backend import cli

    calc_calls = []
    rebuild_calls = []
    logged_steps = []

    def fake_log_end(con, lid, step_name, t0, status, row_count=0, **kw):
        logged_steps.append((step_name, status, row_count, kw.get("data_completeness")))

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ"])
    monkeypatch.setattr(
        "backend.fetch.ods_daily.fetch_by_date_range_parallel",
        lambda *a, **k: _fr(0, rows_unchanged=5000, api_rows=5000),
    )
    monkeypatch.setattr("backend.etl.calc_gate.has_prior_calc_snapshot", lambda *a, **k: True)
    monkeypatch.setattr(
        "backend.etl.calc_spec_gate.has_spec_stale_indicators", lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "backend.etl.orchestrator.find_stale_ods_codes", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.etl.orchestrator.find_stale_dwd_codes", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.etl.build_dwd.rebuild_dwd_for_stale",
        lambda *a, **k: rebuild_calls.append(True) or {},
    )
    monkeypatch.setattr(
        cli, "cmd_calc", lambda *a, **k: calc_calls.append(True),
    )
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(99))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", fake_log_end)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=False,
    )
    cli.cmd_run(args)

    assert calc_calls == []
    assert rebuild_calls == []
    step_names = [s[0] for s in logged_steps]
    assert step_names == ["run_fetch", "run_rebuild_dwd", "run_export"]
    assert logged_steps[0][2] == 0
    rebuild_comp = logged_steps[1][3]
    assert rebuild_comp["skipped"] is True
    assert rebuild_comp["pipeline_shortcut"] is True
    assert logged_steps[2][2] == 99


def test_cmd_run_force_bypasses_pipeline_shortcut(monkeypatch):
    """run --force: 0 ODS diff 仍进入 calc（L0 不短路），DWD 无 stale 时可跳过 rebuild。"""
    import argparse
    from backend import cli

    calc_calls = []
    rebuild_calls = []

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ"])
    monkeypatch.setattr(
        "backend.fetch.ods_daily.fetch_by_date_range_parallel",
        lambda *a, **k: _fr(0, rows_unchanged=5000, api_rows=5000),
    )
    monkeypatch.setattr("backend.etl.calc_gate.has_prior_calc_snapshot", lambda *a, **k: True)
    monkeypatch.setattr(
        "backend.etl.calc_spec_gate.has_spec_stale_indicators", lambda *a, **k: False,
    )
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes", lambda *a, **k: [])
    monkeypatch.setattr(
        "backend.etl.build_dwd.find_stocks_needing_qfq_refresh", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.etl.build_dwd.rebuild_dwd_for_stale",
        lambda *a, **k: rebuild_calls.append(True) or {},
    )

    def fake_calc(_args, skip_stale_fetch=False):
        calc_calls.append({"force": getattr(_args, "force", False), "skip_stale_fetch": skip_stale_fetch})

    monkeypatch.setattr(cli, "cmd_calc", fake_calc)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(1))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=True, skip_export=False,
    )
    cli.cmd_run(args)

    assert len(calc_calls) == 1
    assert calc_calls[0]["force"] is True
    assert calc_calls[0]["skip_stale_fetch"] is True
    assert rebuild_calls == []


def test_cmd_run_skip_export(monkeypatch):
    """--skip-export → 不调用 export_wide_to_excel。"""
    import argparse
    from backend import cli

    export_called = []

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: _fr(0))
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes", lambda *a, **k: [])
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel",
                        lambda *a, **k: export_called.append(True) or _export(1))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=True,
    )
    cli.cmd_run(args)
    assert export_called == []


def test_cmd_run_refreshes_state_after_dwd_rebuild(monkeypatch):
    """DWD rebuild 非空 → 对 stale 子集调用 refresh-state，再 calc。"""
    import argparse
    from backend import cli

    events = []

    def fake_rebuild_incremental(con, codes, d):
        events.append(("rebuild", list(codes)))
        return {"daily_quote": 10, "weekly_quote": 5, "moneyflow": 3}

    def fake_refresh(con, codes, calc_date, dwd_result, return_artifacts=False):
        events.append(("refresh", list(codes), calc_date, dwd_result))
        return {"records_written": 12}

    def fake_calc(_args, skip_stale_fetch=False):
        events.append("calc")

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ", "B.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: _fr(100))
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes",
                        lambda con, codes, date: ["B.SZ"])
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_dwd_incremental", fake_rebuild_incremental)
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.maybe_refresh_state_after_dwd_rebuild",
        fake_refresh,
    )
    monkeypatch.setattr(cli, "cmd_calc", fake_calc)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(1))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260610", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=True,
    )
    cli.cmd_run(args)
    assert ("rebuild", ["B.SZ"]) in events
    refresh_events = [e for e in events if isinstance(e, tuple) and e[0] == "refresh"]
    assert len(refresh_events) == 1
    assert refresh_events[0][1] == ["B.SZ"]
    assert refresh_events[0][2] == "20260610"
    assert events[-1] == "calc"


def test_cmd_run_skips_refresh_when_dwd_skipped(monkeypatch):
    import argparse
    from backend import cli

    refresh_calls = []

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: _fr(0))
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes", lambda *a, **k: [])
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.maybe_refresh_state_after_dwd_rebuild",
        lambda *a, **k: refresh_calls.append(1),
    )
    monkeypatch.setattr(cli, "cmd_calc", lambda *a, **k: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(1))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260610", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=True,
    )
    cli.cmd_run(args)
    assert refresh_calls == []


def test_cmd_run_sets_preflight_context(monkeypatch):
    """DWD rebuild + refresh with artifacts → calc receives popped context."""
    import argparse
    from backend import cli
    from backend.etl.calc_preflight_context import pop_run_preflight_context

    captured = {}

    tails_bundle = {
        "daily_tails": {"B.SZ": "tail"},
        "weekly_tails": {},
        "dde_daily": {},
        "dde_weekly": {},
        "stock_modes": {"B.SZ": {("macd", "daily"): ("APPEND", ["20260610"])}},
        "fp_cache_by_stock": {"B.SZ": {("macd", "daily"): "fp1"}},
        "state_map": {("B.SZ", "daily", "macd"): {"last_trade_date": "20260609"}},
    }

    def fake_refresh(con, codes, calc_date, dwd_result, return_artifacts=False):
        if return_artifacts:
            return {"records_written": 1}, tails_bundle
        return {"records_written": 1}

    def fake_calc(_args, skip_stale_fetch=False):
        ctx = pop_run_preflight_context()
        captured["preflight_ctx"] = ctx

    monkeypatch.setattr("backend.config.CALC_REUSE_REFRESH_CTX", True)
    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ", "B.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: _fr(100))
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes",
                        lambda con, codes, date: ["B.SZ"])
    monkeypatch.setattr(
        "backend.etl.build_dwd.rebuild_dwd_for_stale",
        lambda con, codes, d: {"daily_quote": 10},
    )
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.maybe_refresh_state_after_dwd_rebuild",
        fake_refresh,
    )
    monkeypatch.setattr(cli, "cmd_calc", fake_calc)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: _export(1))
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260610", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=True,
    )
    cli.cmd_run(args)
    ctx = captured.get("preflight_ctx")
    assert ctx is not None
    assert ctx.source == "refresh_state"
    assert "B.SZ" in ctx.stock_modes
    assert pop_run_preflight_context() is None


def test_cmd_calc_refresh_spec_bypasses_run_calc(monkeypatch):
    """--refresh-spec 走 cmd_refresh_spec，不调用 run_calc。"""
    import argparse
    from backend import cli

    refresh_calls = []
    run_calc_calls = []

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr(
        cli, "_ensure_trade_date", lambda con, d: d or "20260612",
    )
    monkeypatch.setattr(
        cli, "_resolve_trade_date", lambda con, d: d or "20260612",
    )
    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh.cmd_refresh_spec",
        lambda con, calc_date, spec, ts_codes, dry_run=False: refresh_calls.append(
            (calc_date, spec, ts_codes, dry_run),
        ),
    )
    monkeypatch.setattr(
        "backend.etl.orchestrator.run_calc",
        lambda *a, **k: run_calc_calls.append(True),
    )
    monkeypatch.setattr("backend.db.connection.run_checkpoint", lambda con: None)

    args = argparse.Namespace(
        date="20260612", ts_code=["000001.SZ"], force=False,
        refresh_spec="ma,volume",
    )
    cli.cmd_calc(args)

    assert refresh_calls == [("20260612", "ma,volume", ["000001.SZ"], False)]
    assert run_calc_calls == []
