"""Tests for per-stock incremental fetch in backend.fetch.ods_daily."""

import duckdb
import pytest
from backend.fetch.ods_daily import (
    _drop_suspension_gaps,
    _get_missing_days_for_stock,
    _get_missing_ranges_per_stock,
    fetch_stocks_incremental,
)


def test_get_missing_days_for_stock():
    """per-stock detection: only filter out dates already present for THAT stock."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    # 000001.SZ has 5 days
    for d in ["20260101", "20260102", "20260103", "20260104", "20260105"]:
        con.execute("INSERT INTO ods_daily VALUES ('000001.SZ', ?)", (d,))
    # 000002.SZ has only 1 day
    con.execute("INSERT INTO ods_daily VALUES ('000002.SZ', '20260101')")

    all_days = ["20260101", "20260102", "20260103", "20260104", "20260105"]

    missing_1 = _get_missing_days_for_stock(con, "000001.SZ", all_days)
    missing_2 = _get_missing_days_for_stock(con, "000002.SZ", all_days)

    assert len(missing_1) == 0, f"000001 should have no missing days, got {missing_1}"
    assert len(missing_2) == 4, f"000002 should miss 4 days, got {len(missing_2)}"
    assert "20260102" in missing_2
    con.close()


def test_get_missing_ranges_merges_consecutive():
    """Consecutive missing days should be merged into a single range."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    # Only the last day has data
    con.execute("INSERT INTO ods_daily VALUES ('TEST.SZ', '20260105')")

    days = ["20260101", "20260102", "20260103", "20260104", "20260105"]
    ranges = _get_missing_ranges_per_stock(con, "TEST.SZ", days)

    assert len(ranges) == 1, f"Expected 1 missing range, got {len(ranges)}: {ranges}"
    assert ranges[0] == ("20260101", "20260104")
    con.close()


def test_get_missing_ranges_no_gap():
    """When data is complete, no missing ranges should be returned."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    for d in ["20260101", "20260102", "20260103"]:
        con.execute("INSERT INTO ods_daily VALUES ('FULL.SZ', ?)", (d,))

    days = ["20260101", "20260102", "20260103"]
    ranges = _get_missing_ranges_per_stock(con, "FULL.SZ", days)
    assert len(ranges) == 0
    con.close()


def test_fetch_stocks_incremental_skips_when_complete():
    """When data is already complete, only trade_cal is called — no data APIs."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    con.execute(
        "CREATE TABLE ods_daily_basic (ts_code TEXT, trade_date TEXT, circ_mv REAL)",
    )
    con.execute(
        "CREATE TABLE ods_moneyflow (ts_code TEXT, trade_date TEXT, net_amount_dc REAL)",
    )
    for d in ["20260101", "20260102", "20260103"]:
        con.execute("INSERT INTO ods_daily VALUES ('FULL.SZ', ?)", (d,))
        con.execute(
            "INSERT INTO ods_daily_basic VALUES ('FULL.SZ', ?, 1.0)", (d,),
        )
        con.execute(
            "INSERT INTO ods_moneyflow VALUES ('FULL.SZ', ?, 1.0)", (d,),
        )

    api_calls = []

    class FakeClient:
        def call(self, api, **kwargs):
            api_calls.append(api)
            if api == "trade_cal":
                return [{"cal_date": d} for d in
                        ["20260101", "20260102", "20260103"]]
            return []

    n = fetch_stocks_incremental(
        FakeClient(), con, ["FULL.SZ"], start="20260101", end="20260103"
    )
    assert n == 0, f"Should not have fetched any data, got {n}"
    # trade_cal is always called; daily/daily_basic/moneyflow should NOT be called
    data_apis = [a for a in api_calls if a != "trade_cal"]
    assert len(data_apis) == 0, f"Should not have called data APIs, got {data_apis}"
    con.close()


