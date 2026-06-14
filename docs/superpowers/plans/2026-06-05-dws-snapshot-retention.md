# DWS 快照保留/清理

**日期：** 2026-06-05
**范围：** 新增 `prune_dws_snapshots()` + CLI `prune` 子命令；更新 spec 12.23
**默认：** `keep_runs=5`；逻辑删除 + CHECKPOINT（不自动全库重写）

## 根因
DWS INSERT-only 快照无清理，每次全市场 calc ≈ +500 万行永久累积（实测 12 表 10.96M 行 / 3.1G）。

## 设计约束（已审计确认）
- 消费方（导出/API/backtest）**只读 `v_*_latest`**，无人读旧 calc_date。
- 指纹跳过 → 快照交错：某 `(ts_code,trade_date)` 最新值可能在旧 calc_date。
- **不变性铁律：** 清理绝不能删除任一 `(ts_code,trade_date)` 的 `MAX(calc_date)` 行 → latest 视图逐行不变。

## 实现

### `backend/db/connection.py` 新增
```python
DWS_TABLES = [f"dws_{i}_{f}"
              for i in ["kpattern","macd","ma","dde","volume","price_position"]
              for f in ["daily","weekly"]]

def prune_dws_snapshots(con, keep_runs: int = 5) -> dict:
    """删除 superseded 旧快照，保留最近 keep_runs 次运行 + 所有键最新值。
    返回 {table: deleted_rows}。"""
    result = {}
    for tbl in DWS_TABLES:
        cutoff_row = con.execute(
            f"SELECT calc_date FROM (SELECT DISTINCT calc_date FROM {tbl} "
            f"ORDER BY calc_date DESC LIMIT ?) ORDER BY calc_date ASC LIMIT 1",
            (keep_runs,)).fetchone()
        if not cutoff_row:
            result[tbl] = 0
            continue
        cutoff = cutoff_row[0]
        deleted = con.execute(f"""
            DELETE FROM {tbl}
            WHERE calc_date < ?
              AND (ts_code, trade_date, calc_date) NOT IN (
                  SELECT ts_code, trade_date, MAX(calc_date)
                  FROM {tbl} GROUP BY ts_code, trade_date)
        """, (cutoff,)).fetchone()  # DuckDB DELETE returns affected count
        result[tbl] = deleted[0] if deleted else 0
    return result
```
- `keep_runs=1` → 纯 latest 坍缩；`>1` → 保留最近 N 次 + 键最新值。
- 永不删 max-per-key → 正确性保证。

### `backend/cli.py` 新增 `prune` 子命令
- `python -m backend.cli prune [--keep N] [--db-path ...]`
- 调用 `prune_dws_snapshots(con, keep_runs=args.keep)` → `run_checkpoint(con)` → 打印每表删除行数 + 总计。
- **不**挂进 `run_calc`（避免隐式删除）。

## 测试（TDD）
`tests/test_db/test_prune.py`（新建）
1. **坍缩正确性 + 指纹交错**（核心）：插入 A/0101 两个 calc_date（旧被新覆盖）+ B/0101 仅旧 calc_date（模拟指纹跳过，其最新=旧）。`keep_runs=1` 后：A 仅留新行、B 旧行保留；`v_dws_macd_daily_latest` 结果不变。
2. **keep_runs=2 保留中间快照**：3 个 calc_date，keep_runs=2 保留最近 2 个 + 键最新值。
3. **空表**：返回 0，不报错。

`pytest tests/ -v` 全绿。

## 文档
- spec 12.23：policy 由"旧 calc_date 永不清除"→"窗口保留（默认 keep_runs=5），清理只删 superseded 行，latest 不变"。
- `CLAUDE.md`：常用命令加 `prune`；注意事项加快照保留策略。

## 不在本批
- 不自动全库重写压缩磁盘（3.1G 实库由用户手动跑 `prune` 或后续决定）。
- 不在 `run_calc` 自动清理。
