"""SKIP routing must not insert DWS rows for the calc_date."""
import duckdb

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.base import SkipReason
from backend.etl.calc_state import upsert_calc_state
from backend.etl.calc_router import state_signature
from backend.etl.orchestrator import _route_calc
from backend.etl.calc_macd import MACDCalculator
from backend.etl.calc_fast_skip import batch_load_quote_tails
from backend.etl.calc_indicators import quote_tail_columns


def test_route_calc_skip_writes_no_dws_rows():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    ts = "000001.SZ"
    for i in range(260):
        d = f"2020{(i // 28 + 1):02d}{(i % 28 + 1):02d}"[:8]
        if i < 250:
            d = str(20200101 + i)
        con.execute(
            "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
            [str(20200101 + i)],
        )
        c = 10.0 + i * 0.01
        con.execute(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
            "VALUES (?, ?, ?, ?, ?, ?, 1000, 0)",
            [ts, str(20200101 + i), c, c, c, c],
        )
    calc_date = str(20200101 + 259)
    tails = batch_load_quote_tails(con, [ts], "daily", quote_tail_columns())
    df = tails[ts]
    last_td = str(df["trade_date"].max())
    calc = MACDCalculator(con, "daily")
    fp = state_signature(df, last_td, calc.SIGNATURE_COLS)
    upsert_calc_state(
        con, ts, "daily", "macd", last_td, fp, calc_date,
        spec_version=calc.SPEC_VERSION,
    )

    before = con.execute(
        "SELECT COUNT(*) FROM dws_macd_daily WHERE calc_date = ?", [calc_date],
    ).fetchone()[0]

    result = _route_calc(
        con, calc, "macd", "daily", ts, df, calc_date,
        calc_date, None, append_on=True,
    )
    assert result.calculated == 0
    assert SkipReason.FINGERPRINT_MATCH in result.skipped

    after = con.execute(
        "SELECT COUNT(*) FROM dws_macd_daily WHERE calc_date = ?", [calc_date],
    ).fetchone()[0]
    assert before == after == 0
    con.close()
