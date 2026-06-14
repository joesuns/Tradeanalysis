# 周线数据修复 Runbook（date_trunc 周划分变更）

**日期：** 2026-06-05
**范围：** 代码修复已落地 + 实库一次性运维步骤
**关联：**

- `2026-06-05-kpattern-weekly-is-week-end-fix.md`（kpattern weekly 采样）
- `2026-06-05-dws-snapshot-retention.md`（prune 快照清理）

---

## 1. 背景：两个同源缺陷

### 1.1 kpattern 周线未过滤 is_week_end

`calc_kpattern.py` weekly 路径漏了 `is_week_end=1` 过滤，在滚动周线的每根 intra-week
bar 上做形态识别 → `dws_kpattern_weekly` 膨胀（1.37M 行 vs 其他周线表 285k）且取值错误。
**已修复**：weekly 分支对齐其余 5 个计算器，`JOIN dim_date WHERE dd.is_week_end=1`。

### 1.2 `%Y-%W` 跨年周切分错误

`dim_date.is_week_end`（build_dim）与 `dwd_weekly_quote` 滚动周线分区（build_dwd）原用
`strftime(dt,'%Y-%W')`。`%Y` 为日历年、年初不满一周记第 `00` 周 → 跨年自然周被切成两段。

实证（2025-12-29 周一 ~ 2026-01-02 周五，同一自然周）：


| 日期       | `%Y-%W`(旧)  | `date_trunc('week')`(新) |
| -------- | ----------- | ----------------------- |
| 12-29 周一 | 2025-52     | 2025-12-29              |
| 12-31 周三 | 2025-52     | 2025-12-29              |
| 01-01 周四 | **2026-00** | 2025-12-29              |
| 01-02 周五 | **2026-00** | 2025-12-29              |


后果：跨年周 bar 的 OHLC/pct_chg/active_days 错误；该周出现两个 `is_week_end=1`
（上一年尾 + 新年头），6 个周线指标在年初多算一根假周末 bar。每年元旦附近发生一次。
**已修复**：两处统一改为 `date_trunc('week', dt)`（周一锚点）。spec §12.56。

---

## 2. 代码改动（已完成，全量 246 passed）


| 文件                             | 改动                                                                                                 |
| ------------------------------ | -------------------------------------------------------------------------------------------------- |
| `backend/etl/calc_kpattern.py` | weekly 查询补 `is_week_end=1`                                                                         |
| `backend/etl/build_dim.py`     | `is_week_end` 判定改 `date_trunc('week')`                                                             |
| `backend/etl/build_dwd.py`     | 周线分区改 `date_trunc('week')`                                                                         |
| `backend/etl/repair_weekly.py` | 新增 `repair_weekly(con, dry_run=True)`                                                              |
| `backend/cli.py`               | 新增 `repair-weekly` 子命令（默认 dry-run）                                                                 |
| tests                          | `test_calc_kpattern_weekly` / `test_build_dim`(跨年周) / `test_build_dwd`(跨年周) / `test_repair_weekly` |


### `repair_weekly(con, dry_run=True)` 行为

- **dry-run（默认，只读）**：返回 `wrongly_marked`（错标周末）、`newly_marked`（漏标周末）、
各周线表 `orphans` 行数（`trade_date` 不在正确周末集合）。正确周末集合用 `date_trunc('week')`
独立计算，不依赖已存的 `is_week_end`。
- `**dry_run=False`**：`build_dim_date` → `build_dwd_weekly_quote` → 删 6 个周线表孤儿行
→ `CHECKPOINT`，返回 `deleted` 明细。**不**自动重算 DWS（重操作，交由 `calc`）。

---

## 3. 实库执行顺序（运维手动，按需）

> 前置：确保无其他进程在写库；建议先备份 `data/tradeanalysis.duckdb`。

```bash
# 1) 预览影响面（只读，不写库）
python3 -m backend.cli repair-weekly

# 2) 应用结构修复 + 删孤儿行
python3 -m backend.cli repair-weekly --execute

# 3) 刷新跨年周的过期周线指标值
#    （指纹自动跳过未变周，仅重算输入变化的跨年周）
python3 -m backend.cli calc

# 4) 可选：回收快照空间
python3 -m backend.cli prune --keep 1
```

### 为什么第 3 步必需

删孤儿只清除污染行；**残留的真周末行**仍是用旧（错误分区的）`dwd_weekly` 算出的过期值。
重建 `dwd_weekly` 后这些跨年周的输入指纹改变，`calc` 会重算并写新 `calc_date`，
`v_*_latest` 随之取到正确值。非跨年周指纹不变 → 跳过，开销小。

### 正确性保证

- 孤儿删除条件 `trade_date NOT IN (is_week_end=1 集合)`，与 build_dim 的 `date_trunc('week')`
口径一致。
- 不删真周末行；过期值由第 3 步 `calc` 以新快照覆盖（`v_*_latest` 取 MAX(calc_date)）。

---

## 4. 不在本批

- 不在 `run_calc` / `calc` 自动挂接 `repair-weekly`（避免隐式重建）。
- `dim_date.week_of_year` 展示字段保留 `%W`（无下游计算依赖）。
- 不做破坏性全库重写压缩（由运维 `prune` + 时间窗自然回收，或单独决策）。

