"""周线数据修复运维工具。

修复 `%Y-%W` → `date_trunc('week')` 周划分变更后，纠正实库历史数据：
- `dim_date.is_week_end` 在跨年周多标了假周末；
- 所有周线 DWS 表残留 `trade_date` 不再是真周末的孤儿行（kpattern intra-week
  膨胀 + 跨年周假周末），会污染 `v_*_latest` 视图。

默认 dry-run 只预览、不写库；`dry_run=False` 才重建 dim_date + dwd_weekly_quote
并删除孤儿行。重建后须另跑 `calc` 刷新真周末行的过期取值（指纹会自动跳过未变周，
仅重算跨年周）。
"""
import logging

from backend.db.connection import run_checkpoint
from backend.etl.build_dim import build_dim_date
from backend.etl.build_dwd import build_dwd_weekly_quote

logger = logging.getLogger(__name__)

WEEKLY_DWS_TABLES = [
    f"dws_{indicator}_weekly"
    for indicator in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]
]

# 正确周末集合：按 date_trunc('week') 分组取每周最后一个交易日（独立于已存 is_week_end）
_CORRECT_WEEK_ENDS_SQL = """
    SELECT MAX(trade_date) AS trade_date
    FROM dim_date
    WHERE is_trade_day = 1
    GROUP BY date_trunc('week', CAST(
        substr(trade_date,1,4)||'-'||substr(trade_date,5,2)||'-'||substr(trade_date,7,2)
        AS DATE))
"""


def repair_weekly(con, dry_run: bool = True) -> dict:
    """预览或执行周线历史数据修复。

    Args:
        dry_run: True 仅返回预览统计，不写库；False 执行重建 + 删孤儿。

    Returns:
        {
          "executed": bool,
          "wrongly_marked": [...],   # 当前被错误标记为周末、实际不是的交易日
          "newly_marked": [...],     # 当前漏标、实际应为周末的交易日
          "orphans": {table: count}, # 各周线 DWS 表非真周末的孤儿行数
          "deleted": {table: count}, # 仅 executed=True 时存在
        }
    """
    correct_we = {
        r[0] for r in con.execute(_CORRECT_WEEK_ENDS_SQL).fetchall()
    }
    current_we = {
        r[0] for r in con.execute(
            "SELECT trade_date FROM dim_date WHERE is_week_end = 1"
        ).fetchall()
    }

    orphans = {}
    for tbl in WEEKLY_DWS_TABLES:
        orphans[tbl] = con.execute(
            f"SELECT COUNT(*) FROM {tbl} "
            f"WHERE trade_date NOT IN ({_CORRECT_WEEK_ENDS_SQL})"
        ).fetchone()[0]

    result = {
        "executed": False,
        "wrongly_marked": sorted(current_we - correct_we),
        "newly_marked": sorted(correct_we - current_we),
        "orphans": orphans,
    }

    if dry_run:
        return result

    if not correct_we:
        logger.warning("repair_weekly: no trading days in dim_date, aborting execute")
        return result

    build_dim_date(con)
    build_dwd_weekly_quote(con)

    deleted = {}
    for tbl in WEEKLY_DWS_TABLES:
        before = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        con.execute(
            f"DELETE FROM {tbl} WHERE trade_date NOT IN "
            f"(SELECT trade_date FROM dim_date WHERE is_week_end = 1)"
        )
        after = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        deleted[tbl] = before - after

    from backend.etl.calc_state import invalidate_weekly_calc_state
    n_state = invalidate_weekly_calc_state(con)

    run_checkpoint(con)
    result["executed"] = True
    result["deleted"] = deleted
    result["weekly_state_invalidated"] = n_state
    logger.info(
        "repair_weekly executed: deleted %d orphan rows across %d weekly tables; "
        "invalidated %d weekly calc_state rows",
        sum(deleted.values()), len(WEEKLY_DWS_TABLES), n_state,
    )
    return result