def test_get_missing_days_empty_trading_days():
    """Empty trading day list should return empty list."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    result = _get_missing_days_for_stock(con, "TEST.SZ", [])
    assert result == []
    con.close()


def test_drop_suspension_gaps_internal_dropped():
    """Internal gaps between first/last ODS dates are suspension — skip fetch."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    for d in ["20260101", "20260105"]:
        con.execute("INSERT INTO ods_daily VALUES ('SUSP.SZ', ?)", (d,))

    missing = ["20260102", "20260103", "20260104"]
    assert _drop_suspension_gaps(con, "SUSP.SZ", missing) == []
    con.close()


def test_drop_suspension_gaps_keeps_head_and_tail():
    """Head (<first_ods) and tail (>last_ods) gaps remain fetchable."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    for d in ["20260103", "20260105"]:
        con.execute("INSERT INTO ods_daily VALUES ('GAP.SZ', ?)", (d,))

    missing = ["20260101", "20260102", "20260106", "20260107"]
    assert _drop_suspension_gaps(con, "GAP.SZ", missing) == missing
    con.close()


def test_drop_suspension_gaps_no_ods_keeps_all():
    """No ODS rows → first fetch; keep all missing days."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    missing = ["20260101", "20260102", "20260103"]
    assert _drop_suspension_gaps(con, "NEW.SZ", missing) == missing
    con.close()


def test_get_missing_ranges_skips_internal_suspension():
    """Internal suspension gaps produce no fetch ranges."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    for d in ["20260101", "20260105"]:
        con.execute("INSERT INTO ods_daily VALUES ('SUSP.SZ', ?)", (d,))

    days = ["20260101", "20260102", "20260103", "20260104", "20260105"]
    ranges = _get_missing_ranges_per_stock(con, "SUSP.SZ", days)
    assert ranges == []
    con.close()


def test_get_missing_ranges_tail_still_fetched():
    """Tail gaps beyond last ODS date are still fetched."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    for d in ["20260101", "20260102", "20260103", "20260104"]:
        con.execute("INSERT INTO ods_daily VALUES ('TAIL.SZ', ?)", (d,))

    days = ["20260101", "20260102", "20260103", "20260104",
            "20260105", "20260106"]
    ranges = _get_missing_ranges_per_stock(con, "TAIL.SZ", days)
    assert ranges == [("20260105", "20260106")]
    con.close()


# ── _get_trading_days local dim_date preference (A3) ──


def _seed_dim_date(con, rows):
    """rows: list of (trade_date, is_trade_day)."""
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER,
            is_week_end INTEGER, is_month_end INTEGER, is_year_end INTEGER,
            year INTEGER, quarter INTEGER, month INTEGER, week_of_year INTEGER
        )
    """)
    for d, flag in rows:
        con.execute(
            "INSERT INTO dim_date VALUES (?,?,0,0,0,2026,1,1,1)", (d, flag))


def test_get_trading_days_uses_local_dim_date_when_covered():
    """dim_date covers the range → query local, do NOT hit trade_cal API."""
    from backend.fetch.ods_daily import _get_trading_days

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    _seed_dim_date(con, [
        ("20260101", 1), ("20260102", 1),
        ("20260103", 0),  # non-trading day → excluded
        ("20260104", 1),
    ])

    api_calls = []

    class FakeClient:
        def call(self, api, **kwargs):
            api_calls.append(api)
            return []

    days = _get_trading_days(FakeClient(), "20260101", "20260104", con=con)
    assert "trade_cal" not in api_calls, "should use local dim_date, not API"
    assert days == ["20260101", "20260102", "20260104"]
    con.close()


def test_get_trading_days_falls_back_to_api_when_dim_date_partial():
    """dim_date doesn't cover the requested end → fall back to trade_cal API."""
    from backend.fetch.ods_daily import _get_trading_days

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    # dim_date only goes to 20260102 but range asks through 20260104
    _seed_dim_date(con, [("20260101", 1), ("20260102", 1)])

    api_calls = []

    class FakeClient:
        def call(self, api, **kwargs):
            api_calls.append(api)
            if api == "trade_cal":
                return [{"cal_date": d} for d in
                        ["20260101", "20260102", "20260103", "20260104"]]
            return []

    days = _get_trading_days(FakeClient(), "20260101", "20260104", con=con)
    assert "trade_cal" in api_calls, "partial dim_date → must fall back to API"
    assert days == ["20260101", "20260102", "20260103", "20260104"]
    con.close()


