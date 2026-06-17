"""Tests for ODS column → indicator dependency mapping (Wave 5)."""
import duckdb
import pytest

from backend.db.schema import create_all_tables
from backend.etl.column_indicator_deps import (
    ALL_INDICATORS,
    QUOTE_INDICATORS,
    affected_ods_column_names_block_calc,
    calc_affecting_changed_codes,
    calc_routes_narrowed,
    dde_patch_ts_codes,
    fetch_blocks_dwd_calc,
    resolve_affected_indicators,
    resolve_run_calc_indicator_filter,
)
from backend.fetch.fetch_result import FetchResult


def test_circ_mv_only_affects_dde():
    events = [("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False)]
    assert resolve_affected_indicators(events) == {"dde"}


def test_adj_factor_returns_none_for_narrow():
    events = [("000001.SZ", "20260612", "ods_daily", "adj_factor", False)]
    assert resolve_affected_indicators(events) is None


def test_daily_insert_returns_none():
    events = [("000001.SZ", "20260612", "ods_daily", "close", True)]
    assert resolve_affected_indicators(events) is None


def test_moneyflow_vol_column_affects_dde_only():
    events = [("000001.SZ", "20260612", "ods_moneyflow", "buy_lg_vol", False)]
    assert resolve_affected_indicators(events) == {"dde"}


def test_vol_only_affects_kpattern_and_volume():
    events = [("000001.SZ", "20260612", "ods_daily", "vol", False)]
    assert resolve_affected_indicators(events) == {"kpattern", "volume"}


def test_close_only_is_full_indicator_set():
    events = [("000001.SZ", "20260612", "ods_daily", "close", False)]
    assert resolve_affected_indicators(events) is None


def test_pe_ttm_only_returns_none():
    events = [("000001.SZ", "20260612", "ods_daily_basic", "pe_ttm", False)]
    assert resolve_affected_indicators(events) is None


def test_turnover_rate_drift_does_not_block_l0():
    events = [
        ("600021.SH", "20260617", "ods_daily_basic", "turnover_rate", False),
        ("603108.SH", "20260617", "ods_daily_basic", "turnover_rate", False),
    ]
    fr = FetchResult(
        rows_written=2,
        changed_pairs=[("600021.SH", "20260617"), ("603108.SH", "20260617")],
        changed_field_events=events,
    )
    assert fetch_blocks_dwd_calc(fr) is False
    assert calc_affecting_changed_codes(events, "20260617") == []
    assert affected_ods_column_names_block_calc(["turnover_rate"]) is False


def test_close_drift_blocks_l0():
    events = [("000001.SZ", "20260612", "ods_daily", "close", False)]
    fr = FetchResult(
        rows_written=1,
        changed_pairs=[("000001.SZ", "20260612")],
        changed_field_events=events,
    )
    assert fetch_blocks_dwd_calc(fr) is True
    assert calc_affecting_changed_codes(events, "20260612") == ["000001.SZ"]
    assert affected_ods_column_names_block_calc(["close"]) is True


def test_empty_events_returns_none():
    assert resolve_affected_indicators([]) is None


def test_dde_patch_ts_codes_net_amount_dc_and_circ_mv():
    events = [
        ("000001.SZ", "20260612", "ods_moneyflow", "net_amount_dc", False),
        ("000002.SZ", "20260612", "ods_daily_basic", "circ_mv", False),
        ("000003.SZ", "20260612", "ods_moneyflow", "buy_lg_vol", False),
    ]
    assert dde_patch_ts_codes(events) == ["000001.SZ", "000002.SZ"]


def test_dde_patch_ts_codes_empty():
    assert dde_patch_ts_codes([]) == []


def test_quote_indicators_constant():
    assert "dde" not in QUOTE_INDICATORS
    assert QUOTE_INDICATORS | {"dde"} == ALL_INDICATORS


def test_resolve_run_calc_filter_circ_mv_only(db_with_schema):
    con = db_with_schema
    fr = FetchResult(
        rows_written=1,
        changed_pairs=[("000001.SZ", "20260612")],
        changed_field_events=[
            ("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False),
        ],
    )
    filt = resolve_run_calc_indicator_filter(
        con, fr,
        changed_codes=["000001.SZ"],
        stale_extra_codes=[],
        qfq_codes=[],
    )
    assert filt == ["dde"]
    assert calc_routes_narrowed(filt) is True


def test_resolve_run_calc_filter_blocked_by_qfq(db_with_schema):
    con = db_with_schema
    fr = FetchResult(
        rows_written=1,
        changed_field_events=[
            ("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False),
        ],
    )
    assert resolve_run_calc_indicator_filter(
        con, fr,
        changed_codes=["000001.SZ"],
        stale_extra_codes=[],
        qfq_codes=["000001.SZ"],
    ) is None


def test_resolve_run_calc_filter_blocked_by_stale_extra(db_with_schema):
    con = db_with_schema
    fr = FetchResult(
        rows_written=1,
        changed_field_events=[
            ("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False),
        ],
    )
    assert resolve_run_calc_indicator_filter(
        con, fr,
        changed_codes=["000001.SZ"],
        stale_extra_codes=["000002.SZ"],
        qfq_codes=[],
    ) is None


def test_resolve_run_calc_filter_merges_spec_stale(db_with_schema, monkeypatch):
    con = db_with_schema
    fr = FetchResult(
        rows_written=1,
        changed_field_events=[
            ("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False),
        ],
    )
    monkeypatch.setattr(
        "backend.etl.column_indicator_deps._spec_stale_indicator_names",
        lambda _con: {"ma"},
    )
    filt = resolve_run_calc_indicator_filter(
        con, fr,
        changed_codes=["000001.SZ"],
        stale_extra_codes=[],
        qfq_codes=[],
    )
    assert filt == ["dde", "ma"]


def test_resolve_run_calc_filter_disabled(monkeypatch, db_with_schema):
    monkeypatch.setenv("CALC_COLUMN_NARROW", "0")
    import importlib
    import backend.config as cfg
    importlib.reload(cfg)

    con = db_with_schema
    fr = FetchResult(
        rows_written=1,
        changed_field_events=[
            ("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False),
        ],
    )
    assert resolve_run_calc_indicator_filter(
        con, fr,
        changed_codes=["000001.SZ"],
        stale_extra_codes=[],
        qfq_codes=[],
    ) is None

    monkeypatch.setenv("CALC_COLUMN_NARROW", "1")
    importlib.reload(cfg)




@pytest.fixture
def db_with_schema():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    yield con
    con.close()
