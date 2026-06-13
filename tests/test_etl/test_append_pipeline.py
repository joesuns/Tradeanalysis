"""Integration test: append-only routing wired into calc_stock_pipeline.

Verifies that a new trading day routes PricePosition to APPEND (not FULL),
writes only the new bar (no history rewrite), and that the new bar's value
matches a full recompute over the whole DWD (atol=1e-9).
"""
import duckdb
import numpy as np
import pandas as pd

from backend.db.schema import create_all_tables
from backend.etl.orchestrator import calc_stock_pipeline
from backend.etl.calc_state import load_calc_state
from backend.etl.calc_price_position import PricePositionCalculator
from backend.etl.base import load_quote_groups

TS = "T.SZ"


def _dates(n):
    # Valid YYYYMMDD — MACD weekly B4 calls datetime.strptime(calc_date, "%Y%m%d").
    base = pd.Timestamp("2020-01-01")
    return [(base + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def _setup(con, n):
    create_all_tables(con)
    dates = _dates(n)
    con.executemany(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
        [(d,) for d in dates],
    )
    rng = np.random.default_rng(123)
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    rows = []
    for i, d in enumerate(dates):
        c = float(close[i])
        rows.append((TS, d, c, c + 0.1, c - 0.1, c, 1000.0 + i, 0))
    con.executemany(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return dates


def _add_bar(con, date, close):
    con.execute(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
        [date],
    )
    con.execute(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        [TS, date, close, close + 0.1, close - 0.1, close, 5000.0],
    )


def test_pipeline_routes_new_day_to_append_and_matches_full():
    con = duckdb.connect(":memory:")
    dates = _setup(con, 260)  # dates[0..259]
    t1 = dates[259]

    # Run 1: establishes baseline (FULL) for the stock.
    calc_stock_pipeline(con, TS, calc_date=t1, daily_recalc=t1, weekly_recalc=None)
    st1 = load_calc_state(con, "daily", "priceposition", [TS]).get(TS)
    assert st1 is not None and st1["last_trade_date"] == t1

    # New trading day arrives.
    t2 = (pd.Timestamp(dates[-1]) + pd.Timedelta(days=1)).strftime("%Y%m%d")
    _add_bar(con, t2, 12.34)

    calc_stock_pipeline(con, TS, calc_date=t2, daily_recalc=t2, weekly_recalc=None)

    # State advanced -> APPEND happened (FULL would also advance, but see below
    # that history was NOT rewritten, which is the APPEND signature).
    st2 = load_calc_state(con, "daily", "priceposition", [TS]).get(TS)
    assert st2["last_trade_date"] == t2

    # History not rewritten: the t1 row keeps its original calc_date (t1),
    # only the new bar t2 was written at calc_date t2.
    rows = con.execute(
        "SELECT trade_date, calc_date FROM dws_price_position_daily "
        "WHERE ts_code = ? ORDER BY trade_date", [TS]
    ).fetchall()
    cd = {td: c for td, c in rows}
    assert cd[t1] == t1, f"t1 row was rewritten: calc_date={cd[t1]}"
    assert cd[t2] == t2

    # New bar value equals a full recompute over the whole DWD.
    groups = load_quote_groups(con, "dwd_daily_quote", "daily",
                               ["trade_date", "close_qfq"], [TS])
    full = PricePositionCalculator(con, "daily")._compute_positions(groups[TS].copy())
    full_last = full[full["trade_date"] == t2].iloc[0]

    dws = con.execute(
        "SELECT price_position_60d, price_position_120d, price_position_250d "
        "FROM dws_price_position_daily WHERE ts_code = ? AND trade_date = ? "
        "AND calc_date = ?", [TS, t2, t2]
    ).fetchone()
    # DWS columns are REAL (float32); compare at storage precision. The exact
    # append-vs-full equivalence (atol=1e-9) is locked in test_append_calc.py.
    for got, col in zip(dws, ["price_position_60d", "price_position_120d",
                              "price_position_250d"]):
        exp = full_last[col]
        if pd.isna(exp):
            assert got is None or pd.isna(got)
        else:
            assert abs(np.float32(got) - np.float32(exp)) < 1e-4, \
                f"{col}: {got} != {exp}"

    con.close()


def test_route_calc_skips_state_when_no_rows_written():
    """FULL/APPEND 均未写入 DWS 时不得刷新 dws_calc_state。"""
    from unittest.mock import MagicMock, patch
    import pandas as pd

    from backend.db.schema import create_all_tables
    from backend.etl.base import CalcResult
    from backend.etl.calc_state import load_calc_state
    from backend.etl.orchestrator import _route_calc

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    df = pd.DataFrame({"trade_date": ["20260101", "20260102"], "close_qfq": [10.0, 11.0]})
    calc = MagicMock()
    calc.SIGNATURE_COLS = ["close_qfq"]
    calc.calculate.return_value = CalcResult()
    calc.append_calculate.return_value = CalcResult()

    with patch("backend.etl.calc_router.classify_calc_mode", return_value=("FULL", [])):
        _route_calc(con, calc, "macd", "daily", "T.SZ", df, "20260102",
                    None, {}, append_on=True)

    assert load_calc_state(con, "daily", "macd", ["T.SZ"]).get("T.SZ") is None

    with patch("backend.etl.calc_router.classify_calc_mode", return_value=("APPEND", ["20260102"])):
        _route_calc(con, calc, "macd", "daily", "T.SZ", df, "20260102",
                    None, {}, append_on=True)

    assert load_calc_state(con, "daily", "macd", ["T.SZ"]).get("T.SZ") is None
    con.close()


def test_append_calculate_no_phantom_state_when_insert_empty():
    """write_start/write_end 过滤后无行 → calculated=0，不得刷新 state。"""
    from unittest.mock import patch
    import pandas as pd

    from backend.db.schema import create_all_tables
    from backend.etl.calc_state import load_calc_state, upsert_calc_state
    from backend.etl.calc_macd import MACDCalculator
    from backend.etl.orchestrator import _route_calc

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    df = pd.DataFrame({
        "trade_date": ["20260101", "20260102"],
        "close_qfq": [10.0, 11.0],
    })
    upsert_calc_state(con, "T.SZ", "daily", "macd",
                      last_trade_date="20260101", history_fp="fp0",
                      calc_date="20260101")
    calc = MACDCalculator(con, "daily")

    with patch("backend.etl.calc_router.classify_calc_mode",
               return_value=("APPEND", ["20260102"])):
        with patch.object(calc, "_insert", return_value=0):
            _route_calc(con, calc, "macd", "daily", "T.SZ", df, "20260102",
                        None, {}, append_on=True)

    st = load_calc_state(con, "daily", "macd", ["T.SZ"]).get("T.SZ")
    assert st["last_trade_date"] == "20260101"
    assert st["history_fp"] == "fp0"
    con.close()
