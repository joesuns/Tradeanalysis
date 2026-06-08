"""周线修复运维命令测试。

修复 dim_date.is_week_end 后，所有周线 DWS 表里 trade_date 不再是真周末的行
都成了污染 v_*_latest 的孤儿行（kpattern intra-week 膨胀 + 跨年周假周末）。
repair_weekly 默认 dry-run 只预览，--execute 才重建 dim/dwd + 删孤儿。
"""
from backend.db.schema import create_all_tables
from backend.etl.repair_weekly import repair_weekly


def _seed_cross_year_week(con):
    """跨年自然周 12-29~01-02 全交易，build_dim_date 后手动注入旧 bug 状态。"""
    create_all_tables(con)
    con.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES "
        "('20251229',1),('20251230',1),('20251231',1),('20260101',1),('20260102',1)"
    )
    from backend.etl.build_dim import build_dim_date
    build_dim_date(con)
    # 模拟旧 %Y-%W bug：把 12-31 也错误标成周末
    con.execute("UPDATE dim_date SET is_week_end = 1 WHERE trade_date = '20251231'")
    # 注入 dws 孤儿行：12-31(假周末) + 01-01(intra-week) 是孤儿，01-02 才是真周末
    for td in ("20251231", "20260101", "20260102"):
        con.execute(
            "INSERT INTO dws_kpattern_weekly (ts_code, trade_date, strength, calc_date) "
            "VALUES ('A.SZ', ?, 0.5, '20260102')",
            (td,),
        )


def test_repair_weekly_dry_run_previews_without_writes(temp_db):
    _seed_cross_year_week(temp_db)

    res = repair_weekly(temp_db, dry_run=True)

    assert res["executed"] is False
    assert res["wrongly_marked"] == ["20251231"]
    assert res["newly_marked"] == []
    # 孤儿 = 12-31 + 01-01（非真周末）
    assert res["orphans"]["dws_kpattern_weekly"] == 2

    # dry-run 绝不写库：is_week_end 与 dws 行数都不变
    still_marked = temp_db.execute(
        "SELECT COUNT(*) FROM dim_date WHERE trade_date='20251231' AND is_week_end=1"
    ).fetchone()[0]
    assert still_marked == 1
    assert temp_db.execute(
        "SELECT COUNT(*) FROM dws_kpattern_weekly"
    ).fetchone()[0] == 3


def test_repair_weekly_execute_fixes_and_deletes_orphans(temp_db):
    _seed_cross_year_week(temp_db)
    # dwd_daily_quote 供 build_dwd_weekly_quote 重建
    for td in ("20251229", "20251230", "20251231", "20260101", "20260102"):
        temp_db.execute(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, "
            " vol, amount, pct_chg, is_suspended) "
            "VALUES ('A.SZ', ?, 10.0, 11.0, 9.0, 10.5, 100, 1000, 0.0, 0)",
            (td,),
        )

    res = repair_weekly(temp_db, dry_run=False)

    assert res["executed"] is True
    assert res["deleted"]["dws_kpattern_weekly"] == 2

    # is_week_end 修正：跨年周只剩 01-02 一个周末
    week_ends = [
        r[0] for r in temp_db.execute(
            "SELECT trade_date FROM dim_date WHERE is_week_end=1 ORDER BY trade_date"
        ).fetchall()
    ]
    assert week_ends == ["20260102"]

    # dws 孤儿已删，仅留真周末行
    remaining = [
        r[0] for r in temp_db.execute(
            "SELECT trade_date FROM dws_kpattern_weekly ORDER BY trade_date"
        ).fetchall()
    ]
    assert remaining == ["20260102"]

    # dwd_weekly 已重建：跨年周聚合进同一 bar
    bar = temp_db.execute(
        "SELECT active_days FROM dwd_weekly_quote "
        "WHERE ts_code='A.SZ' AND trade_date='20260102'"
    ).fetchone()
    assert bar is not None and bar[0] == 5
