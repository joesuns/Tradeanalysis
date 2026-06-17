"""Golden tests: batch append vs per-stock append_calculate (atol=1e-9)."""
import os


def test_calc_batch_append_defaults_on():
    """CALC_BATCH_APPEND defaults to enabled when unset."""
    env = os.environ.pop("CALC_BATCH_APPEND", None)
    try:
        import importlib
        import backend.config as cfg
        importlib.reload(cfg)
        assert cfg.CALC_BATCH_APPEND is True
    finally:
        if env is not None:
            os.environ["CALC_BATCH_APPEND"] = env


def test_calc_batch_append_respects_zero():
    os.environ["CALC_BATCH_APPEND"] = "0"
    try:
        import importlib
        import backend.config as cfg
        importlib.reload(cfg)
        assert cfg.CALC_BATCH_APPEND is False
    finally:
        os.environ.pop("CALC_BATCH_APPEND", None)


def test_partition_stocks_by_mode_groups_append_and_full():
    from backend.etl.calc_batch_append import partition_stocks_by_mode

    modes = {
        "A.SZ": {("macd", "daily"): "APPEND", ("ma", "daily"): "SKIP"},
        "B.SZ": {("macd", "daily"): "FULL", ("ma", "daily"): "APPEND"},
    }
    append, full, skip = partition_stocks_by_mode(modes, "macd", "daily")
    assert set(append) == {"A.SZ"}
    assert set(full) == {"B.SZ"}
    assert skip == []


def test_load_zone_seeds_batch_matches_fetch_zone_seed():
    import duckdb

    from backend.etl.calc_batch_seeds import load_zone_seeds_batch
    from backend.etl.calc_volume import VolumeCalculator

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_volume_daily (
            ts_code TEXT, trade_date TEXT, calc_date TEXT, zone TEXT
        )
    """)
    con.execute("""
        INSERT INTO dws_volume_daily VALUES
        ('V.SZ','20260606','20260607','normal'),
        ('V.SZ','20260607','20260607','explosive'),
        ('W.SZ','20260606','20260607','low_volume')
    """)
    batch = load_zone_seeds_batch(con, "dws_volume_daily", ["V.SZ", "W.SZ"], "20260608")
    calc = VolumeCalculator(con, "daily")
    assert batch["V.SZ"] == "explosive"
    assert batch["W.SZ"] == calc._fetch_zone_seed("W.SZ", "20260608")
    con.close()


def test_load_ema_seeds_batch_matches_single():
    import duckdb

    from backend.etl.calc_batch_seeds import load_ema_seeds_batch

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_macd_daily (
            ts_code TEXT, trade_date TEXT, calc_date TEXT,
            ema_12 DOUBLE, ema_26 DOUBLE, dea DOUBLE
        )
    """)
    con.execute("""
        INSERT INTO dws_macd_daily VALUES
        ('A.SZ','20260101','20260105', 10.0, 20.0, 0.1),
        ('A.SZ','20260102','20260105', 10.5, 20.2, 0.12),
        ('B.SZ','20260101','20260105', 30.0, 40.0, 0.2),
        ('B.SZ','20260102','20260105', 30.1, 40.1, 0.21)
    """)
    recalc_start = "20260102"
    batch = load_ema_seeds_batch(
        con, "dws_macd_daily", ["A.SZ", "B.SZ"], recalc_start,
        ("ema_12", "ema_26", "dea"),
    )
    assert batch["A.SZ"]["ema_12"] == 10.0  # bar before 20260102
    assert batch["B.SZ"]["dea"] == 0.2
    con.close()


def _macd_test_dates(n):
    return [f"41{i:06d}" for i in range(n)]


def _setup_macd_batch_stocks(con, codes, n=300):
    """Three stocks × n bars in dim_date + dwd; FULL MACD baseline through bar n-2."""
    import numpy as np

    from backend.db.schema import create_all_tables
    from backend.etl.base import load_quote_groups
    from backend.etl.calc_macd import MACDCalculator

    create_all_tables(con)
    dates = _macd_test_dates(n)
    con.executemany(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
        [(d,) for d in dates],
    )
    rows = []
    for j, code in enumerate(codes):
        rng = np.random.default_rng(100 + j)
        close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
        for i, d in enumerate(dates):
            c = float(close[i])
            rows.append((code, d, c, c + 0.1, c - 0.1, c, 1000.0 + i, 0))
    con.executemany(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )

    baseline_date = dates[-2]
    calc = MACDCalculator(con, "daily")
    groups = load_quote_groups(
        con, "dwd_daily_quote", "daily",
        ["trade_date", "close_qfq"], list(codes),
    )
    for code in codes:
        hist = groups[code]
        hist = hist[hist["trade_date"] <= baseline_date].reset_index(drop=True)
        calc.calculate(
            [code], baseline_date,
            recalc_start=hist["trade_date"].iloc[0],
            quote_groups={code: hist},
        )
    return dates


def _macd_row(con, ts_code, trade_date, calc_date):
    import pandas as pd

    row = con.execute(
        "SELECT ema_12, ema_26, dif, dea, macd_bar, trend_strength, "
        "divergence, zone, trend, turning_point, alert "
        "FROM dws_macd_daily WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
        [ts_code, trade_date, calc_date],
    ).fetchone()
    cols = [
        "ema_12", "ema_26", "dif", "dea", "macd_bar", "trend_strength",
        "divergence", "zone", "trend", "turning_point", "alert",
    ]
    return pd.Series(dict(zip(cols, row)))


def test_batch_append_macd_loads_seeds_once(monkeypatch):
    """batch_append_macd calls load_ema_seeds_batch once for full ts_codes list."""
    import pandas as pd

    from backend.etl.calc_batch_append import batch_append_macd

    calls = []

    def spy(con, table, ts_codes, before_td, cols):
        calls.append(list(ts_codes))
        return {c: {"ema_12": 1.0, "ema_26": 2.0, "dea": 0.1} for c in ts_codes}

    monkeypatch.setattr(
        "backend.etl.calc_batch_append.load_ema_seeds_batch", spy,
    )
    monkeypatch.setattr(
        "backend.etl.calc_batch_append.insert_dws_batch_multi",
        lambda *a, **k: 2,
    )

    codes = ["S0.SZ", "S1.SZ", "S2.SZ"]
    df = pd.DataFrame({"trade_date": ["20260607", "20260608"], "close_qfq": [10.0, 10.1]})
    groups = {c: df for c in codes}
    new_bars = {c: ["20260608"] for c in codes}

    batch_append_macd(None, "daily", codes, "20260608", groups, new_bars)
    assert len(calls) == 1
    assert set(calls[0]) == set(codes)


