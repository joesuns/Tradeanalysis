# Calc Spec Version 治理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **前置依赖：** [2026-06-14-ma-alignment-fallback-fix.md](./2026-06-14-ma-alignment-fallback-fix.md)（算法 v2 已合入；**存量 DWS 刷新未完成**）

**Goal:** 建立「算法 spec 升级 → DWS 窄窗 FULL → latest 截面一致 → export 可信」的端到端数据契约，修复 MA v2 发布后全市场仍 v1 的架构缺口，并防止同类事故复发。

**Architecture:** 不新增 DWS 基表列；不改动 `v_*_latest` 的 MAX(calc_date) 语义。在 **calc 路由层** 统一 SKIP 不变量（fp + spec + new_bar）；在 **ADS/质检层** 增加只读 DQ 视图与 health_check 门禁；在 **运维层** 增加 spec 发布 SOP 与可选 `--refresh-spec` 窄指标刷新。

**Tech Stack:** Python 3.9+, DuckDB, pytest, scripts/health_check.py

---

## ① 问题全景（数据架构）

### 1.1 事故链

```
代码 bump MACalculator.SPEC_VERSION v1→v2
  → 日常 run/calc 输入指纹未变
  → L1 _should_skip_calc_idempotent(force) 整段秒退
  → L2 try_force_same_day_batch_shortcut → _modes_from_state_only 全 SKIP
  → L3 classify_calc_mode 的 spec mismatch→FULL 从未执行
  → dws_ma_* / dws_calc_state 仍 v1
  → v_dws_ma_daily_latest / Excel 99.98% 旧语义
```

**第二实例（DDE trend，2026-06-14）：** `DDECalculator.SPEC_VERSION=v2` 已打标，但 `trend` 列仍为旧语义或未正确 FULL → 须 `cli repair-dde-trend` + content oracle（plan `2026-06-14-dde-trend-repair.md`）。`--refresh-spec dde` **无效**（state 已是 v2）。

### 1.2 设计已有、未贯通的能力

| 能力 | 位置 | 状态 |
|------|------|------|
| `expected_spec_version` → FULL | `calc_router.py:41-44` | ✓ 已实现 |
| `check_dwd_unchanged(..., expected_spec_version)` | `base.py:544-583` | ✓ 已实现 |
| fast-skip preflight 传 spec | `calc_fast_skip.py:156-158` | ✓ 已实现 |
| batch_full `check_spec=True` | macd/ma/dde | ✓ |
| batch_full `check_spec=True` | volume | ✗ 有 spec_version 无 check_spec |
| per-stock `calculate()` spec 门禁 | ma/macd/dde | ✓ |
| per-stock `calculate()` spec 门禁 | volume | ✗ 预取 latest_specs 未传入 check |
| force 短路 spec 感知 | orchestrator + batch_append | ✗ |
| DQ 视图 / health_check | — | ✗ |
| runbook spec 发布 SOP | daily-runbook | ✗ |

### 1.3 全项目 SPEC_VERSION 清单（2026-06-14 核实）

| Calculator | SPEC_VERSION | calculate check_spec | batch_full check_spec |
|------------|--------------|----------------------|------------------------|
| MACDCalculator | v3 | ✓ | ✓ |
| MACalculator | v2 | ✓ | ✓ |
| DDECalculator | v2 | ✓ | ✓ |
| VolumeCalculator | v2 | ✗ | ✗ |
| KPatternCalculator | （无，默认 v1） | ✗ | ✗ |
| PricePositionCalculator | （无，默认 v1） | ✗ | ✗ |

kpattern / priceposition 当前无 bump 需求；Volume v2 已 bump 但门禁未接齐——**同类风险**。

### 1.4 SKIP 不变量（目标契约，写入 spec）

```
允许 SKIP 当且仅当同时满足：
  ① input_fingerprint 同（DWD 域未变）
  ② spec_version 同（stored == Calculator.SPEC_VERSION）
  ③ 无新 trade_date bar（last_trade_date 已覆盖）
```

**反模式（禁止）：** 仅凭 `dws_calc_state.updated_calc_date == calc_date` 或 ODS 未变即全 SKIP。

---

## ② 方案总览（四轨并行）

| 轨道 | 名称 | 类型 | 优先级 |
|------|------|------|--------|
| **Track 0** | MA v2 存量刷新 + 验收 | 一次性运维 | P0（阻塞 export 可信） |
| **Track 1** | 路由层 spec 感知 | 代码 | P0 |
| **Track 2** | 数据质量观测 | 视图 + health_check | P1 |
| **Track 3** | 运维 SOP + CLI | 文档 + 可选 CLI | P1 |
| **Track 4** | run export 软门禁 | 代码（可选） | P2 |

