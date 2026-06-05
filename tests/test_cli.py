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