def test_batch_append_macd_matches_per_stock_append():
    """Batch MACD append == per-stock append_calculate on new bar (3 stocks)."""
    import duckdb
    import numpy as np
    import pandas as pd

    from backend.etl.base import load_quote_groups
    from backend.etl.calc_batch_append import batch_append_macd
    from backend.etl.calc_macd import MACDCalculator

    codes = ["M0.SZ", "M1.SZ", "M2.SZ"]
    con = duckdb.connect(":memory:")
    dates = _setup_macd_batch_stocks(con, codes, n=300)
    new_td = f"41{300:06d}"  # one bar beyond baseline history (through dates[-1])
    calc_date = new_td
    tail_n = 80

    groups = load_quote_groups(
        con, "dwd_daily_quote", "daily",
        ["trade_date", "close_qfq"], codes,
    )
    quote_tails = {}
    for code in codes:
        g = groups[code]
        quote_tails[code] = g[g["trade_date"] >= dates[-tail_n]].reset_index(drop=True)

    con.execute(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
        [new_td],
    )
    # Append genuinely new bar to DWD for each stock.
    for j, code in enumerate(codes):
        c = float(quote_tails[code].iloc[-1]["close_qfq"] + 0.05 * (j + 1))
        con.execute(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            [code, new_td, c, c + 0.1, c - 0.1, c, 9999.0],
        )
        quote_tails[code] = pd.concat([
            quote_tails[code],
            pd.DataFrame({
                "trade_date": [new_td],
                "close_qfq": [c],
            }),
        ], ignore_index=True)

    calc = MACDCalculator(con, "daily")
    per_stock = {}
    for code in codes:
        state = {"last_trade_date": dates[-1]}
        calc.append_calculate(code, quote_tails[code], [new_td], calc_date, state)
        per_stock[code] = _macd_row(con, code, new_td, calc_date)

    # Clear per-stock writes; run batch path on all three.
    con.execute(
        "DELETE FROM dws_macd_daily WHERE trade_date = ? AND calc_date = ?",
        [new_td, calc_date],
    )
    new_bars_map = {code: [new_td] for code in codes}
    batch_append_macd(
        con, "daily", codes, calc_date, quote_tails, new_bars_map,
    )

    float_cols = ["ema_12", "ema_26", "dif", "dea", "macd_bar", "trend_strength"]
    str_cols = ["divergence", "zone", "trend", "turning_point", "alert"]
    for code in codes:
        batch_row = _macd_row(con, code, new_td, calc_date)
        single_row = per_stock[code]
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


def _setup_quote_baseline(con, CalcCls, codes, n=200, ohlcv=False):
    """dim_date + dwd rows + FULL baseline through bar n-2 for one calculator."""
    import numpy as np

    from backend.db.schema import create_all_tables
    from backend.etl.base import load_quote_groups

    create_all_tables(con)
    dates = [f"43{i:06d}" for i in range(n)]
    con.executemany(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
        [(d,) for d in dates],
    )
    rows = []
    for j, code in enumerate(codes):
        rng = np.random.default_rng(200 + j)
        close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
        for i, d in enumerate(dates):
            c = float(close[i])
            pct = 0.0 if i == 0 else (c - float(close[i - 1])) / float(close[i - 1]) * 100.0
            if ohlcv:
                o = c * (1 + 0.001)
                h = c + 0.15
                lo = c - 0.15
                rows.append((code, d, o, h, lo, c, 1000.0 + i, pct, 0))
            else:
                rows.append((code, d, c, c + 0.1, c - 0.1, c, 1000.0 + i, 0))
    if ohlcv:
        con.executemany(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, pct_chg, "
            "is_suspended) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    else:
        con.executemany(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    baseline_date = dates[-2]
    calc = CalcCls(con, "daily")
    cols = ["trade_date", "close_qfq"]
    if ohlcv:
        cols = [
            "trade_date", "open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol", "pct_chg",
        ]
    groups = load_quote_groups(
        con, "dwd_daily_quote", "daily", cols, list(codes),
    )
    for code in codes:
        hist = groups[code]
        hist = hist[hist["trade_date"] <= baseline_date].reset_index(drop=True)
        calc.calculate(
            [code], baseline_date,
            recalc_start=hist["trade_date"].iloc[0],
            quote_groups={code: hist},
        )
    return dates


def _append_new_bar(con, codes, dates, quote_tails, tail_n=80):
    """Insert one new DWD bar per code and extend quote_tails frames."""
    import pandas as pd

    new_td = f"43{len(dates):06d}"
    con.execute(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
        [new_td],
    )
    for j, code in enumerate(codes):
        c = float(quote_tails[code].iloc[-1]["close_qfq"] + 0.03 * (j + 1))
        o, h, lo = c * 0.999, c + 0.12, c - 0.12
        vol = 8888.0
        con.execute(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            [code, new_td, o, h, lo, c, vol],
        )
        prev_c = float(quote_tails[code].iloc[-1]["close_qfq"])
        pct = (c - prev_c) / prev_c * 100.0 if prev_c else 0.0
        extra = {"trade_date": [new_td], "close_qfq": [c]}
        if "open_qfq" in quote_tails[code].columns:
            extra.update(
                open_qfq=[o], high_qfq=[h], low_qfq=[lo], vol=[vol], pct_chg=[pct],
            )
        quote_tails[code] = pd.concat(
            [quote_tails[code], pd.DataFrame(extra)], ignore_index=True,
        )
    return new_td


def _assert_rows_match(con, table, cols, codes, trade_date, calc_date, per_stock):
    import pandas as pd

    for code in codes:
        row = con.execute(
            f"SELECT {', '.join(cols)} FROM {table} "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, trade_date, calc_date],
        ).fetchone()
        batch_row = pd.Series(dict(zip(cols, row)))
        single_row = per_stock[code]
        for col in cols:
            a, b = batch_row[col], single_row[col]
            if isinstance(b, str) or b is None:
                assert a == b, f"{code} {col}: batch={a!r} single={b!r}"
            elif pd.isna(b):
                assert pd.isna(a), f"{code} {col}: expected NaN, got {a}"
            else:
                assert abs(a - b) < 1e-9, f"{code} {col}: |{a} - {b}| >= 1e-9"


def test_batch_append_ma_matches_per_stock_append():
    import duckdb
    import pandas as pd

    from backend.etl.base import load_quote_groups
    from backend.etl.calc_batch_append import batch_append_ma
    from backend.etl.calc_ma import MACalculator

    codes = ["MA0.SZ"]
    con = duckdb.connect(":memory:")
    dates = _setup_quote_baseline(con, MACalculator, codes, n=200)
    groups = load_quote_groups(
        con, "dwd_daily_quote", "daily", ["trade_date", "close_qfq"], codes,
    )
    quote_tails = {
        c: groups[c][groups[c]["trade_date"] >= dates[-80]].reset_index(drop=True)
        for c in codes
    }
    new_td = _append_new_bar(con, codes, dates, quote_tails)
    calc_date = new_td
    calc = MACalculator(con, "daily")
    cols = ["ma_5", "ma_10", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope",
            "alignment", "turning_point"]
    per_stock = {}
    for code in codes:
        calc.append_calculate(
            code, quote_tails[code], [new_td], calc_date,
            {"last_trade_date": dates[-1]},
        )
        row = con.execute(
            f"SELECT {', '.join(cols)} FROM dws_ma_daily "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, new_td, calc_date],
        ).fetchone()
        per_stock[code] = pd.Series(dict(zip(cols, row)))

    con.execute(
        "DELETE FROM dws_ma_daily WHERE trade_date = ? AND calc_date = ?",
        [new_td, calc_date],
    )
    batch_append_ma(
        con, "daily", codes, calc_date, quote_tails, {c: [new_td] for c in codes},
    )
    _assert_rows_match(con, "dws_ma_daily", cols, codes, new_td, calc_date, per_stock)
    con.close()


