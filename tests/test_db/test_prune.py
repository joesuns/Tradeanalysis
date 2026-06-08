"""DWS 快照保留/清理测试。

不变性铁律：清理绝不能删除任一 (ts_code, trade_date) 的 MAX(calc_date) 行，
因此 v_*_latest 视图结果逐行不变。
"""
from backend.db.schema import create_all_tables
from backend.db.connection import prune_dws_snapshots


def _insert_macd(con, ts_code, trade_date, calc_date, dif):
    con.execute(
        "INSERT INTO dws_macd_daily "
        "(ts_code, trade_date, dif, trend, calc_date) "
        "VALUES (?, ?, ?, 'flat', ?)",
        (ts_code, trade_date, dif, calc_date),
    )


def test_prune_collapses_superseded_but_keeps_latest_per_key(temp_db):
    """keep_runs=1：删除被覆盖的旧快照，但保留每个键的最新值。

    覆盖指纹跳过交错：B 只在旧 calc_date 计算（其最新值在旧日期），不得删除。
    """
    create_all_tables(temp_db)
    # A: 0604 被 0605 覆盖
    _insert_macd(temp_db, "A.SZ", "20260101", "20260604", 1.0)
    _insert_macd(temp_db, "A.SZ", "20260101", "20260605", 2.0)
    # B: 仅在 0604 计算（指纹跳过 0605），其最新值=旧日期
    _insert_macd(temp_db, "B.SZ", "20260101", "20260604", 9.0)

    # latest 视图基线
    before = dict(temp_db.execute(
        "SELECT ts_code, dif FROM v_dws_macd_daily_latest"
    ).fetchall())

    deleted = prune_dws_snapshots(temp_db, keep_runs=1)

    # 只删除了 A 的旧 superseded 行
    assert deleted["dws_macd_daily"] == 1
    total = temp_db.execute("SELECT COUNT(*) FROM dws_macd_daily").fetchone()[0]
    assert total == 2

    # latest 视图逐行不变
    after = dict(temp_db.execute(
        "SELECT ts_code, dif FROM v_dws_macd_daily_latest"
    ).fetchall())
    assert after == before == {"A.SZ": 2.0, "B.SZ": 9.0}


def test_prune_keep_runs_retains_intermediate_snapshot(temp_db):
    """keep_runs=2：保留最近 2 次运行的快照 + 所有键最新值。"""
    create_all_tables(temp_db)
    _insert_macd(temp_db, "A.SZ", "20260101", "20260603", 1.0)
    _insert_macd(temp_db, "A.SZ", "20260101", "20260604", 2.0)
    _insert_macd(temp_db, "A.SZ", "20260101", "20260605", 3.0)

    deleted = prune_dws_snapshots(temp_db, keep_runs=2)

    # 最近 2 次 = {0604, 0605}，0603 被删（且非键最新值）
    assert deleted["dws_macd_daily"] == 1
    remaining = sorted(
        r[0] for r in temp_db.execute(
            "SELECT calc_date FROM dws_macd_daily WHERE ts_code='A.SZ'"
        ).fetchall()
    )
    assert remaining == ["20260604", "20260605"]


def test_prune_empty_table_returns_zero(temp_db):
    """空表不报错，返回 0。"""
    create_all_tables(temp_db)
    deleted = prune_dws_snapshots(temp_db, keep_runs=5)
    assert deleted["dws_macd_daily"] == 0
    assert all(v == 0 for v in deleted.values())
