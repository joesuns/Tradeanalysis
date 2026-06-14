import logging

import duckdb

from backend.fetch.ods_daily import fetch_by_date_range_parallel


class _FakeClient:
    def call(self, api, **kwargs):
        if api == "trade_cal":
            return [{"cal_date": d} for d in ["20260101", "20260102", "20260103"]]
        return []


def test_parallel_fetch_emits_unified_progress_prefix(caplog, monkeypatch, tmp_path):
    """3 trading days → progress lines use progress fetch.ods: prefix."""
    db_path = str(tmp_path / "test.duckdb")
    monkeypatch.setattr("backend.fetch.ods_daily.DUCKDB_PATH", db_path)

    con = duckdb.connect(db_path)
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT, "
                "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
                "vol DOUBLE, amount DOUBLE, pct_chg DOUBLE, adj_factor DOUBLE, fetched_at TIMESTAMP)")
    con.execute("CREATE TABLE ods_daily_basic (ts_code TEXT, trade_date TEXT, "
                "total_mv DOUBLE, pe_ttm DOUBLE, turnover_rate DOUBLE, volume_ratio DOUBLE, fetched_at TIMESTAMP)")
    con.execute("CREATE TABLE ods_moneyflow (ts_code TEXT, trade_date TEXT, "
                "buy_sm_vol DOUBLE, buy_sm_amount DOUBLE, sell_sm_vol DOUBLE, sell_sm_amount DOUBLE, "
                "buy_md_vol DOUBLE, buy_md_amount DOUBLE, sell_md_vol DOUBLE, sell_md_amount DOUBLE, "
                "buy_lg_vol DOUBLE, buy_lg_amount DOUBLE, sell_lg_vol DOUBLE, sell_lg_amount DOUBLE, "
                "buy_elg_vol DOUBLE, buy_elg_amount DOUBLE, sell_elg_vol DOUBLE, sell_elg_amount DOUBLE, "
                "net_mf_vol DOUBLE, net_mf_amount DOUBLE, fetched_at TIMESTAMP)")
    con.execute("CREATE TABLE dim_date (trade_date TEXT, is_trade_day INTEGER)")
    for d in ["20260101", "20260102", "20260103"]:
        con.execute("INSERT INTO dim_date VALUES (?, 1)", (d,))
    con.close()

    monkeypatch.setattr("backend.fetch.client.TushareClient", lambda: _FakeClient())
    monkeypatch.setattr(
        "backend.fetch.ods_daily._get_trading_days",
        lambda *a, **k: ["20260101", "20260102", "20260103"],
    )

    with caplog.at_level(logging.INFO):
        fetch_by_date_range_parallel(
            "20260101", "20260103", workers=1, ts_codes=[], con=duckdb.connect(db_path),
        )

    progress = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("progress fetch.ods:")]
    assert any("started" in m for m in progress), progress
    assert any("3/3 (100%)" in m for m in progress), progress
