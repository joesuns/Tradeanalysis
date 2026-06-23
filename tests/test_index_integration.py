"""End-to-end index pipeline integration tests."""
import pytest


@pytest.mark.integration
@pytest.mark.slow
def test_index_full_pipeline(db_with_schema):
    """Fetch -> DIM -> DWD -> Calc -> Query — full pipeline for 000001.SH."""
    from backend.fetch.ods_index import (
        fetch_index_basic, fetch_index_daily,
    )
    from backend.etl.build_dim_index import build_dim_index
    from backend.etl.build_dwd_index import build_dwd_index_all
    from backend.etl.calc_index import calc_index_pipeline

    con = db_with_schema

    # 1. Fetch (requires tushare token, skipped in CI)
    import os
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        pytest.skip("TUSHARE_TOKEN not set — skipping full pipeline test")

    from backend.fetch.client import get_client
    client = get_client()

    fetch_index_basic(client, con)
    fetch_index_daily(client, con)

    # 2. DIM
    n_dim = build_dim_index(con)
    assert n_dim >= 1

    # 3. DWD
    dwd_result = build_dwd_index_all(con)
    assert dwd_result["dwd_index_daily"] > 0

    # 4. Calc
    calc_date = con.execute(
        "SELECT MAX(trade_date) FROM dwd_index_daily"
    ).fetchone()[0]
    stats = calc_index_pipeline(con, calc_date)
    assert stats["index_macd_daily"]["calculated"] >= 1

    # 5. Query via view
    row = con.execute(f"""
        SELECT ts_code, close, dif, dea, macd_bar, macd_zone
        FROM v_ads_market_index_daily
        WHERE ts_code = '000001.SH' AND trade_date = ?
    """, [calc_date]).fetchone()
    assert row is not None
    assert row[1] is not None  # close
    assert row[4] is not None  # macd_bar
    assert row[5] in ("bull", "bear", None)  # macd_zone


@pytest.mark.integration
def test_index_export_sheet_populates(db_with_schema):
    """Index export sheet function doesn't crash with empty data."""
    from openpyxl import Workbook
    from backend.export_index import export_index_sheet

    wb = Workbook()
    ws = wb.active
    ws.title = "指数概览"
    n = export_index_sheet(db_with_schema, "20260601", ws)
    # Should return 0 for no data, not crash
    assert n >= 0
    # Header or placeholder should exist
    assert ws.cell(row=1, column=1).value is not None