def test_batch_append_priceposition_matches_per_stock_append():
    import duckdb
    import pandas as pd

    from backend.etl.base import load_quote_groups
    from backend.etl.calc_batch_append import batch_append_priceposition
    from backend.etl.calc_price_position import PricePositionCalculator

    codes = ["PP0.SZ"]
    con = duckdb.connect(":memory:")
    dates = _setup_quote_baseline(con, PricePositionCalculator, codes, n=260)
    groups = load_quote_groups(
        con, "dwd_daily_quote", "daily", ["trade_date", "close_qfq"], codes,
    )
    quote_tails = {
        c: groups[c][groups[c]["trade_date"] >= dates[-120]].reset_index(drop=True)
        for c in codes
    }
    new_td = _append_new_bar(con, codes, dates, quote_tails)
    calc_date = new_td
    calc = PricePositionCalculator(con, "daily")
    cols = ["price_position_60d", "price_position_120d", "price_position_250d"]
    per_stock = {}
    for code in codes:
        calc.append_calculate(
            code, quote_tails[code], [new_td], calc_date,
            {"last_trade_date": dates[-1]},
        )
        row = con.execute(
            f"SELECT {', '.join(cols)} FROM dws_price_position_daily "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, new_td, calc_date],
        ).fetchone()
        per_stock[code] = pd.Series(dict(zip(cols, row)))

    con.execute(
        "DELETE FROM dws_price_position_daily WHERE trade_date = ? AND calc_date = ?",
        [new_td, calc_date],
    )
    batch_append_priceposition(
        con, "daily", codes, calc_date, quote_tails, {c: [new_td] for c in codes},
    )
    _assert_rows_match(
        con, "dws_price_position_daily", cols, codes, new_td, calc_date, per_stock,
    )
    con.close()


def test_batch_append_kpattern_matches_per_stock_append():
    import duckdb
    import pandas as pd

    from backend.etl.base import load_quote_groups
    from backend.etl.calc_batch_append import batch_append_kpattern
    from backend.etl.calc_kpattern import KPatternCalculator

    codes = ["KP0.SZ"]
    con = duckdb.connect(":memory:")
    dates = _setup_quote_baseline(con, KPatternCalculator, codes, n=200, ohlcv=True)
    cols_q = [
        "trade_date", "open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol", "pct_chg",
    ]
    groups = load_quote_groups(con, "dwd_daily_quote", "daily", cols_q, codes)
    quote_tails = {
        c: groups[c][groups[c]["trade_date"] >= dates[-80]].reset_index(drop=True)
        for c in codes
    }
    new_td = _append_new_bar(con, codes, dates, quote_tails)
    calc_date = new_td
    calc = KPatternCalculator(con, "daily")
    cols = ["yang_bao_yin", "yang_ke_yin", "strength"]
    per_stock = {}
    for code in codes:
        calc.append_calculate(
            code, quote_tails[code], [new_td], calc_date,
            {"last_trade_date": dates[-1]},
        )
        row = con.execute(
            f"SELECT {', '.join(cols)} FROM dws_kpattern_daily "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, new_td, calc_date],
        ).fetchone()
        per_stock[code] = pd.Series(dict(zip(cols, row)))

    con.execute(
        "DELETE FROM dws_kpattern_daily WHERE trade_date = ? AND calc_date = ?",
        [new_td, calc_date],
    )
    batch_append_kpattern(
        con, "daily", codes, calc_date, quote_tails, {c: [new_td] for c in codes},
    )
    _assert_rows_match(con, "dws_kpattern_daily", cols, codes, new_td, calc_date, per_stock)
    con.close()


def test_batch_append_kpattern_via_quote_tail_columns():
    """Regression: batch tails from quote_tail_columns must include pct_chg."""
    import duckdb

    from backend.etl.calc_batch_append import batch_append_kpattern
    from backend.etl.calc_fast_skip import batch_load_quote_tails
    from backend.etl.calc_indicators import quote_tail_columns
    from backend.etl.calc_kpattern import KPatternCalculator

    codes = ["KP1.SZ"]
    con = duckdb.connect(":memory:")
    dates = _setup_quote_baseline(con, KPatternCalculator, codes, n=200, ohlcv=True)
    quote_tails = batch_load_quote_tails(
        con, codes, "daily", quote_tail_columns("daily"), window=80,
    )
    assert "pct_chg" in quote_tails[codes[0]].columns

    new_td = _append_new_bar(con, codes, dates, quote_tails)
    calc_date = new_td
    batch_append_kpattern(
        con, "daily", codes, calc_date, quote_tails, {c: [new_td] for c in codes},
    )
    cnt = con.execute(
        "SELECT COUNT(*) FROM dws_kpattern_daily "
        "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
        [codes[0], new_td, calc_date],
    ).fetchone()[0]
    assert cnt == 1
    con.close()


def test_batch_append_volume_matches_per_stock_append():
    import duckdb
    import pandas as pd

    from backend.etl.base import load_quote_groups
    from backend.etl.calc_batch_append import batch_append_volume
    from backend.etl.calc_volume import VolumeCalculator

    codes = ["VOL0.SZ"]
    con = duckdb.connect(":memory:")
    dates = _setup_quote_baseline(con, VolumeCalculator, codes, n=200, ohlcv=True)
    cols_q = ["trade_date", "close_qfq", "vol"]
    groups = load_quote_groups(con, "dwd_daily_quote", "daily", cols_q, codes)
    quote_tails = {
        c: groups[c][groups[c]["trade_date"] >= dates[-120]].reset_index(drop=True)
        for c in codes
    }
    new_td = _append_new_bar(con, codes, dates, quote_tails)
    calc_date = new_td
    calc = VolumeCalculator(con, "daily")
    cols = ["volume_ratio", "pct_vol_rank", "trend", "trend_strength",
            "divergence", "zone"]
    per_stock = {}
    for code in codes:
        calc.append_calculate(
            code, quote_tails[code], [new_td], calc_date,
            {"last_trade_date": dates[-1]},
        )
        row = con.execute(
            f"SELECT {', '.join(cols)} FROM dws_volume_daily "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, new_td, calc_date],
        ).fetchone()
        per_stock[code] = pd.Series(dict(zip(cols, row)))

    con.execute(
        "DELETE FROM dws_volume_daily WHERE trade_date = ? AND calc_date = ?",
        [new_td, calc_date],
    )
    batch_append_volume(
        con, "daily", codes, calc_date, quote_tails, {c: [new_td] for c in codes},
    )
    _assert_rows_match(con, "dws_volume_daily", cols, codes, new_td, calc_date, per_stock)
    con.close()


