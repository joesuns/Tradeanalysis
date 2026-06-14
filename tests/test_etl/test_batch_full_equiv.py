"""Golden tests: batch FULL vs per-stock selective FULL (atol=1e-9)."""
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest


def test_calc_batch_full_defaults_on():
    """CALC_BATCH_FULL defaults to enabled when unset."""
    env = os.environ.pop("CALC_BATCH_FULL", None)
    try:
        import importlib
        import backend.config as cfg
        importlib.reload(cfg)
        assert cfg.CALC_BATCH_FULL is True
    finally:
        if env is not None:
            os.environ["CALC_BATCH_FULL"] = env


def test_calc_batch_full_respects_zero():
    os.environ["CALC_BATCH_FULL"] = "0"
    try:
        import importlib
        import backend.config as cfg
        importlib.reload(cfg)
        assert cfg.CALC_BATCH_FULL is False
    finally:
        os.environ.pop("CALC_BATCH_FULL", None)
        import importlib
        import backend.config as cfg
        importlib.reload(cfg)


def _seed_weekly_kpattern_stocks(con, codes, weeks=300, bars_per_week=3):
    """Rolling weekly bars + dim_date week-end flags for multiple stocks."""
    from backend.db.schema import create_all_tables

    create_all_tables(con)
    for code in codes:
        con.execute(
            "INSERT INTO dim_stock (ts_code, name, list_date, is_st) "
            "VALUES (?, ?, '20200101', 0)",
            [code, code],
        )

    d = date(2025, 1, 6)
    week_end_dates = []
    for wk, code in enumerate(codes):
        rng = np.random.default_rng(300 + wk)
        close = 10.0 + np.cumsum(rng.normal(0, 0.15, weeks * bars_per_week))
        bar_i = 0
        d = date(2025, 1, 6)
        for _ in range(weeks):
            for b in range(bars_per_week):
                td = d.strftime("%Y%m%d")
                c = float(close[bar_i])
                o, h, lo = c * 0.998, c + 0.2, c - 0.2
                vol = 1_000_000.0 + bar_i * 1000
                pct = 0.0 if bar_i == 0 else (c - float(close[bar_i - 1])) / float(close[bar_i - 1]) * 100.0
                con.execute(
                    "INSERT INTO dwd_weekly_quote "
                    "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, pct_chg) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [code, td, o, h, lo, c, vol, pct],
                )
                is_we = 1 if b == bars_per_week - 1 else 0
                exists = con.execute(
                    "SELECT 1 FROM dim_date WHERE trade_date = ?", [td],
                ).fetchone()
                if not exists:
                    con.execute(
                        "INSERT INTO dim_date (trade_date, is_week_end, is_trade_day) "
                        "VALUES (?, ?, 1)",
                        [td, is_we],
                    )
                if is_we:
                    week_end_dates.append(td)
                bar_i += 1
                d += timedelta(days=1)
            d += timedelta(days=7 - bars_per_week)
    return sorted(set(week_end_dates))


def _kpattern_weekly_rows(con, codes, calc_date):
    cols = [
        "yang_bao_yin", "yang_ke_yin", "mu_bei_xian", "bi_lei_zhen",
        "gao_kai_chang_yin", "yin_bao_yang", "yin_ke_yang", "strength",
    ]
    out = {}
    for code in codes:
        row = con.execute(
            f"SELECT {', '.join(cols)} FROM dws_kpattern_weekly "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, calc_date, calc_date],
        ).fetchone()
        out[code] = pd.Series(dict(zip(cols, row)))
    return out


