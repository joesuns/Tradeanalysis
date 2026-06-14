# Calc 增量优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 calc 从「全 DWD 全量 × 12 pass」改为「RecalcSpec 窗口增量 + 域指纹 + 单管线 + 多进程 + 算法微增量」，保持指标口径与 API/export 语义不变。

**Architecture:** `RecalcSpec` 注册表聚合重算宽度与 warmup；P0.5 保守域指纹；P0 窄读窄写；P1 单股单管线；P2 `multiprocessing` + `CALC_WORKERS`；P3 PP deque / EMA 种子 / 背离向量化。每项可 `CALC_INCREMENTAL=0` 回退。

**Tech Stack:** Python 3.9+, DuckDB, NumPy/pandas, pytest, golden-master

**上游架构：** [`2026-06-07-calc-incremental-optimization.md`](2026-06-07-calc-incremental-optimization.md)（D1–D6 已批）

---

## 决策锁定（勿改）

| ID | 决定 |
|----|------|
| D1 | 指纹 A：`last_trade_date` 同 **且** 窗口子集指纹同 → skip |
| D3 | `RecalcSpec` 动态聚合，禁止 magic number |
| D4 | 无墙钟硬 KPI；记 `ods_etl_log` |
| D5 | P3 纳入首期 |
| D6 | `min(cpu-1,8)` + `CALC_WORKERS` 覆盖 |

---

## Task 1: RecalcSpec 注册表（P0 基础）

**Files:**
- Create: `backend/etl/recalc_spec.py`
- Create: `tests/test_etl/test_recalc_spec.py`
- Modify: 6× `backend/etl/calc_*.py`（仅加 `RECALC_SPEC_DAILY/WEEKLY` 类属性）

- [ ] **Step 1: 写失败测试** — 聚合 daily total=255（250+5 safety），warmup≥250

```python
def test_resolve_recalc_bars_daily_current_registry():
    from backend.etl.recalc_spec import resolve_recalc_bars, collect_specs
    specs = collect_specs("daily")
    assert resolve_recalc_bars(specs, safety=5) == 255
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/test_etl/test_recalc_spec.py -v`

- [ ] **Step 3: 实现 `RecalcSpec` + `collect_specs(freq)` + `resolve_recalc_bars`**

- [ ] **Step 4: 各 Calculator 声明 RecalcSpec**（值见架构 doc §5.0 表）

- [ ] **Step 5: 测试绿** — `pytest tests/test_etl/test_recalc_spec.py -v`

---

## Task 2: P0.5 域指纹（策略 A）

**Files:**
- Modify: `backend/etl/base.py`
- Modify: 6× `calc_*.py`（传入 `recalc_start` 给指纹函数）
- Modify: `tests/test_etl/test_fingerprint_skip.py`

- [ ] **Step 1: 写失败测试** — 同 df 连跑 skip；日增 1 bar 不 skip；全序列旧指纹行为废弃

- [ ] **Step 2: 实现 `compute_input_fingerprint(df, recalc_start)`**

  - `last_td = df.trade_date.max()`
  - `window_df = df[df.trade_date >= recalc_start]`（`recalc_start=None` 时全 df）
  - `fp = sha256(f"last_td:{last_td}|{compute_fingerprint(window_df)}")[:16]`

- [ ] **Step 3: 改造 `check_dwd_unchanged` 使用新指纹**

- [ ] **Step 4: quote 域 / moneyflow 域分离**（DDE 用 moneyflow 帧指纹）

- [ ] **Step 5: `pytest tests/test_etl/test_fingerprint_skip.py -v` 绿**

---

## Task 3: P0 窗口窄读窄写

**Files:**
- Modify: `backend/etl/base.py` — `load_quote_groups(..., start_date=None)`
- Modify: `backend/etl/calc_dde.py` — batch load `start_date`
- Modify: `backend/etl/orchestrator.py` — `resolve_recalc_start(con, calc_date, freq)`
- Modify: `backend/etl/base.py` — `insert_dws_batch(..., write_start=None, write_end=None)`
- Create: `tests/test_etl/test_incremental_calc.py`

- [ ] **Step 1: 写 golden-master 测试** — 10 股全量 oracle vs 窗口增量，窗口内 `atol=1e-9`

- [ ] **Step 2: `resolve_recalc_start`** — `dim_date` 回溯 `resolve_recalc_bars(specs)` 个交易日/周末

- [ ] **Step 3: 各 `calculate()` 传 `recalc_start` 给 load + insert 过滤**

- [ ] **Step 4: `WARMUP_TDAYS` / `WEEKLY_WARMUP_WEEKS` 改从注册表推导**

- [ ] **Step 5: feature flag `CALC_INCREMENTAL=1`（默认 1，0 回退全量）**

- [ ] **Step 6: golden-master 绿 + `pytest tests/test_etl/ -v`**

---

## Task 4: P1 单管线

