# Calc 路由双指纹对齐 + 热路径 chunk 修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 run `7c090546` 暴露的 calc 空转（chunk_codes 污染 + history_fp/DWS 指纹分叉），同 calc_date 复跑 Step2 从 ~515s 降至 <120s；Export 优化独立 PR。

**Architecture:** (1) batch_append 收尾重算 `chunk_codes`，不再保留热路径初始「ctx 外」membership；(2) preflight FULL 且无 new_bars 时，用 `check_dwd_unchanged` 降级 SKIP 并写 state；(3) batch_full fingerprint_match 兜底 upsert `history_fp`；(4) 热路径 cold merge 补 progress。质量门禁：禁止对 APPEND/spec 不匹配 bypass。

**Tech Stack:** Python 3.9、DuckDB、pytest；开关 `CALC_DWD_FP_GATE=1`（默认开）。

**实库锚点：** run_id `7c090546`，calc_date=20260612，chunk_stocks=5290，batch_full dde_weekly=5223，0 calculated。

---

## 里程碑与双角色验收

每个里程碑完成后 **必须** 通过下表签字方可进入下一里程碑。

| 里程碑 | 内容 | 数据架构师验收 | 量化交易专家验收 |
|--------|------|----------------|------------------|
| **M1** | Calc-R1 chunk_codes | `chunk_stocks=0`（同日复跑场景）；`fallthrough=0` 且 `work_items=0` 时不进 chunk worker | 同日复跑 DWS 列值与优化前 bit-equal（`v_*_latest` 抽样） |
| **M2** | Calc-R2 双指纹门 A+B | `full_by_indicator.dde_weekly=0`；`batch_full_items=0`；state `history_fp` 与 tail 对齐 | MACD/DDE 背离/zone/trend 信号无变化；`test_append_calc` + 新 gate 测试 PASS |
| **M3** | Calc-O1 观测 | 热路径 cold merge 有 `progress calc.batch_preflight`；`cold_merge_stocks` 写入 data_completeness | —（观测项） |
| **M4** | 文档 | pipeline 附录 E5 + CLAUDE.md | — |

**Export-E1**（building sheets）→ 独立 plan `2026-06-14-export-sheet-perf.md`，不在本 plan 范围。

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `backend/config.py` | `CALC_DWD_FP_GATE` 开关 |
| `backend/etl/calc_dwd_fp_gate.py` | DWS 指纹门 + dwd_fp 批缓存 |
| `backend/etl/calc_fast_skip.py` | preflight 接线 gate |
| `backend/etl/calc_batch_append.py` | chunk_codes 重算、cold merge 进度、batch_full state 对齐 |
| `backend/etl/orchestrator.py` | data_completeness 增 cold_merge 字段 |
| `tests/test_etl/test_calc_routing_chunk.py` | M1 测试 |
| `tests/test_etl/test_calc_dwd_fp_gate.py` | M2 测试 |

---

### Task 1: Calc-R1 — chunk_codes 重算（M1）

**Files:**
- Modify: `backend/etl/calc_batch_append.py`
- Test: `tests/test_etl/test_calc_routing_chunk.py`
- Modify: `tests/test_etl/test_batch_append_calc.py`（扩展 hot path 测试）

- [ ] **Step 1: Write failing test** — `test_hot_path_cold_merge_not_in_chunk_codes`

- [ ] **Step 2: Run test — expect FAIL**

- [ ] **Step 3: Add `_compute_chunk_codes(codes, stock_modes, full_items)`**

```python
def _compute_chunk_codes(codes, stock_modes, full_items):
    fallthrough = {ts for ts in codes if ts not in stock_modes}
    full_stocks = {ts for ts, _ in full_items}
    return sorted(fallthrough | full_stocks)
```

Replace final `chunk_codes |= ...` in `run_batch_append_phase` and `try_force_same_day_batch_shortcut`.

Remove `chunk_codes` mutation from `_merge_cold_tails_and_preflight` signature.

- [ ] **Step 4: Run tests — PASS**

Run: `pytest tests/test_etl/test_calc_routing_chunk.py tests/test_etl/test_batch_append_calc.py::test_batch_append_hot_path_after_partial_ctx_merge -v`

---

### Task 2: Calc-R2A — DWS 指纹门（M2）

**Files:**
- Create: `backend/etl/calc_dwd_fp_gate.py`
- Modify: `backend/config.py`
- Modify: `backend/etl/calc_fast_skip.py`
- Test: `tests/test_etl/test_calc_dwd_fp_gate.py`

- [ ] **Step 1: Failing tests** — stale history_fp + matching DWS fp → SKIP; mismatched DWS fp → FULL; APPEND with new_bars → no gate

- [ ] **Step 2: Implement `apply_dwd_fp_gate` + `build_dwd_fp_cache(con, codes, calc_date)`**

- [ ] **Step 3: Wire `preflight_stock_modes_with_fps(..., con=, calc_date=, dwd_fp_cache=)`**

- [ ] **Step 4: Wire cold path + `_merge_cold_tails_and_preflight`**

Run: `pytest tests/test_etl/test_calc_dwd_fp_gate.py -v`

---

### Task 3: Calc-R2B — batch_full state 对齐兜底（M2）

**Files:**
- Modify: `backend/etl/calc_batch_append.py` — `_batch_full_loop`

On `fingerprint_match` skip: if `state_map` + `SIGNATURE_COLS`, append to `align_state_records` → `upsert_calc_state_batch` after loop.

- [ ] **Step 1: Test** in `test_calc_dwd_fp_gate.py` or `test_batch_full_equiv.py`

- [ ] **Step 2: Implement + run pytest**

---

### Task 4: Calc-O1 — 热路径 cold merge 进度（M3）

**Files:**
- Modify: `backend/etl/calc_batch_append.py` — `_merge_cold_tails_and_preflight`
- Modify: `backend/etl/orchestrator.py` — data_completeness

- [ ] **Step 1: Add `log_timed_step` for 4 tails + `stock_progress` for preflight loop**

- [ ] **Step 2: Return `cold_merge_stocks` / `cold_merge_elapsed_sec` in batch_ctx**

---

### Task 5: 文档（M4）

- Modify: `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md` — 附录 E5
- Modify: `CLAUDE.md` — `CALC_DWD_FP_GATE`、chunk_codes 修复说明

---

### Task 6: 全量回归

Run: `pytest tests/test_etl/test_calc_routing_chunk.py tests/test_etl/test_calc_dwd_fp_gate.py tests/test_etl/test_batch_append_calc.py tests/test_etl/test_append_calc.py -v`

---

## 运维一次性（非本 plan 代码）

B4 元数据回填后若 M2 仍有大面积 stale state：

```bash
python -m backend.cli refresh-state --date 20260612
```

根因：存量 `history_fp` 未对齐；日常 run 靠 gate 自愈，无需全市场 refresh 成为常态。