def _setup_dde_baseline(con, codes, n=200):
    import numpy as np

    from backend.db.schema import create_all_tables
    from backend.etl.calc_dde import DDECalculator

    create_all_tables(con)
    dates = [f"44{i:06d}" for i in range(n)]
    con.executemany(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
        [(d,) for d in dates],
    )
    q_rows, m_rows = [], []
    for j, code in enumerate(codes):
        rng = np.random.default_rng(300 + j)
        close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
        for i, d in enumerate(dates):
            c = float(close[i])
            tv = float(rng.uniform(10000, 50000))
            buy_lg = tv * 0.3
            sell_lg = tv * 0.25
            buy_elg = tv * 0.1
            sell_elg = tv * 0.08
            net_mf = buy_lg + buy_elg - sell_lg - sell_elg
            q_rows.append((code, d, c, c + 0.1, c - 0.1, c, 1000.0 + i, 0))
            m_rows.append((code, d, buy_lg, sell_lg, buy_elg, sell_elg, tv, net_mf))
    con.executemany(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        q_rows,
    )
    con.executemany(
        "INSERT INTO dwd_daily_moneyflow "
        "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, "
        "sell_elg_vol, total_vol, net_mf_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        m_rows,
    )
    baseline_date = dates[-2]
    calc = DDECalculator(con, "daily")
    for code in codes:
        calc.calculate([code], baseline_date, recalc_start=dates[0])
    return dates


def test_batch_append_dde_matches_per_stock_append():
    import duckdb
    import pandas as pd

    from backend.etl.calc_batch_append import batch_append_dde
    from backend.etl.calc_dde import DDECalculator

    codes = ["DDE0.SZ"]
    con = duckdb.connect(":memory:")
    dates = _setup_dde_baseline(con, codes, n=200)
    calc = DDECalculator(con, "daily")
    dde_groups = calc._load_daily_batch(codes)
    dde_tails = {
        c: dde_groups[c][dde_groups[c]["trade_date"] >= dates[-80]].reset_index(drop=True)
        for c in codes
    }
    new_td = f"44{len(dates):06d}"
    con.execute(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
        [new_td],
    )
    rng_close = float(dde_tails[codes[0]].iloc[-1]["close_qfq"]) + 0.05
    tv = 45000.0
    con.execute(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        [codes[0], new_td, rng_close, rng_close + 0.1, rng_close - 0.1, rng_close, 9000.0],
    )
    con.execute(
        "INSERT INTO dwd_daily_moneyflow "
        "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, "
        "sell_elg_vol, total_vol, net_mf_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [codes[0], new_td, 10000, 8000, 3000, 2000, tv, 3000.0],
    )
    dde_tails[codes[0]] = pd.concat([
        dde_tails[codes[0]],
        pd.DataFrame({
            "trade_date": [new_td],
            "buy_lg_vol": [10000.0], "sell_lg_vol": [8000.0],
            "buy_elg_vol": [3000.0], "sell_elg_vol": [2000.0],
            "total_vol": [tv], "net_mf_amount": [3000.0],
            "close_qfq": [rng_close],
        }),
    ], ignore_index=True)

    calc_date = new_td
    cols = ["ddx", "ddx2", "trend", "trend_strength", "divergence", "alert"]
    per_stock = {}
    for code in codes:
        calc.append_calculate(
            code, dde_tails[code], [new_td], calc_date,
            {"last_trade_date": dates[-1]},
        )
        row = con.execute(
            f"SELECT {', '.join(cols)} FROM dws_dde_daily "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, new_td, calc_date],
        ).fetchone()
        per_stock[code] = pd.Series(dict(zip(cols, row)))

    con.execute(
        "DELETE FROM dws_dde_daily WHERE trade_date = ? AND calc_date = ?",
        [new_td, calc_date],
    )
    batch_append_dde(
        con, "daily", codes, calc_date, dde_tails, {c: [new_td] for c in codes},
    )
    _assert_rows_match(con, "dws_dde_daily", cols, codes, new_td, calc_date, per_stock)
    con.close()


def _setup_dde_baseline_with_dc(con, codes, n=200):
    """DDE baseline with net_amount_dc/circ_mv; dc net declines (600831-class trend down)."""
    import numpy as np

    from backend.db.schema import create_all_tables
    from backend.etl.calc_dde import DDECalculator

    create_all_tables(con)
    dates = [f"44{i:06d}" for i in range(n)]
    con.executemany(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
        [(d,) for d in dates],
    )
    net_decline = np.linspace(5000, -5000, n)
    mv = np.full(n, 1e9)
    q_rows, m_rows = [], []
    for j, code in enumerate(codes):
        rng = np.random.default_rng(301 + j)
        close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
        for i, d in enumerate(dates):
            c = float(close[i])
            tv = float(rng.uniform(10000, 50000))
            buy_lg = tv * 0.3
            sell_lg = tv * 0.25
            buy_elg = tv * 0.1
            sell_elg = tv * 0.08
            net_mf = buy_lg + buy_elg - sell_lg - sell_elg
            q_rows.append((code, d, c, c + 0.1, c - 0.1, c, 1000.0 + i, mv[i], mv[i], 0))
            m_rows.append((
                code, d, buy_lg, sell_lg, buy_elg, sell_elg, tv, net_mf,
                float(net_decline[i]),
            ))
    con.executemany(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, "
        "total_mv, circ_mv, is_suspended) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        q_rows,
    )
    con.executemany(
        "INSERT INTO dwd_daily_moneyflow "
        "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, "
        "sell_elg_vol, total_vol, net_mf_amount, net_amount_dc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        m_rows,
    )
    baseline_date = dates[-2]
    calc = DDECalculator(con, "daily")
    for code in codes:
        calc.calculate([code], baseline_date, recalc_start=dates[0])
    return dates


def test_batch_append_dde_daily_trend_matches_full(monkeypatch):
    """Daily batch APPEND trend must match per-stock append (vector path, dc+circ B4).

    M5 investigation: batch_append_dde vector path preserves net_amount_dc/circ_mv from
    tail load and calls the same _compute_dde_derived as non-vector. On declining-dc
    fixtures both write trend=down, not the stale up seen pre-repair on 20260612.
    """
    import duckdb
    import pandas as pd

    monkeypatch.setenv("CALC_VECTOR_APPEND", "1")

    from backend.etl.calc_batch_append import batch_append_dde
    from backend.etl.calc_dde import DDECalculator

    codes = ["DDEd.SZ"]
    con = duckdb.connect(":memory:")
    dates = _setup_dde_baseline_with_dc(con, codes, n=200)
    calc = DDECalculator(con, "daily")
    dde_groups = calc._load_daily_batch(codes)
    dde_tails = {
        c: dde_groups[c][dde_groups[c]["trade_date"] >= dates[-80]].reset_index(drop=True)
        for c in codes
    }
    new_td = f"45{len(dates):06d}"
    con.execute(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
        [new_td],
    )
    rng_close = float(dde_tails[codes[0]].iloc[-1]["close_qfq"]) + 0.05
    tv = 45000.0
    new_dc = -5500.0
    con.execute(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, "
        "total_mv, circ_mv, is_suspended) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        [codes[0], new_td, rng_close, rng_close + 0.1, rng_close - 0.1, rng_close,
         9000.0, 1e9, 1e9],
    )
    con.execute(
        "INSERT INTO dwd_daily_moneyflow "
        "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, "
        "sell_elg_vol, total_vol, net_mf_amount, net_amount_dc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [codes[0], new_td, 10000, 8000, 3000, 2000, tv, 3000.0, new_dc],
    )
    dde_tails[codes[0]] = pd.concat([
        dde_tails[codes[0]],
        pd.DataFrame({
            "trade_date": [new_td],
            "buy_lg_vol": [10000.0], "sell_lg_vol": [8000.0],
            "buy_elg_vol": [3000.0], "sell_elg_vol": [2000.0],
            "total_vol": [tv], "net_mf_amount": [3000.0],
            "net_amount_dc": [new_dc],
            "close_qfq": [rng_close],
            "total_mv": [1e9], "circ_mv": [1e9],
        }),
    ], ignore_index=True)

    calc_date = new_td
    per_stock = {}
    for code in codes:
        calc.append_calculate(
            code, dde_tails[code], [new_td], calc_date,
            {"last_trade_date": dates[-1]},
        )
        row = con.execute(
            "SELECT trend FROM dws_dde_daily "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, new_td, calc_date],
        ).fetchone()
        per_stock[code] = row[0]
        assert row[0] == "down", f"append_calculate trend should be down, got {row[0]}"

    con.execute(
        "DELETE FROM dws_dde_daily WHERE trade_date = ? AND calc_date = ?",
        [new_td, calc_date],
    )
    batch_append_dde(
        con, "daily", codes, calc_date, dde_tails, {c: [new_td] for c in codes},
    )
    for code in codes:
        got = con.execute(
            "SELECT trend FROM dws_dde_daily "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, new_td, calc_date],
        ).fetchone()[0]
        assert got == per_stock[code], (
            f"batch_append_dde trend {got!r} != append {per_stock[code]!r}"
        )
        assert got == "down"
    con.close()