def test_batch_full_kpattern_weekly_matches_selective_full(monkeypatch):
    """Batch FULL kpattern weekly == per-stock calc_stock_pipeline_selective FULL."""
    import importlib
    import duckdb

    import backend.config as cfg
    monkeypatch.setenv("CALC_BATCH_FULL", "1")
    importlib.reload(cfg)

    from backend.etl.calc_batch_append import run_batch_full_phase
    from backend.etl.calc_executor import build_work_queue, group_by_indicator
    from backend.etl.calc_fast_skip import batch_load_quote_tails
    from backend.etl.calc_indicators import quote_tail_columns
    from backend.etl.orchestrator import (
        calc_stock_pipeline_selective,
        resolve_recalc_start,
    )

    codes = ["KF0.SZ", "KF1.SZ", "KF2.SZ"]
    con = duckdb.connect(":memory:")
    week_ends = _seed_weekly_kpattern_stocks(con, codes)
    calc_date = week_ends[-1]

    stock_modes = {
        code: {("kpattern", "weekly"): ("FULL", [])}
        for code in codes
    }
    wq = build_work_queue(stock_modes, set())
    full_groups = group_by_indicator(wq.full_items)
    assert len(full_groups) == 1
    assert set(full_groups[("kpattern", "weekly")]) == set(codes)

    weekly_tails = batch_load_quote_tails(
        con, codes, "weekly", quote_tail_columns("weekly"),
    )
    batch_ctx = {
        "stock_modes": stock_modes,
        "daily_tails": {},
        "weekly_tails": weekly_tails,
        "dde_daily": {},
        "dde_weekly": {},
        "state_map": {},
    }

    daily_recalc = resolve_recalc_start(con, calc_date, "daily")
    weekly_recalc = resolve_recalc_start(con, calc_date, "weekly")
    oracle = {}
    for code in codes:
        calc_stock_pipeline_selective(
            con, code, calc_date, daily_recalc, weekly_recalc,
            run_keys={("kpattern", "weekly")},
            run_modes={("kpattern", "weekly"): ("FULL", [])},
        )
        oracle[code] = _kpattern_weekly_rows(con, [code], calc_date)[code]

    con.execute(
        "DELETE FROM dws_kpattern_weekly WHERE calc_date = ?", [calc_date],
    )
    con.execute("DELETE FROM dws_calc_state WHERE indicator = 'kpattern' AND freq = 'weekly'")

    result = run_batch_full_phase(con, calc_date, full_groups, batch_ctx)
    assert result["batch_full_items"] == 3
    assert result["full_by_indicator"] == {"kpattern_weekly": 3}

    batch_rows = _kpattern_weekly_rows(con, codes, calc_date)
    float_cols = ["strength"]
    str_cols = [
        "yang_bao_yin", "yang_ke_yin", "mu_bei_xian", "bi_lei_zhen",
        "gao_kai_chang_yin", "yin_bao_yang", "yin_ke_yang",
    ]
    for code in codes:
        batch_row = batch_rows[code]
        single_row = oracle[code]
        for col in float_cols:
            a, b = batch_row[col], single_row[col]
            if pd.isna(b):
                assert pd.isna(a), f"{code} {col}: expected NaN, got {a}"
            else:
                assert abs(a - b) < 1e-9, f"{code} {col}: |{a} - {b}| >= 1e-9"
        for col in str_cols:
            assert batch_row[col] == single_row[col], (
                f"{code} {col}: batch={batch_row[col]!r} single={single_row[col]!r}"
            )
    con.close()


def test_run_batch_append_phase_clears_full_items_after_batch_full(monkeypatch):
    """Integrated path: batch FULL handles mass FULL; chunk gets remainder only."""
    import importlib
    import duckdb

    import backend.config as cfg
    from backend.etl.calc_batch_append import run_batch_append_phase

    codes = ["INT0.SZ", "INT1.SZ"]
    con = duckdb.connect(":memory:")
    _seed_weekly_kpattern_stocks(con, codes)
    calc_date = con.execute(
        "SELECT MAX(trade_date) FROM dim_date WHERE is_week_end = 1",
    ).fetchone()[0]

    modes = {
        code: {("kpattern", "weekly"): ("FULL", [])}
        for code in codes
    }
    monkeypatch.setenv("CALC_APPEND", "1")
    monkeypatch.setenv("CALC_BATCH_APPEND", "1")
    monkeypatch.setenv("CALC_BATCH_FULL", "1")
    importlib.reload(cfg)
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.preflight_stock_modes_with_fps",
        lambda ts_code, state_map, *tails, **kwargs: (modes[ts_code], {}),
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.load_calc_state_batch",
        lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.build_skip_state_records",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.upsert_calc_state_batch",
        lambda *a, **k: None,
    )

    ctx = run_batch_append_phase(con, codes, calc_date)
    assert ctx is not None
    assert ctx["batch_full_items"] == 2
    assert ctx["full_by_indicator"] == {"kpattern_weekly": 2}
    assert ctx["full_items"] == []
    assert ctx["chunk_work_items"] == 0
    con.close()
