import os
import tempfile
import duckdb
from backend.db.schema import create_all_tables
from backend.etl.build_dim import build_dim_date
from backend.export_wide import export_wide_to_excel


def _seed_weekly(con):
    """Minimal weekly scaffolding so the daily+weekly merge path is exercised.

    The export always merges daily + weekly; without a resolvable week-end and
    at least one weekly row, the weekly frame is empty and has no ts_code column.
    """
    con.execute("INSERT INTO ods_trade_cal (cal_date,is_open) VALUES ('20260101',1)")
    build_dim_date(con)
    con.execute(
        "INSERT INTO dwd_weekly_quote (ts_code, trade_date, close_qfq) "
        "VALUES ('000001.SZ', '20260101', 10.0)"
    )


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
    _seed_weekly(con)

    con.close()

    fd2, out_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd2)
    os.unlink(out_path)

    try:
        n = export_wide_to_excel(db_path, "20260101", out_path)
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
    _seed_weekly(con)
    con.close()

    fd2, out_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd2)
    os.unlink(out_path)

    try:
        n = export_wide_to_excel(
            db_path, "20260101", out_path, include_index=False
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


def test_export_handles_empty_weekly():
    """When no week-end resolves (empty weekly frame), export must not crash —
    it should still write the daily rows."""
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
    # Intentionally NO trade_cal / dim_date / dwd_weekly_quote → week_end is None
    # → weekly frame is empty with no ts_code column.
    con.close()

    fd2, out_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd2)
    os.unlink(out_path)

    try:
        n = export_wide_to_excel(db_path, "20260101", out_path, include_index=False)
        assert n >= 1, f"Expected daily rows despite empty weekly, got {n}"
        assert os.path.exists(out_path)
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        wal = db_path + ".wal"
        if os.path.exists(wal):
            os.unlink(wal)
        if os.path.exists(out_path):
            os.unlink(out_path)


def test_wide_view_column_symmetry():
    """v_ads_analysis_wide_daily must have macd_trend_strength — column parity with weekly."""
    fd, db_path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(db_path)

    con = duckdb.connect(db_path)
    create_all_tables(con)

    from backend.db.schema import _ADS_WIDE_VIEWS_DDL
    for sql in _ADS_WIDE_VIEWS_DDL:
        con.execute(sql)

    daily_cols = {r[0] for r in con.execute("DESCRIBE v_ads_analysis_wide_daily").fetchall()}
    weekly_cols = {r[0] for r in con.execute("DESCRIBE v_ads_analysis_wide_weekly").fetchall()}

    # macd_trend_strength must exist in both
    assert "macd_trend_strength" in daily_cols, \
        f"v_ads_analysis_wide_daily missing macd_trend_strength"
    assert "macd_trend_strength" in weekly_cols, \
        f"v_ads_analysis_wide_weekly missing macd_trend_strength"

    # Key signal columns must have parity across daily/weekly
    signal_cols = {
        "macd_trend", "macd_trend_strength",
        "macd_divergence", "macd_zone", "macd_turning_point", "macd_alert",
        "dde_trend", "dde_trend_strength", "dde_alert", "dde_divergence",
        "vol_trend", "vol_trend_strength", "vol_divergence", "vol_zone",
    }
    missing_daily = signal_cols - daily_cols
    missing_weekly = signal_cols - weekly_cols
    assert not missing_daily, f"Daily view missing signal columns: {missing_daily}"
    assert not missing_weekly, f"Weekly view missing signal columns: {missing_weekly}"

    con.close()
    os.unlink(db_path)


def test_export_display_null_semantics():
    """事件列 NULL→'-'；状态列 NULL→'N/A'；PE NULL→'N/A'。"""
    from backend.export_wide import apply_display_nulls
    import pandas as pd

    df = pd.DataFrame({
        "macd_divergence": [None],
        "pct_vol_rank": [None],
        "pe_ttm": [None],
        "ma_alignment": [None],
    })
    out = apply_display_nulls(df)
    assert out["macd_divergence"].iloc[0] == "-"
    assert out["pct_vol_rank"].iloc[0] == "N/A"
    assert out["pe_ttm"].iloc[0] == "N/A"
    assert out["ma_alignment"].iloc[0] == "N/A"