def _insert_weekly_quote(con, code, trade_date, close):
    con.execute(
        "INSERT OR REPLACE INTO dwd_weekly_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, amount, "
        "pct_chg, total_mv, pe_ttm, turnover_rate, volume_ratio, active_days) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            code, trade_date, close, close + 0.1, close - 0.1, close,
            1000.0, 100.0, 0.0, 2e6, None, None, None, 5,
        ],
    )


def _setup_dde_weekly_with_dc(con, codes, n_days=400):
    """Daily history with net_amount_dc/circ_mv + week-end markers (real calendar dates)."""
    import numpy as np
    import pandas as pd

    from backend.db.schema import create_all_tables
    from backend.etl.calc_dde import DDECalculator

    create_all_tables(con)
    dates = pd.date_range("2023-09-11", periods=n_days, freq="B").strftime("%Y%m%d").tolist()
    week_ends = []
    q_rows, m_rows = [], []
    for j, code in enumerate(codes):
        rng = np.random.default_rng(400 + j)
        close = 10.0 + np.cumsum(rng.normal(0, 0.2, len(dates)))
        for i, d in enumerate(dates):
            c = float(close[i])
            tv = float(rng.uniform(10000, 50000))
            buy_lg = tv * 0.3
            sell_lg = tv * 0.25
            buy_elg = tv * 0.1
            sell_elg = tv * 0.08
            net_mf = buy_lg + buy_elg - sell_lg - sell_elg
            is_we = 1 if (i + 1) % 5 == 0 else 0
            con.execute(
                "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) "
                "VALUES (?, 1, ?)",
                [d, is_we],
            )
            q_rows.append((code, d, c, c + 0.1, c - 0.1, c, 1000.0 + i, 2e6, 1e6, 0))
            m_rows.append(
                (code, d, buy_lg, sell_lg, buy_elg, sell_elg, tv, net_mf, net_mf * 0.01),
            )
            if is_we:
                week_ends.append(d)
                _insert_weekly_quote(con, code, d, c)
    con.executemany(
        "INSERT INTO dwd_daily_quote "
        "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, "
        "total_mv, circ_mv, is_suspended) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        q_rows,
    )
    con.executemany(
        "INSERT INTO dwd_daily_moneyflow "
        "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, "
        "sell_elg_vol, total_vol, net_mf_amount, net_amount_dc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        m_rows,
    )
    calc = DDECalculator(con, "weekly")
    baseline_date = week_ends[-2]
    calc.calculate(codes, baseline_date, recalc_start=week_ends[0])
    return dates, week_ends


def test_batch_append_dde_weekly_matches_per_stock_append():
    """Weekly batch APPEND must use _weekly_trend_from_daily (daily_for_trend)."""
    import duckdb
    import pandas as pd

    from backend.etl.calc_batch_append import batch_append_dde
    from backend.etl.calc_dde import DDECalculator

    codes = ["DDEw.SZ"]
    con = duckdb.connect(":memory:")
    dates, week_ends = _setup_dde_weekly_with_dc(con, codes, n_days=400)
    calc = DDECalculator(con, "weekly")
    dde_groups = calc._load_weekly_batch(codes)
    dde_tails = {
        c: dde_groups[c][dde_groups[c]["trade_date"] >= week_ends[-80]].reset_index(
            drop=True,
        )
        for c in codes
    }

    # New week: 5 business days + week-end (continue calendar after baseline)
    import pandas as pd

    last = pd.Timestamp(dates[-1])
    new_days = pd.date_range(last + pd.Timedelta(days=1), periods=5, freq="B")
    new_days = [d.strftime("%Y%m%d") for d in new_days]
    new_we = new_days[-1]
    for i, d in enumerate(new_days):
        is_we = 1 if i == 4 else 0
        con.execute(
            "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) "
            "VALUES (?, 1, ?)",
            [d, is_we],
        )
        c = 12.0 + i * 0.1
        tv = 40000.0 + i * 100
        net_mf = 500.0 + i
        con.execute(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, "
            "total_mv, circ_mv, is_suspended) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            [codes[0], d, c, c + 0.1, c - 0.1, c, 9000.0, 2e6, 1e6],
        )
        con.execute(
            "INSERT INTO dwd_daily_moneyflow "
            "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, "
            "sell_elg_vol, total_vol, net_mf_amount, net_amount_dc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [codes[0], d, 10000, 8000, 3000, 2000, tv, net_mf, net_mf * 0.01],
        )
    _insert_weekly_quote(con, codes[0], new_we, 12.4)

    dde_groups = calc._load_weekly_batch(codes)
    dde_tails = {
        c: dde_groups[c][dde_groups[c]["trade_date"] >= week_ends[-80]].reset_index(
            drop=True,
        )
        for c in codes
    }
    tail = dde_tails[codes[0]]
    calc_date = new_we
    per_stock = {}
    for code in codes:
        calc.append_calculate(
            code, tail, [new_we], calc_date,
            {"last_trade_date": week_ends[-1]},
        )
        row = con.execute(
            "SELECT trend FROM dws_dde_weekly "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, new_we, calc_date],
        ).fetchone()
        per_stock[code] = row[0]

    con.execute(
        "DELETE FROM dws_dde_weekly WHERE trade_date = ? AND calc_date = ?",
        [new_we, calc_date],
    )
    batch_append_dde(
        con, "weekly", codes, calc_date, dde_tails, {c: [new_we] for c in codes},
    )
    for code in codes:
        got = con.execute(
            "SELECT trend FROM dws_dde_weekly "
            "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
            [code, new_we, calc_date],
        ).fetchone()[0]
        assert got == per_stock[code]
    con.close()


