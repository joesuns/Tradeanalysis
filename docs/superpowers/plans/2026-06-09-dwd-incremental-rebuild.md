# DWD 增量 Rebuild + 停牌填充降噪

**日期：** 2026-06-09  
**状态：** 已落地（2026-06-09）  
**触发：** run `6916aea6` — DWD 57min（停牌填充 49min）+ calc batch APPEND 未命中

## 目标

| 场景 | 基线 (6916aea6) | 目标 |
|------|-----------------|------|
| 新日 `run` DWD | ~57 min | **<10 min** |
| 新日 calc APPEND 命中 | batch_only=74 | **>5000** |
| 端到端 `run` | ~100 min | **<25 min** |

## 根因

1. `n_fetch>0` → `rebuild_all_dwd(全市场)`，每股 DELETE+全历史重插
2. 停牌填充 gap 检测用 **ODS vs 日历** → 已填充股每日仍进 fill_list（3176 股 × LATERAL）
3. DWD 历史被全量重建 → `history_fp` 失效 → calc 98.6% FULL chunk

## 方案

### P0 停牌填充（DWD gap 检测）

- gap 改为 **DWD 行数 vs 日历**（step1 之后）
- 已填充股不再重复 LATERAL

### P1 run 路径增量 rebuild（`DWD_INCREMENTAL=1` 默认）

- `n_fetch>0` → `find_stale_dwd_codes` 子集，非全市场
- `rebuild_dwd_incremental(con, stale, trade_date)`：
  - 除权/adj 变或无 DWD 历史 → 全量 `build_dwd_daily_quote`
  - 其余 → 仅 INSERT `trade_date` 当日行（无 DELETE）
  - moneyflow 同日增量 INSERT
  - weekly 仍对子集全量重建（范围已缩小）

### 验收

- `pytest tests/test_etl/test_build_dwd.py tests/test_cli.py -v`
- 新日 run 日志：`dwd.suspension_fill` 秒级或跳过；`batch_append` 有 `append=N`

## 范围外

- 周线 rolling 按周增量 SQL
- 物化 tail_245 表
