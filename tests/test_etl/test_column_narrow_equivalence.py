"""Wave 5: narrow indicator_filter vs full-route calc equivalence (atol=1e-9)."""
from __future__ import annotations

import importlib
from typing import Dict, List, Optional

import duckdb
import numpy as np
import pandas as pd
import pytest

ATOL = 1e-9

DDE_FLOAT_COLS = ["ddx", "ddx2", "trend_strength", "net_mf_amount"]
DDE_STR_COLS = ["trend", "alert", "divergence"]

VOL_FLOAT_COLS = ["ma_vol_5", "pct_vol_rank", "volume_ratio", "trend_strength"]
VOL_STR_COLS = ["zone", "trend", "divergence"]


def _seed_equiv_db(con, codes: List[str], n_daily: int = 280):
    """Minimal DWD for full-market calc on 1–3 stocks (≥250 bars)."""
    from backend.db.schema import create_all_tables, ensure_calc_state_table

    create_all_tables(con)
    ensure_calc_state_table(con)

    dates = [
        (pd.Timestamp("2020-01-01") + pd.Timedelta(days=i)).strftime("%Y%m%d")
        for i in range(n_daily)
    ]
    for d in dates:
        con.execute(
            "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
            [d],
        )

    calc_date = dates[-1]
    rng = np.random.default_rng(42)
    for j, code in enumerate(codes):
        con.execute(
            "INSERT INTO dim_stock (ts_code, name, list_date, is_st) VALUES (?, ?, '20150101', 0)",
            [code, code],
        )
        close = 10.0 + np.cumsum(rng.normal(0, 0.15, n_daily))
        circ_base = 5e8 + j * 1e7
        for i, d in enumerate(dates):
            c = float(close[i])
            vol = 800_000.0 + i * 500 + j * 1000
            circ = circ_base * (1.0 + 0.001 * np.sin(i / 20.0))
            con.execute(
                "INSERT INTO dwd_daily_quote "
                "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, "
                "amount, pct_chg, total_mv, circ_mv, is_suspended) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                [code, d, c * 0.99, c * 1.01, c * 0.98, c, vol, vol * c, 0.0,
                 1e10, circ],
            )
            buy_lg = vol * 0.25
            sell_lg = vol * 0.20
            buy_elg = vol * 0.08
            sell_elg = vol * 0.06
            net_dc = (buy_lg + buy_elg - sell_lg - sell_elg) * c
            con.execute(
                "INSERT INTO dwd_daily_moneyflow "
                "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, "
                "sell_elg_vol, total_vol, net_mf_amount, net_amount_dc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [code, d, buy_lg, sell_lg, buy_elg, sell_elg, vol,
                 net_dc * 0.5, net_dc],
            )
            con.execute(
                "INSERT INTO ods_daily "
                "(ts_code, trade_date, open, high, low, close, vol, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [code, d, c * 0.99, c * 1.01, c * 0.98, c, vol, vol * c],
            )

        week_dates = dates[4::5][:55]
        for i, wd in enumerate(week_dates):
            c = float(close[dates.index(wd)])
            con.execute("UPDATE dim_date SET is_week_end = 1 WHERE trade_date = ?", [wd])
            con.execute(
                "INSERT INTO dwd_weekly_quote "
                "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, "
                "pct_chg, active_days) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 5)",
                [code, wd, c * 0.99, c * 1.01, c * 0.98, c, 5000.0 + i],
            )

    return calc_date, dates


def _patch_run_calc_gates(monkeypatch, codes: List[str]):
    from backend.etl import orchestrator as orch

    monkeypatch.setattr(
        "backend.fetch.ods_daily.get_all_active_codes",
        lambda con: list(codes),
    )
    monkeypatch.setattr(
        orch, "check_data_completeness",
        lambda con, ts_codes, calc_date=None: {
            "ok": list(ts_codes),
            "missing": {},
            "weekly_fetch": {},
        },
    )
    monkeypatch.setattr(orch, "find_stale_ods_codes", lambda *a, **k: [])
    monkeypatch.setattr(orch, "find_stale_dwd_codes", lambda *a, **k: [])
    monkeypatch.setattr(orch, "_should_skip_calc_idempotent", lambda *a, **k: False)
    monkeypatch.setattr(
        orch, "_filter_delisted",
        lambda con, codes, calc_date: (list(codes), {}),
    )


def _run_calc_route(
    con,
    codes: List[str],
    calc_date: str,
    indicator_filter: Optional[List[str]],
    monkeypatch,
):
    import backend.config as cfg

    for key, val in (
        ("CALC_APPEND", "1"),
        ("CALC_BATCH_APPEND", "1"),
        ("CALC_BATCH_FULL", "1"),
        ("CALC_INCREMENTAL", "1"),
        ("CALC_FORCE_HARD", "1"),
        ("CALC_FAST_SKIP", "0"),
    ):
        monkeypatch.setenv(key, val)
    importlib.reload(cfg)

    _patch_run_calc_gates(monkeypatch, codes)
    from backend.etl.orchestrator import run_calc

    run_calc(
        con,
        ts_codes=None,
        calc_date=calc_date,
        auto_fetch=False,
        force=True,
        indicator_filter=indicator_filter,
    )