def test_run_batch_append_phase_pure_append_empty_chunk(monkeypatch):
    """When all stocks are APPEND-only, chunk_codes should be empty."""
    import importlib
    import duckdb

    import backend.config as cfg
    from backend.etl.base import CalcResult
    from backend.etl.calc_batch_append import run_batch_append_phase
    from backend.etl.calc_macd import MACDCalculator

    monkeypatch.setenv("CALC_APPEND", "1")
    monkeypatch.setenv("CALC_BATCH_APPEND", "1")
    importlib.reload(cfg)

    codes = ["BA.SZ", "BB.SZ"]
    con = duckdb.connect(":memory:")
    dates = _setup_macd_batch_stocks(con, codes, n=260)
    new_td = f"41{260:06d}"

    def fake_preflight(ts_code, state_map, daily_q, weekly_q, daily_dde, weekly_dde, **kwargs):
        return {("macd", "daily"): ("APPEND", [new_td])}, {}

    batch_calls = []

    def fake_batch_macd(*args, **kwargs):
        batch_calls.append(kwargs.get("ts_codes") or args[2])
        n = len(args[2]) if len(args) > 2 else 0
        return CalcResult(calculated=n), []

    route = [("macd", "daily", MACDCalculator, ["close_qfq"], "quote")]
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.preflight_stock_modes_with_fps", fake_preflight,
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_quote_tails",
        lambda _c, ts_codes, freq, cols: {
            c: __import__("pandas").DataFrame({
                "trade_date": dates[-80:] + [new_td],
                "close_qfq": [10.0] * 81,
            }) for c in ts_codes
        },
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_dde_tails",
        lambda _c, ts_codes, freq: {c: __import__("pandas").DataFrame() for c in ts_codes},
    )
    from backend.etl.calc_batch_append import BATCH_APPEND_FNS
    monkeypatch.setitem(BATCH_APPEND_FNS, "macd", fake_batch_macd)
    monkeypatch.setattr("backend.etl.calc_indicators.CALC_ROUTE_SPECS", route)

    ctx = run_batch_append_phase(con, codes, new_td)
    assert ctx is not None
    assert ctx["chunk_codes"] == []
    assert len(batch_calls) == 1
    assert set(batch_calls[0]) == set(codes)
    con.close()


def test_batch_append_loop_uses_single_insert(monkeypatch):
    """_batch_append_loop collects rows then calls insert_dws_batch_multi once."""
    import pandas as pd

    from backend.etl.base import CalcResult
    from backend.etl import calc_batch_append as mod

    calls = {"multi": 0, "single": 0}

    def fake_multi(*args, **kwargs):
        calls["multi"] += 1
        return 2

    def fake_single(*args, **kwargs):
        calls["single"] += 1
        return 1

    monkeypatch.setattr(mod, "insert_dws_batch_multi", fake_multi)
    monkeypatch.setattr(mod, "insert_dws_batch", fake_single)

    class FakeCalc:
        con = None
        dws_table = "dws_ma_daily"
        SIGNATURE_COLS = ["close_qfq"]

        def _insert(self, *args, **kwargs):
            raise AssertionError("per-stock _insert must not be called")

    df = pd.DataFrame({"trade_date": ["20260608"], "close_qfq": [10.0]})
    data_groups = {"A.SZ": df, "B.SZ": df}
    new_bars_map = {"A.SZ": ["20260608"], "B.SZ": ["20260608"]}
    dws_cols = [
        "ts_code", "trade_date", "ma_5", "ma_10", "calc_date",
        "input_fingerprint", "spec_version",
    ]
    float_cols = ["ma_5", "ma_10"]

    mod._batch_append_loop(
        FakeCalc(), ["A.SZ", "B.SZ"], "20260608", data_groups, new_bars_map,
        lambda c, code, frame, bars: frame,
        dws_cols=dws_cols, float_cols=float_cols,
    )
    assert calls["multi"] == 1
    assert calls["single"] == 0


def test_batch_append_loop_emits_compute_progress(caplog, monkeypatch):
    """batch_compute 批算循环应输出带中文标签的进度，避免假卡死。"""
    import logging
    import pandas as pd

    from backend.etl import calc_batch_append as mod

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(mod, "insert_dws_batch_multi", lambda *a, **k: 1)

    class FakeCalc:
        con = None
        dws_table = "dws_ma_daily"
        SIGNATURE_COLS = ["close_qfq"]

    df = pd.DataFrame({"trade_date": ["20260608"], "close_qfq": [10.0]})
    data_groups = {"A.SZ": df}
    new_bars_map = {"A.SZ": ["20260608"]}
    dws_cols = [
        "ts_code", "trade_date", "ma_5", "ma_10", "calc_date",
        "input_fingerprint", "spec_version",
    ]

    mod._batch_append_loop(
        FakeCalc(), ["A.SZ"], "20260608", data_groups, new_bars_map,
        lambda c, code, frame, bars: frame,
        dws_cols=dws_cols, float_cols=["ma_5", "ma_10"],
        label_zh="均线日线",
    )
    msgs = [r.getMessage() for r in caplog.records if "calc.batch_compute" in r.getMessage()]
    assert any("均线日线 开始" in m for m in msgs)
    assert any("均线日线 完成" in m for m in msgs)


