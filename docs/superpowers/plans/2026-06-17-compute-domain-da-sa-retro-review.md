# P2+ Compute Domain — DA/SA 追溯验收（M0–M4）

> **日期：** 2026-06-17  
> **分支：** `feat/batch-full-compute-domain`（base: `main`，6 commits）  
> **背景：** 实施时未按 plan §治理协议 完成 DA+SA 独立双签；用户批准 **Step B+C 补救**。  
> **状态：** 待 DA / SA / 用户 三方签字（**STOP — 双签完成前禁止 M5**）

**Plan：** [`2026-06-17-batch-full-compute-domain-optimization.md`](2026-06-17-batch-full-compute-domain-optimization.md)

---

## 验收方法

| 角色 | 职责 |
|------|------|
| **DA（数据架构师）** | 数据契约、算域=写域、export/DQ 截面一致性、oracle 等价性 |
| **SA（系统架构师）** | PR 边界、CLI/开关、无 breaking、性能 profile、schema/编排未越界 |
| **用户** | 最终裁决；Fail/待议项处置 |

**证据来源：** 分支 diff、`pytest` 输出、profile 脚本、代码路径 grep（2026-06-17 复验）。

---

## 全局证据

| 项 | 证据 |
|----|------|
| 分支 commits | `14eb961` M0 → `9e53464` M1 → `f2bd0ef` M2 → `3872aa0` M3 → `c5d7eb2` M4 |
| diff 规模 | 22 files, +1327 / -34；**未改** `orchestrator.CALCULATORS`、`export_wide` layout |
| hotfix 隔离 | `git stash@{0}`：`spec-gate-hotfix-and-export-comments WIP`（与本 PR 分离） |
| Plan 相关 pytest | **49/49 PASS**（2026-06-17，`test_calc_compute_domain` / `ops_spec_status` / `calc_spec_refresh` / `batch_full_compute_domain` / `b4_macd_weekly_append` / `append_calc` / CLI refresh-spec） |
| 全量 pytest | **696 PASS / 9 FAIL / 5 SKIP**（259s）；9 FAIL 均为 **实库 DuckDB 锁**（PID 51544）或 golden 读库，**非本 plan 代码路径** |
| MACD profile Q5 | `profile_macd_b4_weekly --stocks 100 --bars 245 --mode fast` → **10.9×**；`fast_write_window` → **10.2×** |
| Volume profile Q6 | `profile_volume_trend_v2 --stocks 50 --bars 245` → 写窗 **~188×**（M2 记录） |

---

## M0 — ADR + Runbook + 分支

### DA 验收

| # | 检查项 | 判定 | 证据 |
|---|--------|------|------|
| D0.1 | ADR 三层域与 export 截面语义一致（读 245 / 算写 `[recalc_start,calc_date]`） | **Pass** | `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` §Batch FULL 三层域；与 plan §② 一致 |
| D0.2 | `ACCEPTANCE_DATE=20260616` 与 runbook/S2 一致 | **Pass** | `daily-runbook.md` Migration 单页；plan header |

### SA 验收

| # | 检查项 | 判定 | 证据 |
|---|--------|------|------|
| S0.1 | PR 与 spec-gate hotfix **未混 diff** | **Pass** | 分支自 clean `main` 创建；hotfix 在 `stash@{0}`；本分支仅 docs（`14eb961`） |
| S0.2 | STOP 协议（S1–S8）可执行 | **Pass** | plan §治理协议 完整；本文件即为 S7 补救 |
| S0.3 | migration SOP 命令可落地 | **Pass** | runbook 6 步；`ops spec-status` 标注 M1 起可用 |

### M0 小结

| DA | SA | 阻塞项 |
|----|-----|--------|
| ☐ 待签 | ☐ 待签 | 无 |

---

## M1 — ops spec-status + dry-run + resolve_compute_indices

### DA 验收

| # | 检查项 | 判定 | 证据 |
|---|--------|------|------|
| D1.1 | `spec-status` 读 `v_dq_spec_freshness`，daily + weekly anchor | **Pass** | `ops_spec_status.fetch_spec_freshness_rows` + `resolve_weekly_anchor_trade_date`；`test_fetch_spec_freshness_rows_requires_view` |
| D1.2 | `--dry-run` **零 DWS 写** | **Pass** | `run_refresh_spec(..., dry_run=True)` 不调用 `run_batch_full_phase`；`test_run_refresh_spec_dry_run_no_writes` |
| D1.3 | `resolve_compute_indices` 边界确定性 | **Pass** | 4 tests PASS |

### SA 验收

| # | 检查项 | 判定 | 证据 |
|---|--------|------|------|
| S1.1 | 现有 `calc --refresh-spec` 非 dry-run 行为不变 | **Pass** | `dry_run=False` 默认；`test_run_refresh_spec_invokes_batch_full_when_stale` PASS |
| S1.2 | 测试不依赖实库 | **Pass** | M1 测试均 `:memory:` DuckDB |
| S1.3 | **scope：** plan §⑥ 写「不改动 schema」，M1 新增 `v_dq_spec_freshness` | **待议** | `backend/db/schema.py` +48 行 DQ 视图；spec §12 已文档化该视图；**建议 SA 裁定：M1 依赖只读 DQ 视图，不算 DWS/ADS 语义变更** |

### M1 小结

| DA | SA | 阻塞项 |
|----|-----|--------|
| ☐ 待签 | ☐ 待签 | S1.3 待 SA 书面裁定（Pass 或写入 plan 例外） |

---

## M2 — Volume FULL 算域

### DA 验收

