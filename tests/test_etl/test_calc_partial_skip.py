"""Golden: selective pipeline matches full pipeline for non-SKIP indicators."""
import duckdb
import numpy as np

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.orchestrator import calc_stock_pipeline, calc_stock_pipeline_selective
from backend.etl.calc_state import load_calc_state_batch
from backend.etl.calc_fast_skip import (
    batch_load_quote_tails,
    batch_load_dde_tails,
    preflight_stock_modes_v2,
    partition_preflight_modes,
)
from backend.etl.calc_indicators import CALC_ROUTE_SPECS, quote_tail_columns

TS = "PART.SZ"


def _setup(con, n=260):
    create_all_tables(con)
    ensure_calc_state_table(con)
    dates = [(np.datetime64("2020-01-01") + np.timedelta64(i, "D"))
             .astype("datetime64[D]").astype(str).replace("-", "")
             for i in range(n)]
    for d in dates:
        con.execute(
            "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
            [d],
        )
    rng = np.random.default_rng(3)
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    for i, d in enumerate(dates):
        c = float(close[i])
        con.execute(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
            "VALUES (?, ?, ?, ?, ?, ?, 1000, 0)",
            [TS, d, c, c, c, c],
        )
        con.execute(
            "INSERT INTO dwd_daily_moneyflow "
            "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, sell_elg_vol, "
            " total_vol, net_mf_amount) VALUES (?, ?, 10, 5, 3, 2, 1000, 1.5)",
            [TS, d],
        )
    return dates


def test_selective_pipeline_matches_full_for_run_keys():
    """Two isolated fresh DBs with identical seed → selective == full per run_key."""

    def _run_full():
        con = duckdb.connect(":memory:")
        dates = _setup(con)
        calc_date = dates[-1]
        out = calc_stock_pipeline(
            con, TS, calc_date, daily_recalc=calc_date, weekly_recalc=None,
        )
        con.close()
        return out

    def _run_selective(run_keys):
        con = duckdb.connect(":memory:")
        dates = _setup(con)
        calc_date = dates[-1]
        out = calc_stock_pipeline_selective(
            con, TS, calc_date, daily_recalc=calc_date, weekly_recalc=None,
            run_keys=run_keys,
        )
        con.close()
        return out

    full = _run_full()
    full_map = {(n, f): r for n, f, r in full}
    run_keys = {(n, f) for n, f, _ in full if f == "daily"}

    sel = _run_selective(run_keys)
    sel_map = {(n, f): r for n, f, r in sel}

    for key in run_keys:
        assert sel_map[key].calculated == full_map[key].calculated
        assert sel_map[key].skipped.keys() == full_map[key].skipped.keys()