def test_run_batch_append_skip_refresh_reuses_preflight_fp(monkeypatch):
    """skip_refresh must not recompute state_signature — preflight fp cache is reused."""
    import importlib

    import duckdb
    import numpy as np
    import pandas as pd

    from backend.db.schema import create_all_tables, ensure_calc_state_table
    from backend.etl import calc_router
    from backend.etl.calc_batch_append import run_batch_append_phase
    from backend.etl.calc_fast_skip import batch_load_dde_tails, batch_load_quote_tails
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS, quote_tail_columns
    from backend.etl.calc_router import state_signature
    from backend.etl.calc_state import upsert_calc_state

    monkeypatch.setenv("CALC_APPEND", "1")
    monkeypatch.setenv("CALC_BATCH_APPEND", "1")
    import backend.config as cfg
    importlib.reload(cfg)

    codes = ["BA.SZ", "BB.SZ"]
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    n_daily = 260
    dates = [
        (pd.Timestamp("2020-01-01") + pd.Timedelta(days=i)).strftime("%Y%m%d")
        for i in range(n_daily)
    ]
    for d in dates:
        con.execute(
            "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
            [d],
        )
    rng = np.random.default_rng(42)
    for j, code in enumerate(codes):
        close = 10.0 + np.cumsum(rng.normal(0, 0.2, n_daily))
        rows = []
        for i, d in enumerate(dates):
            c = float(close[i])
            rows.append((code, d, c, c + 0.1, c - 0.1, c, 1000.0 + i, 0))
        con.executemany(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        for d in dates:
            con.execute(
                "INSERT INTO dwd_daily_moneyflow "
                "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, sell_elg_vol, "
                " total_vol, net_mf_amount) VALUES (?, ?, 10, 5, 3, 2, 1000, 1.5)",
                [code, d],
            )
        week_dates = dates[::5][:60]
        for i, d in enumerate(week_dates):
            c = float(close[min(i * 5, len(close) - 1)])
            con.execute(
                "UPDATE dim_date SET is_week_end = 1 WHERE trade_date = ?", [d],
            )
            con.execute(
                "INSERT INTO dwd_weekly_quote "
                "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, "
                "pct_chg, active_days) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 5)",
                [code, d, c, c + 0.1, c - 0.1, c, 2000.0 + i],
            )

    calc_date = dates[-1]
    for code in codes:
        daily = batch_load_quote_tails(con, [code], "daily", quote_tail_columns("daily"))
        weekly = batch_load_quote_tails(con, [code], "weekly", quote_tail_columns("weekly"))
        dde_d = batch_load_dde_tails(con, [code], "daily")
        dde_w = batch_load_dde_tails(con, [code], "weekly")
        last_td = daily[code]["trade_date"].max()
        for indicator_name, freq, CalcCls, sig_cols, source in CALC_ROUTE_SPECS:
            if source == "quote":
                df = daily[code] if freq == "daily" else weekly.get(code)
            else:
                df = dde_d.get(code) if freq == "daily" else dde_w.get(code)
            if df is None or df.empty:
                continue
            fp = state_signature(df, last_td, sig_cols)
            spec_ver = getattr(CalcCls, "SPEC_VERSION", "v1")
            upsert_calc_state(
                con, code, freq, indicator_name,
                last_trade_date=last_td, history_fp=fp, calc_date=calc_date,
                spec_version=spec_ver,
            )

    calls = {"n": 0}
    orig = calc_router.state_signature

    def counted(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(calc_router, "state_signature", counted)

    ctx = run_batch_append_phase(con, codes, calc_date)
    assert ctx is not None
    assert ctx["chunk_codes"] == []
    n_indicators = len(CALC_ROUTE_SPECS)
    assert calls["n"] <= n_indicators * len(codes)
    con.close()


def test_insert_dws_batch_multi_writes_all_stocks_one_insert():
    """Multi-stock narrow write == sum of per-stock insert_dws_batch row counts."""
    import duckdb
    import pandas as pd

    from backend.db.schema import create_all_tables
    from backend.etl.base import insert_dws_batch, insert_dws_batch_multi

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    calc_date = "20260608"
    dws_cols = [
        "ts_code", "trade_date", "ema_12", "ema_26", "dif", "dea", "macd_bar",
        "divergence", "zone", "turning_point", "alert", "trend", "trend_strength",
        "calc_date", "input_fingerprint", "spec_version",
    ]
    float_cols = ["ema_12", "ema_26", "dif", "dea", "macd_bar", "trend_strength"]
    rows = []
    for code in ("A.SZ", "B.SZ"):
        df = pd.DataFrame({
            "trade_date": ["20260608"],
            "ema_12": [1.0], "ema_26": [2.0], "dif": [0.1], "dea": [0.05],
            "macd_bar": [0.05], "divergence": [None], "zone": [None],
            "turning_point": [None], "alert": [None], "trend": ["flat"],
            "trend_strength": [0.01],
        })
        n = insert_dws_batch(
            con, "dws_macd_daily", df, code, calc_date, dws_cols, float_cols,
            input_fingerprint="fp1", write_start="20260608", write_end="20260608",
        )
        assert n == 1
        rows.append((code, df, "fp1", "20260608", "20260608"))

    con.execute("DELETE FROM dws_macd_daily")
    total = insert_dws_batch_multi(
        con, "dws_macd_daily", rows, calc_date, dws_cols, float_cols,
    )
    assert total == 2
    n_db = con.execute(
        "SELECT COUNT(*) FROM dws_macd_daily WHERE calc_date = ?", [calc_date],
    ).fetchone()[0]
    assert n_db == 2
    con.close()


def _make_minimal_preflight_ctx(codes, calc_date="20260611"):
    from backend.etl.calc_preflight_context import CalcPreflightContext

    stock_modes = {
        c: {("macd", "daily"): ("SKIP", [])} for c in codes
    }
    fp_cache = {
        c: {("macd", "daily"): "fp_cached"} for c in codes
    }
    return CalcPreflightContext(
        calc_date=calc_date,
        source="refresh_state",
        stale_codes=list(codes),
        state_map={},
        daily_tails={c: None for c in codes},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes=stock_modes,
        fp_cache_by_stock=fp_cache,
    )


def test_batch_append_hot_path_after_partial_ctx_merge(monkeypatch):
    """ctx covers KEEP; PATCH merged in — cold merge only for PATCH."""
    import duckdb

    from backend.db.schema import create_all_tables, ensure_calc_state_table
    from backend.etl.calc_batch_append import run_batch_append_phase
    from backend.etl.calc_preflight_context import CalcPreflightContext

    quote_calls = []

    def patch_quote_tails(con, codes, freq, columns):
        quote_calls.append((list(codes), freq))
        return {c: None for c in codes}

    preflight_calls = []

    def patch_preflight(ts_code, *args, **kwargs):
        preflight_calls.append(ts_code)
        return {("macd", "daily"): ("SKIP", [])}, {"macd": "fp_patch"}

    monkeypatch.setenv("CALC_APPEND", "1")
    monkeypatch.setenv("CALC_BATCH_APPEND", "1")
    monkeypatch.setenv("CALC_REUSE_REFRESH_CTX", "1")
    import importlib
    import backend.config as cfg
    importlib.reload(cfg)

    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_quote_tails",
        patch_quote_tails,
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_dde_tails",
        lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.preflight_stock_modes_with_fps",
        patch_preflight,
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.load_calc_state_batch",
        lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.upsert_calc_state_batch",
        lambda *a, **k: 0,
    )

    codes = ["KEEP.SZ", "PATCH.SZ"]
    ctx = CalcPreflightContext(
        calc_date="20260611",
        source="refresh_state",
        stale_codes=["KEEP.SZ"],
        state_map={},
        daily_tails={"KEEP.SZ": None},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={"KEEP.SZ": {("macd", "daily"): ("SKIP", [])}},
        fp_cache_by_stock={"KEEP.SZ": {("macd", "daily"): "fp_keep"}},
    )

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    result = run_batch_append_phase(con, codes, "20260611", preflight_ctx=ctx)
    assert result is not None
    assert result["preflight_source"] == "refresh"
    assert all(call[0] == ["PATCH.SZ"] for call in quote_calls)
    assert len(quote_calls) == 2
    assert preflight_calls == ["PATCH.SZ"]
    con.close()


def test_batch_append_reuses_preflight_ctx(monkeypatch):
    """Hot path must not call batch_load_quote_tails when ctx covers all codes."""
    import duckdb

    from backend.db.schema import create_all_tables, ensure_calc_state_table
    from backend.etl.calc_batch_append import run_batch_append_phase

    calls = {"quote_tails": 0, "dde_tails": 0, "preflight": 0}

    def counting_quote_tails(*args, **kwargs):
        calls["quote_tails"] += 1
        return {}

    def counting_dde_tails(*args, **kwargs):
        calls["dde_tails"] += 1
        return {}

    def counting_preflight(*args, **kwargs):
        calls["preflight"] += 1
        return None, {}

    monkeypatch.setenv("CALC_APPEND", "1")
    monkeypatch.setenv("CALC_BATCH_APPEND", "1")
    monkeypatch.setenv("CALC_REUSE_REFRESH_CTX", "1")
    import importlib
    import backend.config as cfg
    importlib.reload(cfg)

    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_quote_tails",
        counting_quote_tails,
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_dde_tails",
        counting_dde_tails,
    )
    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.preflight_stock_modes_with_fps",
        counting_preflight,
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.load_calc_state_batch",
        lambda *a, **k: {},
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.upsert_calc_state_batch",
        lambda *a, **k: 0,
    )

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)
    codes = ["000001.SZ"]
    ctx = _make_minimal_preflight_ctx(codes)
    result = run_batch_append_phase(con, codes, "20260611", preflight_ctx=ctx)
    assert result is not None
    assert result["preflight_source"] == "refresh"
    assert result["tails_load_skipped"] is True
    assert calls["quote_tails"] == 0
    assert calls["dde_tails"] == 0
    assert calls["preflight"] == 0
    con.close()


