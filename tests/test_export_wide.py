import os
import tempfile
import duckdb
from backend.db.schema import create_all_tables
from backend.export_wide import export_wide_to_excel


def test_export_creates_file():
    """Export creates an .xlsx file with at least one row for a trade date."""
    # Build a temporary DuckDB database on disk (export_wide_to_excel uses duckdb.connect)
    fd, db_path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(db_path)

    con = duckdb.connect(db_path)
    create_all_tables(con)

    con.execute(
        "INSERT INTO dim_stock (ts_code, stock_code, name) "
        "VALUES ('000001.SZ', '000001', 'Test')"
    )
    con.execute(
        "INSERT INTO dwd_daily_quote (ts_code, trade_date, close_qfq) "
        "VALUES ('000001.SZ', '20260101', 10.0)"
    )
    con.execute(
        "INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, dif, dea, macd_bar) "
        "VALUES ('000001.SZ', '20260101', '20260101', 1.0, 0.5, 1.0)"
    )
    # Latest view for MACD (needed by the wide view)
    con.execute("""CREATE VIEW IF NOT EXISTS v_dws_macd_daily_latest AS
        SELECT * FROM dws_macd_daily d WHERE calc_date = (
            SELECT MAX(calc_date) FROM dws_macd_daily
            WHERE ts_code = d.ts_code AND trade_date = d.trade_date)""")

    # The wide view (v_ads_analysis_wide_daily) is created by create_all_tables
    # but it references DWS tables + dim_stock + dwd_daily_quote.
    # We inserted the minimum data so the wide view returns rows.

    con.close()

    fd2, out_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd2)
    os.unlink(out_path)

    try:
        n = export_wide_to_excel(db_path, "20260101", out_path, freq="daily")
        assert n >= 1, f"Expected at least 1 row, got {n}"
        assert os.path.exists(out_path), f"Output file not found: {out_path}"
        assert os.path.getsize(out_path) > 0, "Output file is empty"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        # Clean up WAL file
        wal = db_path + ".wal"
        if os.path.exists(wal):
            os.unlink(wal)
        if os.path.exists(out_path):
            os.unlink(out_path)


def test_export_rejects_invalid_freq():
    """Export raises ValueError for unsupported freq."""
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        out_path = f.name

    try:
        try:
            export_wide_to_excel("nonexistent.duckdb", "20260101", out_path, freq="monthly")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "monthly" in str(e)
    finally:
        if os.path.exists(out_path):
            os.unlink(out_path)


def test_export_without_index():
    """Export with include_index=False only writes the stock sheet."""
    fd, db_path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(db_path)

    con = duckdb.connect(db_path)
    create_all_tables(con)

    con.execute(
        "INSERT INTO dim_stock (ts_code, stock_code, name) "
        "VALUES ('000001.SZ', '000001', 'Test')"
    )
    con.execute(
        "INSERT INTO dwd_daily_quote (ts_code, trade_date, close_qfq) "
        "VALUES ('000001.SZ', '20260101', 10.0)"
    )
    con.execute(
        "INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, dif, dea, macd_bar) "
        "VALUES ('000001.SZ', '20260101', '20260101', 1.0, 0.5, 1.0)"
    )
    con.execute("""CREATE VIEW IF NOT EXISTS v_dws_macd_daily_latest AS
        SELECT * FROM dws_macd_daily d WHERE calc_date = (
            SELECT MAX(calc_date) FROM dws_macd_daily
            WHERE ts_code = d.ts_code AND trade_date = d.trade_date)""")
    con.close()

    fd2, out_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd2)
    os.unlink(out_path)

    try:
        n = export_wide_to_excel(
            db_path, "20260101", out_path, freq="daily", include_index=False
        )
        assert n >= 1
        assert os.path.exists(out_path)
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        wal = db_path + ".wal"
        if os.path.exists(wal):
            os.unlink(wal)
        if os.path.exists(out_path):
            os.unlink(out_path)
