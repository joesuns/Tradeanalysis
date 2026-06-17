"""Tests for PipelineContext skip_dwd_calc (P0-1A)."""
import json
import duckdb

from backend.db.schema import create_all_tables
from backend.fetch.fetch_result import FetchResult
from backend.etl.pipeline_context import PipelineContext, compute_skip_dwd_calc
from tests.test_etl.helpers import insert_prior_calc_volume, seed_dim_date_anchor


def test_compute_skip_dwd_calc_true_when_unchanged_and_prior_calc():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO dim_stock (ts_code, name, list_date) VALUES ('A.SZ', 'A', '20200101')"
    )
    con.execute(
        "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
        "VALUES ('A.SZ', '20260612', 1,1,1,1,1,1)"
    )
    con.execute(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, amount, pct_chg) "
        "VALUES ('A.SZ', '20260612', 1,1,1,1,1,1,0)"
    )
    con.execute(
        "INSERT INTO dwd_weekly_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, amount, pct_chg) "
        "VALUES ('A.SZ', '20260612', 1,1,1,1,1,1,0)"
    )
    comp = json.dumps({"calc_date": "20260612", "stocks": 1})
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('1', 'calc_dws', 't0', 't1', 'success', 1, '', ?)""",
        [comp],
    )
    seed_dim_date_anchor(con, "20260612")
    insert_prior_calc_volume(con, "20260612")
    fr = FetchResult(api_rows=100, rows_written=0, rows_unchanged=100)
    assert compute_skip_dwd_calc(con, "20260612", ["A.SZ"], fr) is True
    con.close()


def test_compute_skip_dwd_calc_true_despite_structural_stale_ods():
    """Halted stocks (ODS max < date) must not block shortcut when fetch wrote 0."""
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO dim_stock (ts_code, name, list_date) VALUES "
        "('A.SZ', 'A', '20200101'), ('B.SZ', 'B', '20200101')"
    )
    con.execute(
        "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
        "VALUES ('A.SZ', '20260612', 1,1,1,1,1,1), ('B.SZ', '20260611', 1,1,1,1,1,1)"
    )
    for tbl in ("dwd_daily_quote", "dwd_weekly_quote"):
        con.execute(
            f"INSERT INTO {tbl} "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, amount, pct_chg) "
            "VALUES ('A.SZ', '20260612', 1,1,1,1,1,1,0), ('B.SZ', '20260611', 1,1,1,1,1,1,0)"
        )
    comp = json.dumps({"calc_date": "20260612", "stocks": 1})
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('1', 'calc_dws', 't0', 't1', 'success', 1, '', ?)""",
        [comp],
    )
    seed_dim_date_anchor(con, "20260612")
    insert_prior_calc_volume(con, "20260612")
    fr = FetchResult(rows_written=0, rows_unchanged=100)
    assert compute_skip_dwd_calc(con, "20260612", ["A.SZ", "B.SZ"], fr) is True
    con.close()


def test_compute_skip_dwd_calc_false_when_rows_written():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    events = [("A.SZ", "20260612", "ods_daily", "close", False)]
    fr = FetchResult(
        rows_written=1,
        changed_pairs=[("A.SZ", "20260612")],
        changed_field_events=events,
    )
    assert compute_skip_dwd_calc(con, "20260612", ["A.SZ"], fr) is False
    con.close()


def test_compute_skip_dwd_calc_true_when_cosmetic_turnover_rate_only():
    """turnover_rate API drift must not break L0 when prior calc exists."""
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO dim_stock (ts_code, name, list_date) VALUES "
        "('600021.SH', 'A', '20200101'), ('603108.SH', 'B', '20200101')"
    )
    for code in ("600021.SH", "603108.SH"):
        con.execute(
            "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
            "VALUES (?, '20260617', 1,1,1,1,1,1)",
            [code],
        )
        for tbl in ("dwd_daily_quote", "dwd_weekly_quote"):
            con.execute(
                f"INSERT INTO {tbl} "
                "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, amount, pct_chg) "
                "VALUES (?, '20260617', 1,1,1,1,1,1,0)",
                [code],
            )
    comp = json.dumps({"calc_date": "20260617", "stocks": 2})
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('1', 'calc_dws', 't0', 't1', 'success', 1, '', ?)""",
        [comp],
    )
    seed_dim_date_anchor(con, "20260617")
    insert_prior_calc_volume(con, "20260617")
    events = [
        ("600021.SH", "20260617", "ods_daily_basic", "turnover_rate", False),
        ("603108.SH", "20260617", "ods_daily_basic", "turnover_rate", False),
    ]
    fr = FetchResult(
        rows_written=2,
        changed_pairs=[("600021.SH", "20260617"), ("603108.SH", "20260617")],
        changed_field_events=events,
    )
    assert compute_skip_dwd_calc(con, "20260617", ["600021.SH", "603108.SH"], fr) is True
    con.close()


def test_compute_skip_dwd_calc_false_when_force_recalc():
    """run --force 穿透 L0 pipeline shortcut。"""
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    fr = FetchResult(rows_written=0, rows_unchanged=5000)
    assert compute_skip_dwd_calc(con, "20260612", ["A.SZ"], fr, force_recalc=True) is False
    con.close()


def test_pipeline_context_from_fetch_marks_shortcut():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute(
        "INSERT INTO dim_stock (ts_code, name, list_date) VALUES ('A.SZ', 'A', '20200101')"
    )
    con.execute(
        "INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
        "VALUES ('A.SZ', '20260612', 1,1,1,1,1,1)"
    )
    con.execute(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, amount, pct_chg) "
        "VALUES ('A.SZ', '20260612', 1,1,1,1,1,1,0)"
    )
    con.execute(
        "INSERT INTO dwd_weekly_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, amount, pct_chg) "
        "VALUES ('A.SZ', '20260612', 1,1,1,1,1,1,0)"
    )
    comp = json.dumps({"calc_date": "20260612"})
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg,
            data_completeness)
           VALUES ('1', 'calc_dws', 't0', 't1', 'success', 1, '', ?)""",
        [comp],
    )
    seed_dim_date_anchor(con, "20260612")
    insert_prior_calc_volume(con, "20260612")
    fr = FetchResult(rows_written=0, rows_unchanged=50)
    ctx = PipelineContext.from_fetch(con, "20260612", ["A.SZ"], fr, mode="run")
    assert ctx.skip_dwd_calc is True
    assert ctx.pipeline_shortcut is True
    con.close()