def test_batch_append_state_uses_batch_upsert(monkeypatch):
    """APPEND state refresh must use upsert_calc_state_batch, not per-stock write."""
    import importlib

    import duckdb
    import pandas as pd

    from backend.db.schema import create_all_tables, ensure_calc_state_table
    from backend.etl.base import CalcResult
    from backend.etl.calc_batch_append import run_batch_append_phase
    from backend.etl.calc_ma import MACalculator

    monkeypatch.setenv("CALC_APPEND", "1")
    monkeypatch.setenv("CALC_BATCH_APPEND", "1")
    monkeypatch.setenv("CALC_REUSE_REFRESH_CTX", "1")
    import backend.config as cfg
    importlib.reload(cfg)

    upsert_calls = {"n": 0, "records": 0}

    def counting_upsert(con, records):
        upsert_calls["n"] += 1
        upsert_calls["records"] += len(records)
        return len(records)

    def forbidden_write(*args, **kwargs):
        raise AssertionError("per-stock write_calc_state_from_df forbidden")

    monkeypatch.setattr(
        "backend.etl.calc_state.upsert_calc_state_batch",
        counting_upsert,
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.write_calc_state_from_df",
        forbidden_write,
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.load_calc_state_batch",
        lambda *a, **k: {},
    )

    ma_spec = (
        "ma", "daily", MACalculator, MACalculator.SIGNATURE_COLS, "quote",
    )
    monkeypatch.setattr(
        "backend.etl.calc_indicators.CALC_ROUTE_SPECS",
        [ma_spec],
    )

    code = "SA.SZ"
    calc_date = "20260611"
    df_out = pd.DataFrame({"trade_date": [calc_date], "close_qfq": [10.0]})

    def fake_ma_batch(*args, **kwargs):
        return CalcResult(calculated=1), [(code, df_out, "fp_test", calc_date, calc_date)]

    monkeypatch.setattr(
        "backend.etl.calc_batch_append.batch_append_ma",
        fake_ma_batch,
    )

    ctx_preflight = _make_minimal_preflight_ctx([code], calc_date=calc_date)
    ctx_preflight.stock_modes = {
        code: {("ma", "daily"): ("APPEND", [calc_date])},
    }
    ctx_preflight.fp_cache_by_stock = {
        code: {("ma", "daily"): "fp_cached"},
    }
    ctx_preflight.daily_tails = {code: df_out}

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    ensure_calc_state_table(con)

    result = run_batch_append_phase(con, [code], calc_date, preflight_ctx=ctx_preflight)
    assert result is not None
    assert upsert_calls["n"] >= 1
    assert upsert_calls["records"] >= 1
    con.close()


def test_batch_append_macd_passes_spec_version(monkeypatch):
    """batch_append_macd insert_dws_batch_multi must pass MACDCalculator.SPEC_VERSION."""
    import pandas as pd

    from backend.etl.calc_batch_append import batch_append_macd
    from backend.etl.calc_macd import MACDCalculator

    captured = {}

    def spy_multi(*args, **kwargs):
        captured["spec_version"] = kwargs.get("spec_version")
        return 1

    monkeypatch.setattr(
        "backend.etl.calc_batch_append.load_ema_seeds_batch",
        lambda *a, **k: {c: {"ema_12": 1.0, "ema_26": 2.0, "dea": 0.1} for c in a[2]},
    )
    monkeypatch.setattr("backend.etl.calc_batch_append.insert_dws_batch_multi", spy_multi)
    monkeypatch.setattr("backend.config.CALC_VECTOR_APPEND", False)

    codes = ["T.SZ"]
    df = pd.DataFrame({"trade_date": ["20260607", "20260608"], "close_qfq": [10.0, 10.1]})
    batch_append_macd(
        None, "daily", codes, "20260608",
        {c: df for c in codes}, {c: ["20260608"] for c in codes},
    )
    assert captured.get("spec_version") == MACDCalculator.SPEC_VERSION


def test_batch_append_volume_passes_spec_version(monkeypatch):
    """batch_append_volume insert_dws_batch_multi must pass VolumeCalculator.SPEC_VERSION."""
    import pandas as pd

    from backend.etl.calc_batch_append import batch_append_volume
    from backend.etl.calc_volume import VolumeCalculator

    captured = {}

    def spy_multi(*args, **kwargs):
        captured["spec_version"] = kwargs.get("spec_version")
        return 1

    monkeypatch.setattr(
        "backend.etl.calc_batch_append.load_zone_seeds_batch",
        lambda *a, **k: {c: {"zone": "normal"} for c in a[2]},
    )
    monkeypatch.setattr("backend.etl.calc_batch_append.insert_dws_batch_multi", spy_multi)
    monkeypatch.setattr("backend.config.CALC_VECTOR_APPEND", False)

    codes = ["T.SZ"]
    df = pd.DataFrame({
        "trade_date": ["20260607", "20260608"],
        "close_qfq": [10.0, 10.1],
        "vol": [100.0, 110.0],
    })
    batch_append_volume(
        None, "daily", codes, "20260608",
        {c: df for c in codes}, {c: ["20260608"] for c in codes},
    )
    assert captured.get("spec_version") == VolumeCalculator.SPEC_VERSION


def test_batch_append_dde_passes_spec_version(monkeypatch):
    """batch_append_dde insert_dws_batch_multi must pass DDECalculator.SPEC_VERSION."""
    import pandas as pd

    from backend.etl.calc_batch_append import batch_append_dde
    from backend.etl.calc_dde import DDECalculator

    captured = {}

    def spy_multi(*args, **kwargs):
        captured["spec_version"] = kwargs.get("spec_version")
        return 1

    monkeypatch.setattr(
        "backend.etl.calc_batch_append.load_ema_seeds_batch",
        lambda *a, **k: {c: {"ddx2": 0.01} for c in a[2]},
    )
    monkeypatch.setattr("backend.etl.calc_batch_append.insert_dws_batch_multi", spy_multi)
    monkeypatch.setattr("backend.config.CALC_VECTOR_APPEND", False)

    codes = ["T.SZ"]
    dde_df = pd.DataFrame({
        "trade_date": ["20260607", "20260608"],
        "buy_lg_vol": [1.0, 1.0],
        "sell_lg_vol": [1.0, 1.0],
        "buy_elg_vol": [1.0, 1.0],
        "sell_elg_vol": [1.0, 1.0],
        "total_vol": [100.0, 100.0],
        "net_mf_amount": [0.0, 0.0],
        "close_qfq": [10.0, 10.1],
    })
    batch_append_dde(
        None, "daily", codes, "20260608",
        {c: dde_df for c in codes}, {c: ["20260608"] for c in codes},
    )
    assert captured.get("spec_version") == DDECalculator.SPEC_VERSION