**DWD/calc 决策树对齐：**

- spec 升级 → **按指标窄窗 FULL**（`batch_full_*` / `calculate` recalc_start 窗）
- **禁止** rebuild_all_dwd / 无 ts_codes 全库 DWD
- 日常 run 仍走 APPEND/SKIP；仅 spec stale 子集 FULL

---

## ③ Track 0 — MA v2 存量刷新（运维，先于 Gate 3）

**根因：** spec v1→v2，DWD 未变；incremental 路径应窄窗 FULL，但被 L1/L2 旁路。

**命令（全市场，写入 calc_date=20260612 新快照）：**

```bash
CALC_FORCE_HARD=1 python3 -m backend.cli calc --date 20260612 --force
python3 -m scripts.audit_ma_alignment_fallback
python3 -m backend.cli export --date 20260612
```

**验收 SQL：**

```sql
-- spec 覆盖率
SELECT spec_version, COUNT(*) FROM v_dws_ma_daily_latest
WHERE trade_date='20260612' GROUP BY 1;
-- 期望：v2 ≈ 活跃股数

-- 301205 锚点
SELECT alignment, turning_point, spec_version
FROM v_dws_ma_daily_latest
WHERE ts_code='301205.SZ' AND trade_date='20260612';
-- 期望：bear_building, dead_cross, v2
```

**Gate 3（与 ma-alignment plan 对齐）：**

- [ ] `audit_ma_alignment_fallback` 前两项 = 0
- [ ] v2 覆盖率 ≥ 99%（成熟股）
- [ ] export 在 calc **之后**执行

**注意：** DuckDB 单写；与 `run` 并行时须等 lock 释放。

---

## ④ Track 1 — 路由层 spec 感知（P0 代码）

### Task 1.1 — 中央 spec 注册表

**文件：** `backend/etl/calc_indicators.py`

- [x] 新增 `INDICATOR_SPEC_VERSIONS: Dict[Tuple[str,str], str]` 或由 `CALC_ROUTE_SPECS` 派生：
  `{(indicator, freq): getattr(CalcCls, "SPEC_VERSION", "v1")}`
- [x] 供 orchestrator、batch_append、gate 共用，避免散落 `getattr`

**测试：** `tests/test_etl/test_calc_spec_registry.py` — 12 管线与 Calculator 类一致。

### Task 1.2 — `_modes_from_state_only` 增加 spec 校验

**文件：** `backend/etl/calc_batch_append.py`

- [x] `_modes_from_state_only(..., spec_versions: dict)`：对每个 `(indicator,freq)`，若 `state.spec_version != expected` → return `None`（fallthrough 到 chunk / batch_full）
- [x] `try_force_same_day_batch_shortcut` 传入 `INDICATOR_SPEC_VERSIONS`

**测试：** `tests/test_etl/test_force_same_day_shortcut.py`

### Task 1.3 — `_should_skip_calc_idempotent` spec stale 检测

**文件：** `backend/etl/orchestrator.py` + 新 helper `backend/etl/calc_spec_gate.py`

- [x] `has_spec_stale_indicators(con) -> bool`：抽样或聚合 `dws_calc_state` vs `INDICATOR_SPEC_VERSIONS`
- [x] `force=True` 且 spec stale → return `False`（不整段 idempotent skip）
- [x] `CALC_FORCE_HARD=1` 仍 bypass（现有行为）

**测试：** `tests/test_etl/test_calc_spec_gate.py`

### Task 1.4 — Volume spec 门禁对齐

**文件：** `backend/etl/calc_volume.py`, `calc_batch_append.py` (`batch_full_volume`)

- [x] `calculate()`：`check_dwd_unchanged(..., expected_spec_version=self.SPEC_VERSION, latest_specs=latest_specs)`
- [x] `batch_full_volume`：加 `check_spec=True`, `latest_specs=load_latest_spec_versions(...)`

### Task 1.5 — state 写入一致性（防御性）

**文件：** `backend/etl/calc_state.py`, batch append 写 state 路径

- [x] `build_skip_state_records`：SKIP 刷新保留 `st["spec_version"]`，不 advance 到 code 版

### Task 2.1 — `v_dq_spec_freshness` 视图

**文件：** `backend/db/schema.py`

- [x] 12 指标 UNION 视图，列：`indicator`, `freq`, `anchor_trade_date`, `total`, `spec_ok`, `spec_stale`, `expected_spec`

### Task 2.2 — `scripts/health_check.py` 接入

- [x] Section J：`spec_freshness` + `ma_alignment_audit`

### Task 2.3 — `ods_etl_log` 观测字段

