"""Spec-version gate: state + trade_date section staleness."""
import duckdb
import pytest

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.calc_dde import DDECalculator
from backend.etl.calc_spec_gate import (
    count_dws_spec_stale_on_trade_date,
    export_spec_freshness_warnings,
    find_dws_spec_stale_codes,
    find_spec_stale_codes_merged,
    has_spec_stale_indicators,
    has_spec_stale_on_trade_date,
)
from backend.etl.calc_state import upsert_calc_state


def _insert_dde_row(con, ts_code, calc_date, trade_date, spec_version="v1"):
    con.execute(
        """
        INSERT INTO dws_dde_daily (
            ts_code, trade_date, ddx, ddx2, divergence, trend,
            trend_strength, alert, calc_date, input_fingerprint, spec_version
        ) VALUES (?, ?, 0.01, 0.02, NULL, 'flat', 0.0, NULL, ?, 'fp', ?)
        """,
        [ts_code, trade_date, calc_date, spec_version],
    )


def test_trade_date_section_stale_blocks_gate():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "dde", "20260616", "fp", "20260616",
        spec_version=DDECalculator.SPEC_VERSION,
    )
    _insert_dde_row(con, "000001.SZ", "20260616", "20260616", spec_version="v1")
    assert has_spec_stale_on_trade_date(con, "20260616") is True
    assert has_spec_stale_indicators(con, "20260616") is True
    con.close()


def test_other_trade_date_v1_does_not_block_current_section():
    """Historical v1 on T-1 must not block idempotent gate for trade_date=T."""
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "dde", "20260616", "fp", "20260616",
        spec_version=DDECalculator.SPEC_VERSION,
    )
    _insert_dde_row(con, "000001.SZ", "20260615", "20260615", spec_version="v1")
    _insert_dde_row(
        con, "000001.SZ", "20260616", "20260616",
        spec_version=DDECalculator.SPEC_VERSION,
    )
    assert has_spec_stale_on_trade_date(con, "20260616") is False
    assert has_spec_stale_indicators(con, "20260616") is False
    con.close()


def test_state_stale_still_blocks():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "ma", "20260616", "fp", "20260616",
        spec_version="v1",
    )
    assert has_spec_stale_indicators(con, "20260616") is True
    con.close()


def test_merged_stale_unions_state_and_section():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    upsert_calc_state(
        con, "000001.SZ", "daily", "dde", "20260616", "fp", "20260616",
        spec_version="v1",
    )
    upsert_calc_state(
        con, "000002.SZ", "daily", "dde", "20260616", "fp", "20260616",
        spec_version=DDECalculator.SPEC_VERSION,
    )
    _insert_dde_row(con, "000002.SZ", "20260616", "20260616", spec_version="v1")
    merged = find_spec_stale_codes_merged(con, ["dde"], trade_date="20260616")
    codes = set(merged.get(("dde", "daily"), []))
    assert codes == {"000001.SZ", "000002.SZ"}
    con.close()


def test_find_dws_stale_scoped_to_trade_date():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    _insert_dde_row(con, "000001.SZ", "20260615", "20260615", spec_version="v1")
    _insert_dde_row(
        con, "000001.SZ", "20260616", "20260616",
        spec_version=DDECalculator.SPEC_VERSION,
    )
    stale_t16 = find_dws_spec_stale_codes(
        con, ["dde"], ["000001.SZ"], trade_date="20260616",
    )
    stale_t15 = find_dws_spec_stale_codes(
        con, ["dde"], ["000001.SZ"], trade_date="20260615",
    )
    assert stale_t16.get(("dde", "daily"), []) == []
    assert stale_t15.get(("dde", "daily")) == ["000001.SZ"]
    con.close()


def test_export_warnings_use_trade_date_section():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    _insert_dde_row(con, "000001.SZ", "20260616", "20260616", spec_version="v1")
    msgs = export_spec_freshness_warnings(con, "20260616")
    assert any("section spec stale @ 20260616" in m for m in msgs)
    counts = count_dws_spec_stale_on_trade_date(con, "20260616", ["000001.SZ"])
    assert counts.get("dde_daily", 0) >= 1
    con.close()


def test_catalog_exception_returns_false_not_raise():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    # priceposition table uses price_position prefix — view exists; force missing view path
    assert has_spec_stale_on_trade_date(con, "20991231") is False
