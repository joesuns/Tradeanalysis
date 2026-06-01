"""End-to-end integration tests: full ODS-DIM-DWD-DWS-ADS pipeline."""
import duckdb
import os
import tempfile

from backend.db.schema import create_all_tables
from backend.etl.build_dim import build_dim_stock, build_dim_date
from backend.etl.build_dwd import (
    build_dwd_daily_quote,
    build_dwd_daily_moneyflow,
)
from backend.etl.calc_macd import MACDCalculator
from backend.etl.calc_ma import MACalculator
from backend.etl.calc_kpattern import KPatternCalculator
from backend.etl.calc_dde import DDECalculator
from backend.etl.calc_volume import VolumeCalculator


def _gen_dates(num: int = 40):
    """Generate num consecutive 8-digit YYYYMMDD date strings starting from 2026-01-01."""
    dates = []
    for i in range(1, min(num, 31) + 1):
        dates.append(f"202601{i:02d}")
    if num > 31:
        for i in range(1, num - 31 + 1):
            dates.append(f"202602{i:02d}")
    return dates


def test_full_pipeline_single_stock():
    """Single stock end-to-end: ODS-DIM-DWD-DWS-ADS view query."""
    fd, db_path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(db_path)  # Remove the empty file so DuckDB can create it fresh
    con = duckdb.connect(db_path)
    try:
        create_all_tables(con)

        dates = _gen_dates(40)

        # --- ODS: Insert minimal data ---
        con.execute(
            "INSERT INTO ods_stock_basic (ts_code, symbol, name, exchange) "
            "VALUES ('000001.SZ','000001','平安银行','SZSE')"
        )
        for trade_date in dates:
            con.execute(
                "INSERT INTO ods_daily "
                "(ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor) "
                "VALUES (?,?,?,?,?,?,?,?,?,1.0)",
                ("000001.SZ", trade_date, 10.0, 10.5, 9.8, 10.2, 1000000.0, 10000000.0, 1.0),
            )
            con.execute(
                "INSERT INTO ods_daily_basic "
                "(ts_code, trade_date, total_mv, pe_ttm, turnover_rate, volume_ratio) "
                "VALUES (?,?,?,?,?,?)",
                ("000001.SZ", trade_date, 100000.0, 12.0, 2.5, 1.0),
            )
            con.execute(
                "INSERT INTO ods_moneyflow (ts_code, trade_date, buy_lg_vol, buy_elg_vol, "
                "sell_lg_vol, sell_elg_vol, buy_sm_vol, buy_md_vol, net_mf_vol, net_mf_amount) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("000001.SZ", trade_date, 50000.0, 20000.0, 40000.0, 15000.0,
                 30000.0, 25000.0, 15000.0, 150000.0),
            )
            con.execute(
                "INSERT INTO ods_trade_cal (cal_date, is_open) VALUES (?,1)",
                (trade_date,),
            )

        # --- DIM ---
        n = build_dim_stock(con)
        assert n == 1, f"dim_stock should have 1 row, got {n}"
        n = build_dim_date(con)
        assert n == 40, f"dim_date should have 40 rows, got {n}"

        # --- DWD ---
        n = build_dwd_daily_quote(con, ["000001.SZ"])
        assert n > 0, "dwd_daily_quote should have rows"
        n = build_dwd_daily_moneyflow(con, ["000001.SZ"])
        assert n > 0, "dwd_daily_moneyflow should have rows"

        # --- DWS: All 5 calculators ---
        calc_date = "20260131"
        MACDCalculator(con, "daily").calculate(["000001.SZ"], calc_date)
        MACalculator(con, "daily").calculate(["000001.SZ"], calc_date)
        KPatternCalculator(con, "daily").calculate(["000001.SZ"], calc_date)
        DDECalculator(con, "daily").calculate(["000001.SZ"], calc_date)
        VolumeCalculator(con, "daily").calculate(["000001.SZ"], calc_date)

        # --- Verify DWS tables have data ---
        for table in [
            "dws_macd_daily", "dws_ma_daily", "dws_kpattern_daily",
            "dws_dde_daily", "dws_volume_daily",
        ]:
            cnt = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert cnt > 0, f"{table} should have rows"

        # --- Verify latest views work ---
        row = con.execute(
            "SELECT * FROM v_dws_macd_daily_latest WHERE ts_code='000001.SZ' LIMIT 1"
        ).fetchone()
        assert row is not None, "v_dws_macd_daily_latest should return data"

        # --- Verify ADS wide view works ---
        row = con.execute(
            "SELECT ts_code, close, macd_zone "
            "FROM v_ads_analysis_wide_daily "
            "WHERE ts_code='000001.SZ' LIMIT 1"
        ).fetchone()
        assert row is not None, "v_ads_analysis_wide_daily should return data"

    finally:
        con.close()
        os.unlink(db_path)
        # Clean WAL if exists
        wal = db_path + ".wal"
        if os.path.exists(wal):
            os.unlink(wal)


def test_ods_etl_log_is_written():
    """Verify orchestrator logs steps to ods_etl_log."""
    fd, db_path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(db_path)  # Remove the empty file so DuckDB can create it fresh
    con = duckdb.connect(db_path)
    try:
        create_all_tables(con)
        from backend.etl.error_handler import log_etl

        log_etl(con, "test_step", "success", row_count=42)
        row = con.execute(
            "SELECT step_name, status, row_count FROM ods_etl_log "
            "WHERE step_name='test_step'"
        ).fetchone()
        assert row is not None
        assert row[1] == "success"
        assert row[2] == 42
    finally:
        con.close()
        os.unlink(db_path)


def test_cli_status_runs():
    """CLI status command runs without error."""
    import subprocess
    import sys

    fd, db_path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(db_path)
    # Create an empty DuckDB database so read-only open can succeed
    con = duckdb.connect(db_path)
    con.close()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "backend.cli", "status"],
            capture_output=True, text=True,
            env={**os.environ, "DUCKDB_PATH": db_path},
        )
        # Should succeed even with no data (tables don't exist)
        assert result.returncode == 0, (
            f"CLI status exited with {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        wal = db_path + ".wal"
        if os.path.exists(wal):
            os.unlink(wal)


