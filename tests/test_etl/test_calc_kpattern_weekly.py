"""KPattern 周线只应在真周末 bar（dim_date.is_week_end=1）上计算。

dwd_weekly_quote 是滚动周线（每交易日一行），周线指标必须只采样
is_week_end=1 的真周末 bar，与 MACD/MA/DDE/Volume/PricePosition 一致。
"""
from datetime import date, timedelta

from backend.db.schema import create_all_tables
from backend.etl.calc_kpattern import KPatternCalculator


def _seed_rolling_weekly(con, ts_code="TEST.SZ", weeks=35, bars_per_week=3):
    """每周写 bars_per_week 根滚动 bar，仅最后一根标记 is_week_end=1。"""
    create_all_tables(con)
    d = date(2025, 1, 6)  # 周一
    week_end_dates = []
    for _ in range(weeks):
        for b in range(bars_per_week):
            td = d.strftime("%Y%m%d")
            con.execute(
                "INSERT INTO dwd_weekly_quote "
                "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, pct_chg) "
                "VALUES (?, ?, 10.0, 10.5, 9.5, 10.0, 1000000, 0.0)",
                (ts_code, td),
            )
            is_we = 1 if b == bars_per_week - 1 else 0
            con.execute(
                "INSERT INTO dim_date (trade_date, is_week_end, is_trade_day) VALUES (?, ?, 1)",
                (td, is_we),
            )
            if is_we:
                week_end_dates.append(td)
            d += timedelta(days=1)
        d += timedelta(days=7 - bars_per_week)  # 跳到下周一
    return ts_code, week_end_dates


def test_weekly_kpattern_only_samples_week_end_bars(temp_db):
    ts_code, week_end_dates = _seed_rolling_weekly(temp_db)

    calc = KPatternCalculator(temp_db, freq="weekly")
    result = calc.calculate([ts_code], calc_date="20250630")
    assert result.calculated == 1

    written = sorted(
        r[0] for r in temp_db.execute(
            "SELECT trade_date FROM dws_kpattern_weekly WHERE ts_code = ?", (ts_code,)
        ).fetchall()
    )
    # 只写真周末 bar，无 intra-week 滚动 bar
    assert written == sorted(week_end_dates)
    assert len(written) == 35
