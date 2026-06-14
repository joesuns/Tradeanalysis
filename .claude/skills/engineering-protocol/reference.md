# Engineering Protocol — Reference

本文件补充 SKILL.md 中的领域细节。**数值与命令以 CLAUDE.md 为准**；此处仅作 agent 快速索引。

## 日常默认路径（效率）

| 层 | 机制 | 说明 |
|----|------|------|
| DWD | `DWD_INCREMENTAL=1` | `rebuild_dwd_for_stale(stale_codes)`；tail INSERT / 周分区增量 |
| Calc | `CALC_APPEND=1` | SKIP / APPEND / FULL 路由；`dws_calc_state` + 强签名 |
| 同日复跑 | `CALC_FAST_SKIP=1` | chunk preflight 全 SKIP 短路 |
| Batch | `CALC_BATCH_APPEND` / `CALC_BATCH_FULL` | 跨股批处理 APPEND 与 mass FULL |
| 幂等 | calc idempotent skip | 同 calc_date 已成功且无 stale ODS → 秒退 |

## 全量 / rebuild 允许的根因

- 首次建库
- `repair-weekly --execute` 等运维修复
- 除权/adj 变更且未走 `refresh_qfq_prices` 增量路径
- 用户明确要求，且方案已说明 incremental 不足

**禁止：** 日常 `rebuild_all_dwd(con, 全市场)`；无 ts_codes 的全库 rebuild。

## 数据质量不可省略

- G1 warmup（`dwd_rows≥250`）、G2 stale ODS、G3 stale DWD
- OHLC 校验、`adj_factor` 护栏
- `state_signature` / `input_fingerprint` 误判防护（宁可多一次 FULL，不可误 APPEND）
- append vs FULL 等价性（`tests/test_etl/test_append_calc.py`，`atol=1e-9`）
- 算法升级：`spec_version` + 必要时 golden / B4 gate

## 性能 SLA（观测，非硬编码在 skill）

真新日全链路目标见 `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md` 与 `scripts/benchmark_run.py`。skill 不写具体秒数，避免与 plan 漂移。

## Calculator 变更审计清单

修改任意 Calculator 时额外确认：
- `backend/etl/orchestrator.py` CALCULATORS 注册
- `backend/db/schema.py` 对应 DWS 表 DDL、索引
- `v_dws_*_latest` 视图
- `backend/export_wide.py` 列映射
- `RECALC_SPEC` / `SIGNATURE_COLS` / `SPEC_VERSION` 是否需 bump