| # | 检查项 | 判定 | 证据 |
|---|--------|------|------|
| D2.1 | 算域=写域（Volume `trend` 列） | **Pass** | `batch_full_volume` → `resolve_compute_indices` → `trend_target_indices` |
| D2.2 | Q2 oracle：写窗 index 与 expanding 逐 bar 相等 | **Pass** | `test_volume_trend_write_window_matches_expanding_{daily,weekly}` + 集成 test PASS |
| D2.3 | APPEND 路径未改 | **Pass** | `batch_append_volume` 仍 `require_trend_target_indices`；grep 无 M2 diff on append |

### SA 验收

| # | 检查项 | 判定 | 证据 |
|---|--------|------|------|
| S2.1 | APPEND 路径未改 | **Pass** | 同上 |
| S2.2 | profile Q6 ≥10× | **Pass** | ~188×（50 股 × 245 bar，M2 实跑） |
| S2.3 | 仅改 `batch_full_volume` compute lambda，无 orchestrator 变更 | **Pass** | diff `calc_batch_append.py` 局部 |

### M2 小结

| DA | SA | 阻塞项 |
|----|-----|--------|
| ☐ 待签 | ☐ 待签 | 无 |

---

## M3 — MACD B4 fast path

### DA 验收

| # | 检查项 | 判定 | 证据 |
|---|--------|------|------|
| D3.1 | Q1：fast vs expanding 全索引 + 写窗子集 + 多 seed | **Pass** | 15/15 `test_b4_macd_weekly_append.py`（seed 42/99/2025 × n_weeks 60/120/245） |
| D3.2 | `trend` / `turning_point` 完全相等（含 None 位置） | **Pass** | `assert fast_t == exp_t` and `fast_c == exp_c` 全序列 |
| D3.3 | `batch_full_macd` weekly 写窗 `b4_target_indices` | **Pass** | `calc_batch_append.batch_full_macd` + `resolve_compute_indices` |

### SA 验收

| # | 检查项 | 判定 | 证据 |
|---|--------|------|------|
| S3.1 | Q5 ≥5× | **Pass** | 10.9×（100 股 × 245 bar） |
| S3.2 | 未 bump `MACDCalculator.SPEC_VERSION`（仍 v3） | **Pass** | `calc_macd.py` `SPEC_VERSION = "v3"` 无 diff |
| S3.3 | `CALC_B4_WEEKLY_FAST=0` 回退 expanding | **Pass（代码审查）** | `calc_macd._apply_b4_trend_and_zone` 分支 `b4_weekly_series_from_daily` |
| S3.4 | **`CALC_B4_WEEKLY_FAST=0` 无集成测试** | **待议** | 建议补 1 条 env 切换 test 或 M5 实库 spot-check；**非 M3 阻塞** |

### M3 小结

| DA | SA | 阻塞项 |
|----|-----|--------|
| ☐ 待签 | ☐ 待签 | S3.4 待议（可选补测） |

---

## M4 — 文档 + pytest

### DA 验收

| # | 检查项 | 判定 | 证据 |
|---|--------|------|------|
| D4.1 | spec / runbook / ADR / CLAUDE 一致 | **Pass** | CLAUDE 三层域 + ops 命令；pipeline P2+ 附录；spec §三层域 |
| D4.2 | Q1–Q6 测试/profile 已记录 | **Pass** | 见「全局证据」 |

### SA 验收

| # | 检查项 | 判定 | 证据 |
|---|--------|------|------|
| S4.1 | 日常 `run` 命令面无 breaking change | **Pass** | 无 `run`/orchestrator diff；新能力为 opt-in CLI |
| S4.2 | 全量 `pytest tests/` 全绿 | **Fail（环境）** | 696/705；9 FAIL = DuckDB lock PID 51544 + `test_b4_golden_matches_db` 读实库；**非 plan 引入** |
| S4.3 | Plan 相关子集 49/49 | **Pass** | 2026-06-17 复验 |

### M4 小结

| DA | SA | 阻塞项 |
|----|-----|--------|
| ☐ 待签 | ☐ 待签 | S4.2 须 **DB 无锁后复跑全绿** 或用户书面接受环境例外 |

---

## 汇总签字表

| Milestone | DA | SA | 用户 | 备注 |
|-----------|----|----|------|------|
| M0 | ☐ | ☐ | ☑ 口头 | |
| M1 | ☐ | ☐ | ☑ 口头 | SA：S1.3 待议 |
| M2 | ☐ | ☐ | ☑ 口头 | |
| M3 | ☐ | ☐ | ☑ 口头 | SA：S3.4 待议 |
| M4 | ☐ | ☐ | ☐ | SA：S4.2 待复跑 |
| **M5** | — | — | — | **禁止启动** |

---

## 待议项处置（需用户 + SA/DA 裁定）

| ID | 项 | 建议 |
|----|-----|------|
| T1 | M1 新增 `v_dq_spec_freshness` vs plan「不改动 schema」 | 写入 plan §⑥ 例外：「只读 DQ 视图，M1 依赖」 |
| T2 | `CALC_B4_WEEKLY_FAST=0` 无自动化测试 | M5 前补 1 test 或实库 spot-check |
| T3 | 全量 pytest 9 FAIL（DB lock） | `lsof data/tradeanalysis.duckdb` 确认无持锁进程后复跑 |

---

## 三方签字（手写/回复）

```
DA 签字：________  日期：________  备注：________
SA 签字：________  日期：________  备注：________
用户：  ________  日期：________  备注：________
```

**全部 Milestone DA+SA 双签 + 用户确认后**，方可：
1. 更新 plan 总表 ☑  
2. 启动 M5 实库 E1–E3  

---

## STOP 状态（当前）

- **触发：** S7（DA/SA Review 未完成）+ 流程补救中  
- **解除条件：** 上表 M0–M4 DA+SA ☐→☑，T1–T3 处置完毕，S4.2 全绿或书面例外  