**Files:**
- Modify: `backend/etl/orchestrator.py` — `_calc_stock_chunk` / 新 `calc_stock_pipeline`
- Test: `tests/test_etl/test_incremental_calc.py`（管线 vs 12 pass 等价）

- [ ] **Step 1: 写测试** — 单管线输出与串行 12 Calculator 逐字段相等

- [ ] **Step 2: 抽取 `calc_stock_pipeline(con, ts_code, calc_date, recalc_start)`**

  - 一次 `load_quote_groups`（quote 列全集）
  - 顺序调用 6 指标 × daily/weekly（保持现有 `_compute_*`）

- [ ] **Step 3: `_calc_stock_chunk` 改调 pipeline**

- [ ] **Step 4: 测试绿**

---

## Task 5: P2 多进程（D6 A+C）

**Files:**
- Modify: `backend/etl/orchestrator.py`
- Modify: `backend/config.py` — 读 `CALC_WORKERS`
- Modify: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: 实现 `resolve_calc_workers()`**

```python
def resolve_calc_workers() -> int:
    env = os.getenv("CALC_WORKERS", "").strip()
    if env:
        return max(1, int(env))
    return max(1, min(multiprocessing.cpu_count() - 1, 8))
```

- [ ] **Step 2: `ThreadPoolExecutor` → `multiprocessing.Pool`**

  - `initializer` 设日志 + DuckDB 路径（沿用 123 子进程读库模式）
  - 按股分片，互斥写入

- [ ] **Step 3: 测试 worker 解析** — mock env `CALC_WORKERS=4`

- [ ] **Step 4: 实跑记录 `ods_etl_log` 墙钟（观测，非 KPI）**

---

## Task 6: P3 PricePosition deque

**Files:**
- Modify: `backend/etl/calc_price_position.py`
- Modify: `backend/etl/base.py`（可选 `sliding_window_minmax_deque`）
- Test: `tests/test_etl/test_incremental_calc.py`

- [ ] **Step 1: golden-master** — deque 路径 vs 全 rolling 逐行相等

- [ ] **Step 2: 日更路径：仅新 `trade_date` bar 更新三窗口 PP**

- [ ] **Step 3: 滑出 bar 为极值时正确更新 min/max**

- [ ] **Step 4: 测试绿**

---

## Task 7: P3 EMA 种子（MACD + DDE）

**Files:**
- Modify: `backend/etl/calc_macd.py`, `backend/etl/calc_dde.py`, `backend/etl/base.py`
- Test: `tests/test_etl/test_incremental_calc.py`

- [ ] **Step 1: `load_ema_seed(con, dws_table, ts_code, calc_date, col)`** — 读上一 calc_date 末 bar 状态

- [ ] **Step 2: 窗口内递推 MACD（误差 vs 全量 <0.01% on 抽样）**

- [ ] **Step 3: DDE DDX2 EMA5 同理**

- [ ] **Step 4: golden-master 绿**

---

## Task 8: P3 背离向量化（MACD / DDE / Volume）

**Files:**
- Modify: `calc_macd.py`, `calc_dde.py`, `calc_volume.py`
- Test: 扩展现有 golden-master

- [ ] **Step 1: 冻结当前 `_compute_divergence` 为 oracle**

- [ ] **Step 2: 向量化实现（rolling max/argmax 或 Numba）**

- [ ] **Step 3: 随机 100 股 + 边界停牌 `atol=1e-9` + 分类字段相等**

- [ ] **Step 4: 测试绿**

---

## Task 9: 文档与收尾

**Files:**
- Modify: `CLAUDE.md` — RecalcSpec、CALC_WORKERS、CALC_INCREMENTAL、calc 增量语义
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` — §12.7 补充注册表口径

- [ ] **Step 1: 更新 CLAUDE.md**

- [ ] **Step 2: 更新 spec §12.7**

- [ ] **Step 3: 全量 `pytest tests/ -v`**

- [ ] **Step 4: `python -m backend.cli calc --date <最新交易日>` 两次（第二次 ≤30s）**

- [ ] **Step 5: `python -m scripts.health_check`**

---

## 实施顺序（严格）

```
Task 1 (RecalcSpec)
    ├─ Task 2 (P0.5 指纹)  ← 可与 Task 3 并行
    └─ Task 3 (P0 窄写)
           → Task 4 (P1 管线)
           → Task 5 (P2 多进程)
           → Task 6–8 (P3 可并行子任务)
           → Task 9 (文档)
```

**预估：** 8–10 工作日（单人）；P3 纳入首期 +2~3d。

---

## 回滚

| Flag | 效果 |
|------|------|
| `CALC_INCREMENTAL=0` | 回退全量读/写（指纹仍可保留） |
| git revert | 按 Task 原子提交便于回滚 |

---

## 审批

- [x] 架构方案（`2026-06-07-calc-incremental-optimization.md`）
- [ ] **本实施计划审批** — 用户回复「可以」后开始 Task 1