# ── _get_trading_days with ts_codes ──


def test_get_trading_days_with_ts_codes_partial_coverage():
    """Date skipped ONLY when ALL target stocks have data (per-stock check)."""
    from backend.fetch.ods_daily import _get_trading_days

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT,
            open REAL, high REAL, low REAL, close REAL, vol REAL,
            amount REAL, pct_chg REAL, adj_factor REAL)
    """)
    # Stock A: all 3 dates. Stock B: only 20260101.
    for d in ["20260101", "20260102", "20260103"]:
        con.execute(
            "INSERT INTO ods_daily VALUES ('A.SZ', ?, 10,11,9,10,100,1000,0,1)", (d,))
    con.execute(
        "INSERT INTO ods_daily VALUES ('B.SZ', '20260101', 10,11,9,10,100,1000,0,1)")

    class FakeClient:
        def call(self, api, **kwargs):
            return [{"cal_date": d} for d in ["20260101", "20260102", "20260103"]]

    # Without ts_codes: date-global — ANY stock has data → date skipped
    days_all = _get_trading_days(FakeClient(), "20260101", "20260103", con=con)
    assert len(days_all) == 0, f"date-global: all dates have SOME data, got {days_all}"

    # With ts_codes=["A.SZ"]: A has all 3 dates → all skipped
    days_a = _get_trading_days(FakeClient(), "20260101", "20260103",
                               con=con, ts_codes=["A.SZ"])
    assert len(days_a) == 0, f"A.SZ has all dates, got {days_a}"

    # With ts_codes=["B.SZ"]: B has 20260101, missing 01/02 and 01/03
    # → 20260101 is covered → skipped; 01/02 and 01/03 returned
    days_b = _get_trading_days(FakeClient(), "20260101", "20260103",
                               con=con, ts_codes=["B.SZ"])
    assert len(days_b) == 2, (
        f"B has 20260101 (covered), missing 01/02 and 01/03, got {days_b}")
    assert "20260102" in days_b
    assert "20260103" in days_b

    # With ts_codes=["A.SZ", "B.SZ"]: 20260101 both have → covered → skipped
    # 20260102 and 20260103: A has, B doesn't → not covered → returned
    days_both = _get_trading_days(FakeClient(), "20260101", "20260103",
                                  con=con, ts_codes=["A.SZ", "B.SZ"])
    assert len(days_both) == 2, (
        f"Both: only 20260101 fully covered, 01/02 and 01/03 returned, got {days_both}")
    assert "20260102" in days_both
    assert "20260103" in days_both

    con.close()


# ── _validate_ods_batch ──


def test_validate_ods_batch_all_valid():
    """Clean data should all pass validation and be returned intact."""
    from backend.fetch.ods_daily import _validate_ods_batch

    recs = [
        {"ts_code": "A.SZ", "trade_date": "20260101",
         "open": 10, "high": 12, "low": 9, "close": 11, "vol": 100, "amount": 1000},
        {"ts_code": "B.SZ", "trade_date": "20260101",
         "open": 20, "high": 22, "low": 19, "close": 21, "vol": 200, "amount": 2000},
    ]
    valid_recs, invalid = _validate_ods_batch(recs, "daily")
    assert len(valid_recs) == 2
    assert invalid == 0
    # Returned records are the original dicts (filter, not copy of fields)
    assert valid_recs[0]["ts_code"] == "A.SZ"
    assert valid_recs[1]["ts_code"] == "B.SZ"


def test_validate_ods_batch_rejects_bad_ohlc():
    """high < low or missing close should be filtered out of the returned list."""
    from backend.fetch.ods_daily import _validate_ods_batch

    recs = [
        {"ts_code": "A.SZ", "trade_date": "20260101",
         "open": 10, "high": 12, "low": 9, "close": 11, "vol": 100, "amount": 1000},
        {"ts_code": "B.SZ", "trade_date": "20260101",
         "open": 10, "high": 8, "low": 9, "close": 11, "vol": 100,
         "amount": 1000},  # high < low
        {"ts_code": "C.SZ", "trade_date": "20260101",
         "open": 10, "high": 12, "low": 9, "close": None, "vol": 100,
         "amount": 1000},  # missing close
    ]
    valid_recs, invalid = _validate_ods_batch(recs, "daily")
    assert len(valid_recs) == 1
    assert invalid == 2
    # Only the clean row survives
    assert valid_recs[0]["ts_code"] == "A.SZ"


def test_validate_ods_batch_missing_required_field():
    """Missing open → filtered out, empty list returned."""
    from backend.fetch.ods_daily import _validate_ods_batch

    recs = [
        {"ts_code": "D.SZ", "trade_date": "20260101",
         "open": None, "high": 12, "low": 9, "close": 11,
         "vol": 100, "amount": 1000},  # missing open
    ]
    valid_recs, invalid = _validate_ods_batch(recs, "daily")
    assert valid_recs == []
    assert invalid == 1


# ── data quality gate wired into fetch paths ──


def test_fetch_by_date_range_drops_invalid_rows(db_with_schema):
    """date-batched fetch must filter out invalid daily rows before INSERT."""
    from backend.fetch.ods_daily import fetch_by_date_range

    con = db_with_schema

    class FakeClient:
        def call(self, api, **kwargs):
            if api == "trade_cal":
                return [{"cal_date": "20260101"}]
            if api == "adj_factor":
                return [{"ts_code": "GOOD.SZ", "adj_factor": 1.0},
                        {"ts_code": "BAD.SZ", "adj_factor": 1.0}]
            if api == "daily":
                return [
                    {"ts_code": "GOOD.SZ", "trade_date": "20260101",
                     "open": 10, "high": 12, "low": 9, "close": 11,
                     "vol": 100, "amount": 1000, "pct_chg": 0.0},
                    {"ts_code": "BAD.SZ", "trade_date": "20260101",
                     "open": 10, "high": 8, "low": 9, "close": 11,  # high < low
                     "vol": 100, "amount": 1000, "pct_chg": 0.0},
                ]
            return []

    fetch_by_date_range(FakeClient(), con, "20260101", "20260101")
    codes = [r[0] for r in con.execute(
        "SELECT ts_code FROM ods_daily ORDER BY ts_code").fetchall()]
    assert codes == ["GOOD.SZ"], f"BAD.SZ should be dropped, got {codes}"


def test_fetch_stocks_incremental_drops_invalid_rows(db_with_schema):
    """stock-batched incremental fetch must filter out invalid daily rows."""
    from backend.fetch.ods_daily import fetch_stocks_incremental

    con = db_with_schema

    class FakeClient:
        def call(self, api, **kwargs):
            if api == "trade_cal":
                return [{"cal_date": "20260101"}]
            if api == "adj_factor":
                return [{"trade_date": "20260101", "adj_factor": 1.0}]
            if api == "daily":
                return [
                    {"ts_code": "BAD.SZ", "trade_date": "20260101",
                     "open": 10, "high": 8, "low": 9, "close": 11,  # high < low
                     "vol": 100, "amount": 1000, "pct_chg": 0.0},
                ]
            return []

    fetch_stocks_incremental(FakeClient(), con, ["BAD.SZ"],
                             start="20260101", end="20260101")
    n = con.execute("SELECT COUNT(*) FROM ods_daily").fetchone()[0]
    assert n == 0, f"invalid BAD.SZ row should be dropped, got {n} rows"


def test_fetch_stocks_incremental_bulk_inserts_all_layers(db_with_schema):
    """stock-batched 增量批量写入 daily/basic/moneyflow，且 adj_factor 正确映射。"""
    from backend.fetch.ods_daily import fetch_stocks_incremental

    con = db_with_schema

    class FakeClient:
        def call(self, api, **kwargs):
            if api == "trade_cal":
                return [{"cal_date": "20260101"}, {"cal_date": "20260102"}]
            if api == "adj_factor":
                return [
                    {"trade_date": "20260101", "adj_factor": 1.5},
                    {"trade_date": "20260102", "adj_factor": 1.6},
                ]
            if api == "daily":
                return [
                    {"ts_code": "AAA.SZ", "trade_date": "20260101",
                     "open": 10, "high": 12, "low": 9, "close": 11,
                     "vol": 100, "amount": 1000, "pct_chg": 0.0},
                    {"ts_code": "AAA.SZ", "trade_date": "20260102",
                     "open": 11, "high": 13, "low": 10, "close": 12,
                     "vol": 200, "amount": 2000, "pct_chg": 9.0},
                ]
            if api == "daily_basic":
                return [
                    {"ts_code": "AAA.SZ", "trade_date": "20260101",
                     "total_mv": 5000, "pe_ttm": 15, "turnover_rate": 0.5,
                     "volume_ratio": 1.1},
                    {"ts_code": "AAA.SZ", "trade_date": "20260102",
                     "total_mv": 5100, "pe_ttm": 16, "turnover_rate": 0.6,
                     "volume_ratio": 1.2},
                ]
            if api == "moneyflow":
                return [
                    {"ts_code": "AAA.SZ", "trade_date": "20260101",
                     "net_mf_amount": 123.0},
                    {"ts_code": "AAA.SZ", "trade_date": "20260102",
                     "net_mf_amount": 456.0},
                ]
            return []

    fetch_stocks_incremental(FakeClient(), con, ["AAA.SZ"],
                             start="20260101", end="20260102")

    daily = con.execute(
        "SELECT trade_date, close, adj_factor FROM ods_daily ORDER BY trade_date"
    ).fetchall()
    assert [d[0] for d in daily] == ["20260101", "20260102"]
    assert daily[0][1] == pytest.approx(11.0)
    assert daily[0][2] == pytest.approx(1.5)
    assert daily[1][2] == pytest.approx(1.6)

    nb = con.execute("SELECT COUNT(*) FROM ods_daily_basic").fetchone()[0]
    assert nb == 2
    nm = con.execute("SELECT COUNT(*) FROM ods_moneyflow").fetchone()[0]
    assert nm == 2


def test_fetch_stocks_incremental_backfills_net_amount_dc_only(db_with_schema):
    """已有 moneyflow 行但 net_amount_dc 为空时，仅补拉 moneyflow_dc。"""
    from backend.fetch.ods_daily import fetch_stocks_incremental

    con = db_with_schema
    con.execute("""
        INSERT INTO ods_daily VALUES
        ('DC.SZ','20240102',10,12,9,11,100,1000,0,1.0,now())
    """)
    con.execute("""
        INSERT INTO ods_moneyflow
        (ts_code, trade_date, net_mf_amount, net_amount_dc, fetched_at)
        VALUES ('DC.SZ','20240102',999.0,NULL,now())
    """)

    calls = []

    class FakeClient:
        def call(self, api, **kwargs):
            calls.append((api, kwargs))
            if api == "trade_cal":
                return [{"cal_date": "20240102"}]
            if api == "moneyflow_dc":
                return [{"trade_date": "20240102", "net_amount": -1088.0}]
            return []

    n = fetch_stocks_incremental(
        FakeClient(), con, ["DC.SZ"], start="20240102", end="20240102",
    )
    assert n == 1
    assert ("moneyflow_dc", {"ts_code": "DC.SZ", "start_date": "20240102",
                             "end_date": "20240102"}) in calls
    assert not any(c[0] == "daily" for c in calls)
    assert not any(c[0] == "moneyflow" for c in calls)
    dc = con.execute(
        "SELECT net_amount_dc FROM ods_moneyflow WHERE ts_code='DC.SZ'"
    ).fetchone()[0]
    assert dc == pytest.approx(-1088.0)