def _read_dws_table(
    con,
    table: str,
    codes: List[str],
    calc_date: str,
    float_cols: List[str],
    str_cols: List[str],
) -> Dict[str, pd.Series]:
    cols = ["ts_code", "trade_date"] + float_cols + str_cols
    out: Dict[str, pd.Series] = {}
    for code in codes:
        row = con.execute(
            f"SELECT {', '.join(cols)} FROM {table} "
            "WHERE ts_code = ? AND calc_date = ? ORDER BY trade_date DESC LIMIT 1",
            [code, calc_date],
        ).fetchone()
        assert row is not None, f"{table} missing row for {code}"
        out[code] = pd.Series(dict(zip(cols, row)))
    return out


def _assert_rows_equal(
    oracle: Dict[str, pd.Series],
    actual: Dict[str, pd.Series],
    float_cols: List[str],
    str_cols: List[str],
    label: str,
):
    for code in oracle:
        for col in float_cols:
            a, b = actual[code][col], oracle[code][col]
            if pd.isna(b):
                assert pd.isna(a), f"{label} {code} {col}: expected NaN, got {a}"
            else:
                assert abs(float(a) - float(b)) < ATOL, (
                    f"{label} {code} {col}: |{a} - {b}| >= {ATOL}"
                )
        for col in str_cols:
            assert actual[code][col] == oracle[code][col], (
                f"{label} {code} {col}: {actual[code][col]!r} != {oracle[code][col]!r}"
            )


def _clear_indicator_routes(con, indicators: List[str]):
    for ind in indicators:
        for freq in ("daily", "weekly"):
            tbl = f"dws_{ind}_{freq}"
            con.execute(f"DELETE FROM {tbl}")
        con.execute(
            "DELETE FROM dws_calc_state WHERE indicator = ?", [ind],
        )


def test_narrow_dde_matches_full_after_circ_mv_scenario(monkeypatch):
    """circ_mv-only change path: indicator_filter=['dde'] == full 12-route DDE output."""
    from backend.etl.column_indicator_deps import resolve_run_calc_indicator_filter
    from backend.fetch.fetch_result import FetchResult

    codes = ["NE1.SZ", "NE2.SZ"]
    con = duckdb.connect(":memory:")
    calc_date, _dates = _seed_equiv_db(con, codes)

    fr = FetchResult(
        rows_written=1,
        changed_pairs=[(codes[0], calc_date)],
        changed_field_events=[
            (codes[0], calc_date, "ods_daily_basic", "circ_mv", False),
        ],
    )
    filt = resolve_run_calc_indicator_filter(
        con, fr,
        changed_codes=[codes[0]],
        stale_extra_codes=[],
        qfq_codes=[],
    )
    assert filt == ["dde"]

    _run_calc_route(con, codes, calc_date, indicator_filter=None, monkeypatch=monkeypatch)
    oracle_daily = _read_dws_table(
        con, "dws_dde_daily", codes, calc_date, DDE_FLOAT_COLS, DDE_STR_COLS,
    )
    oracle_weekly = _read_dws_table(
        con, "dws_dde_weekly", codes, calc_date, DDE_FLOAT_COLS, DDE_STR_COLS,
    )

    _clear_indicator_routes(con, ["dde"])
    _run_calc_route(con, codes, calc_date, indicator_filter=["dde"], monkeypatch=monkeypatch)

    narrow_daily = _read_dws_table(
        con, "dws_dde_daily", codes, calc_date, DDE_FLOAT_COLS, DDE_STR_COLS,
    )
    narrow_weekly = _read_dws_table(
        con, "dws_dde_weekly", codes, calc_date, DDE_FLOAT_COLS, DDE_STR_COLS,
    )
    _assert_rows_equal(oracle_daily, narrow_daily, DDE_FLOAT_COLS, DDE_STR_COLS, "dde_daily")
    _assert_rows_equal(oracle_weekly, narrow_weekly, DDE_FLOAT_COLS, DDE_STR_COLS, "dde_weekly")
    con.close()


def test_narrow_volume_kpattern_matches_full_after_vol_scenario(monkeypatch):
    """vol-only change path: narrow kpattern+volume == full-route output for those indicators."""
    from backend.etl.column_indicator_deps import resolve_run_calc_indicator_filter
    from backend.fetch.fetch_result import FetchResult

    codes = ["NV1.SZ"]
    con = duckdb.connect(":memory:")
    calc_date, _dates = _seed_equiv_db(con, codes)

    fr = FetchResult(
        rows_written=1,
        changed_pairs=[(codes[0], calc_date)],
        changed_field_events=[
            (codes[0], calc_date, "ods_daily", "vol", False),
        ],
    )
    filt = resolve_run_calc_indicator_filter(
        con, fr,
        changed_codes=[codes[0]],
        stale_extra_codes=[],
        qfq_codes=[],
    )
    assert sorted(filt) == ["kpattern", "volume"]

    _run_calc_route(con, codes, calc_date, indicator_filter=None, monkeypatch=monkeypatch)
    oracle_vol = _read_dws_table(
        con, "dws_volume_daily", codes, calc_date, VOL_FLOAT_COLS, VOL_STR_COLS,
    )

    _clear_indicator_routes(con, ["volume", "kpattern"])
    _run_calc_route(
        con, codes, calc_date,
        indicator_filter=["kpattern", "volume"],
        monkeypatch=monkeypatch,
    )
    narrow_vol = _read_dws_table(
        con, "dws_volume_daily", codes, calc_date, VOL_FLOAT_COLS, VOL_STR_COLS,
    )
    _assert_rows_equal(oracle_vol, narrow_vol, VOL_FLOAT_COLS, VOL_STR_COLS, "volume_daily")
    con.close()