**文件：** `backend/etl/orchestrator.py`（run_calc 收尾）

- [x] `calc_dws.data_completeness.spec_stale_counts`

### Task 3.1 — Runbook 专节

- [x] `docs/superpowers/plans/2026-06-09-daily-runbook.md`, `CLAUDE.md`

### Task 3.2 — Spec 升级写入 data-model spec

- [x] `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`

### Task 3.3 — `cli calc --refresh-spec`

- [x] `backend/cli.py`, `backend/etl/calc_spec_refresh.py`

**范围说明：** 符合决策树「仅 spec 变 → 按指标窄窗 FULL」，比 HARD 全 calc 更小。

---

## ⑦ Track 4 — run export 软门禁（P2，可选）

**文件：** `backend/etl/orchestrator.py`（run  Step export 前）

- [ ] 环境变量 `EXPORT_SPEC_GATE=1`（默认 0，避免阻断日常）
- [ ] 若 `v_dq_spec_freshness.spec_stale > 0` 对 soft-layer 指标（ma）→ WARNING 或 abort

---

## ⑧ 受影响文件全量清单

| 文件 | Track | 变更类型 |
|------|-------|----------|
| `backend/etl/calc_indicators.py` | 1 | 注册表 |
| `backend/etl/calc_batch_append.py` | 1 | shortcut + volume batch_full |
| `backend/etl/orchestrator.py` | 1,2,4 | idempotent + log |
| `backend/etl/calc_spec_gate.py` | 1 | 新 helper |
| `backend/etl/calc_volume.py` | 1 | calculate check_spec |
| `backend/db/schema.py` | 2 | DQ 视图 |
| `scripts/health_check.py` | 2 | 新 section |
| `backend/cli.py` | 3 | --refresh-spec（可选） |
| `docs/superpowers/plans/2026-06-09-daily-runbook.md` | 3 | SOP |
| `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` | 3 | 契约 |
| `CLAUDE.md` | 3 | 命令/注意事项 |
| `tests/test_etl/test_force_same_day_shortcut.py` | 1 | 新 |
| `tests/test_etl/test_calc_spec_registry.py` | 1 | 新 |
| `tests/test_etl/test_calc_volume.py` 或现有 | 1 | 扩展 |

**不改动：** DWD rebuild 路径、DWS 表 DDL、`v_*_latest` 定义、export_wide 列映射、Layer 4 八象限。

---

## ⑨ 实施顺序与里程碑

```
Week 0（立即）
  Track 0: MA v2 HARD calc + audit + export  → Gate 3 通过

Week 1
  Track 1.1–1.3: 路由 spec 感知（核心）
  Track 1.4: Volume 对齐
  pytest tests/test_etl/ -v

Week 2
  Track 2.1–2.2: DQ 视图 + health_check
  Track 3.1–3.2: runbook + spec 文档

Week 3（可选）
  Track 3.3: --refresh-spec CLI
  Track 4: export 软门禁
```

| 里程碑 | 完成标准 |
|--------|----------|
| **M0** | MA v2 全市场 latest + audit=0 + 新 export |
| **M1** | force 同日复跑 + spec bump 场景 pytest 绿；无 L2 blind SKIP |
| **M2** | health_check spec_freshness 可观测 |
| **M3** | runbook 发布；下次 spec bump 可按 SOP 执行 |

---

## ⑩ 测试策略

| 层级 | 内容 |
|------|------|
| 单元 | shortcut / idempotent / volume check_spec |
| 集成 | `--force` 同 day + state v1 → 至少 MA 走 FULL |
| 等价性 | 现有 `test_append_calc.py` 不因路由改动而破坏 |
| 实库 | Track 0 audit + spec 覆盖率 SQL |
| 回归 | `pytest tests/test_etl/test_calc_ma.py tests/test_etl/test_append_calc.py -v` |

---

## ⑪ 风险与缓解

| 风险 | 缓解 |
|------|------|
| HARD calc 墙钟 ~15min | Track 3.3 `--refresh-spec ma` 缩小范围 |
| DuckDB lock 与 run 并行 | 运维 SOP：等 run 结束再 HARD |
| Volume v2 存量也未刷 | Task 1.4 + 可选 `refresh-spec volume` |
| over-engineering export gate | Track 4 默认关，`EXPORT_SPEC_GATE=0` |

---

## ⑫ 审批后执行入口

用户确认本 plan 后：

1. **若 M0 未做：** 先执行 Track 0（运维，无代码）
2. **代码：** 按 Track 1 Task 1.1 起 subagent-driven-development
3. **文档：** Track 3 与 Track 1 同步更新

**禁止在未审批时改路由代码。**
