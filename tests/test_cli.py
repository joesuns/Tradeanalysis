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
        [sys.executable, "-m", "backend.cli", "etl", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "--step" in result.stdout
