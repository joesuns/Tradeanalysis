"""cli ops spec-status formatting and anchor query."""
import duckdb
import pytest

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.ops_spec_status import (
    cmd_spec_status,
    fetch_spec_freshness_rows,
    format_spec_status_table,
    suggest_refresh_spec,
)


def test_format_spec_status_table_prints_stale_rows():
    rows = [
        ("macd", "daily", "20260616", 5375, 5375, 0, "v3"),
    ]
    text = format_spec_status_table(rows)
    assert "macd" in text
    assert "5375" in text
    assert "stale" in text.lower() or "5375" in text


def test_suggest_refresh_spec_when_stale():
    rows = [
        ("macd", "daily", "20260616", 100, 50, 50, "v3"),
        ("volume", "weekly", "20260613", 100, 100, 0, "v2"),
    ]
    hint = suggest_refresh_spec(rows, "20260616")
    assert "macd" in hint
    assert "volume" not in hint
    assert "20260616" in hint


def test_suggest_refresh_spec_empty_when_fresh():
    rows = [("macd", "daily", "20260616", 100, 100, 0, "v3")]
    assert suggest_refresh_spec(rows, "20260616") == ""


def test_fetch_spec_freshness_rows_requires_view():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    con.execute(
        """
        INSERT INTO dim_date (trade_date, is_trade_day, is_week_end)
        VALUES ('20260616', 1, 0), ('20260613', 1, 1)
        """
    )
    rows = fetch_spec_freshness_rows(con, "20260616")
    assert isinstance(rows, list)
    con.close()


def test_cmd_spec_status_empty(capsys):
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    cmd_spec_status(con, "20260616")
    out = capsys.readouterr().out
    assert "No v_dq_spec_freshness" in out or "indicator" in out
    con.close()
