# P4 指标级 Chunk + Batch FULL 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended). Parent agent 里程碑审核 + pytest。

**Goal:** 在 **零指标语义变更** 前提下，将稳态真新日 `chunk_stocks` 压到 **<400**，迁移日 mass FULL 走 **Batch FULL** 而非 5000 股 stock chunk；支撑用户约束 1（整条链路 ≤30min 含 export）与约束 3（少做无用计算）。

**前置（已 ship，勿重复）：** `calc_gate`、`backfill-state`、`CalcWorkQueue` 数据结构、`calc_stock_pipeline_selective` 按指标窄加载、`test_calc_zero_write_skip`。

**父计划：** `docs/superpowers/plans/2026-06-09-calc-fundamental-performance.md` Phase 2 Task 5 / 5b

**用户审批：** 2026-06-12（四条硬约束 SLA 合同 + 双档验收）

---

## 一、根因与目标

| 现象 | 根因 | 本计划对策 |
|------|------|-----------|
| `chunk_stocks=5003` | `build_work_queue` 后仍取 `full_stocks` 并集进 `_calc_stock_chunk` | Task 5：按 `full_items` 调度 |
| kpattern 周线迁移 ~5000 FULL | batch 阶段只处理 APPEND/SKIP | Task 5b：Batch FULL |
| `chunk_stocks` 误导 | 一股一指标 FULL 即计 1 股 | 新增 `chunk_work_items`、`full_by_indicator` |

**稳态验收：** `benchmark_run --date $ODS_MAX --run`（**含 export**）墙钟 ≤1800s；`chunk_stocks<400`；`health_check` 全绿。

**迁移日验收：** 墙钟 ≤1800s + `health_check`；L3 spot-check；**不卡 chunk**。

---

## 二、File Map

| 文件 | 变更 |
|------|------|
| `backend/etl/orchestrator.py` | chunk worker 改消费 `full_items`；`log_etl_end` 增 `chunk_work_items`、`full_by_indicator` |
| `backend/etl/calc_batch_append.py` | Task 5b `run_batch_full_phase`；batch 后余量进 chunk |
| `backend/etl/calc_executor.py` | 可选 `run_indicator_full_batch` 辅助 |
| `tests/test_etl/test_calc_executor.py` | 扩展 queue 消费测试 |
| `tests/test_etl/test_batch_full_equiv.py` | **新建** — Batch FULL golden |
| `tests/test_etl/test_orchestrator.py` | `chunk_work_items` 日志断言 |
| `CLAUDE.md` | calc chunk 观测字段 |

---

## Task 5 — 指标级 chunk worker

### Step 1: 失败测试 — chunk 按 work item 而非 stock

**Files:** `tests/test_etl/test_calc_executor.py`

- 断言 `group_by_indicator(full_items)` 将 macd weekly FULL 与 kpattern weekly FULL 分到不同组
- 断言 orchestrator mock：`chunk_work_items` = len(full_items)，非 len(unique stocks)

### Step 2: `run_calc` 改造

**Files:** `backend/etl/orchestrator.py`

```python
# 旧：chunk_codes = batch_ctx["chunk_codes"]  # 股票并集
# 新：
wq = build_work_queue(batch_ctx["stock_modes"], batch_ctx["completed_keys"])
full_groups = group_by_indicator(wq.full_items)
# chunk worker 按 (indicator, freq) 批处理，或按 work item 线程池
```

- `_calc_stock_chunk` 重命名为 `_calc_indicator_chunk` 或新增 `_calc_full_work_items`
- 进度：`progress calc.chunk.{indicator}_{freq}: N/M items`
- `chunk_stocks` 保留（去重股数，兼容附录 B）；新增 `chunk_work_items`

### Step 3: 尾窗复用

- FULL work item 复用 `batch_ctx` 已有 `daily_tails`/`weekly_tails`/`dde_*`（按股索引），避免 chunk 阶段重复 SQL

### Step 4: 验证

```bash
pytest tests/test_etl/test_calc_executor.py tests/test_etl/test_orchestrator.py -v
pytest tests/test_etl/test_append_calc.py -v
```

### Step 5: Commit

`perf: indicator-level chunk worker replaces stock-based scheduling`

---

## Task 5b — Batch FULL

### Step 1: 失败测试 — mass kpattern weekly FULL

**Files:** `tests/test_etl/test_batch_full_equiv.py`

- 内存库 N 股，统一 kpattern weekly FULL
- `run_batch_full_phase` vs 逐股 `calc_stock_pipeline_selective` — `atol=1e-9`

### Step 2: 实现 `run_batch_full_phase`

**Files:** `backend/etl/calc_batch_append.py`

- 输入：`full_groups: Dict[(indicator,freq), List[ts_code]]`
- 对每组：共享 `batch_load_quote_tails` / `batch_load_dde_tails`（窄窗 recalc）
- 调用现有 Calculator 窄窗 FULL 路径（或抽 `calc_indicator_full_batch`）
- `insert_dws_batch_multi` 窄写
- 刷新 `dws_calc_state` + `completed_keys`

### Step 3: 接入 `run_batch_append_phase` 尾部

```
batch APPEND（现有）
  → batch FULL（5b，按指标组）
  → 余量 fallthrough → Task 5 chunk worker
```

### Step 4: 观测字段

`ods_etl_log.calc_dws.data_completeness` 增加：

```json
{
  "chunk_stocks": 12,
  "chunk_work_items": 15,
  "full_by_indicator": {"kpattern_weekly": 5003, "dde_daily": 12},
  "batch_full_items": 5003
}
```

### Step 5: 实库验收

```bash
# 稳态 ODS_MAX 新日（非迁移）
python3 scripts/benchmark_run.py --date $ODS_MAX --run
python3 scripts/health_check.py

# 对比 20260610 基线（迁移日，记录不拦 chunk）
```

### Step 6: Commit

`perf: batch FULL for mass single-indicator FULL migrations`

---

## Migration playbook（算法变更日）

1. 备份：`cp data/tradeanalysis.duckdb data/tradeanalysis.pre-YYYYMMDD.duckdb`
2. 跑 `cli run --date $DATE`；预期 `full_by_indicator` 尖峰
3. `health_check` 全绿 → 可 export 交付
4. L3：抽 50 股 × 受影响 `(indicator,freq)`，新旧 `calc_date` 同 `trade_date` 比对
5. **不以 chunk_stocks 拦交付**

---

## 里程碑

| 里程碑 | 完成标准 |
|--------|---------|
| M5a Task 5 | pytest 绿；`chunk_work_items` 可见；稳态 chunk_stocks 下降 |
| M5b Task 5b | `test_batch_full_equiv` 绿；迁移日墙钟 < 20260610 基线 |
| M5 签字 | 稳态 `benchmark_run --run`（含 export）≤1800s + health_check |

**Plan approved 2026-06-12.**

### 实施状态（2026-06-12）

| Task | 状态 | 证据 |
|------|------|------|
| Task 5 指标级 chunk | ✅ | `_calc_full_work_chunk`；`chunk_work_items`；63+ pytest |
| Task 5b Batch FULL | ✅ | `run_batch_full_phase`；`CALC_BATCH_FULL`；`test_batch_full_equiv`；85 pytest |
| M5 稳态 benchmark | ⏳ | 待实库 `benchmark_run --run`（含 export） |
