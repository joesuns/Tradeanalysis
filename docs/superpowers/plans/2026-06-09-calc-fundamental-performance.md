# Calc 根本性性能架构（Quality Gate + 指标图执行 + 向量化 + 零写 SKIP）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**立项：** 2026-06-12（pipeline M6 / 附录 D）。触发证据：`20260610` benchmark `chunk_stocks=5003`、`calc_dws=598s`；M4 墙钟 977s PASS 但附录 B chunk 门禁 FAIL。

**用户审批（2026-06-12）：** 稳态真新日整条链路（**含 export**）≤30min；双档验收（稳态 vs 迁移日）；首个实施 **Task 5**（非 Phase 0）。子计划：`docs/superpowers/plans/2026-06-12-p4-indicator-chunk-impl.md`。

**计划审计（2026-06-12）：** Phase 0 Task 0–1、Phase 1 Task 2、Phase 2 Task 4、Task 6 已 ship；`needs_full` 已移除。剩余核心：**Task 5 + Task 5b（Batch FULL）**。

**Goal:** 在 **零指标语义变更、强签名门禁不变** 前提下，将 **真新日 calc 本体** 从实库 ~48–72 min 压到 **≤5 min（stretch ≤3 min）**，同日复跑 **≤60s**，端到端 `fetch+calc` **≤10 min**（fetch 单独立项）。

**Architecture:** 当前性能瓶颈不是 CPU 公式，而是 **(1) 错误 calc_date 触发全量假跑**、**(2) 每股/每指标固定 I/O**、**(3) SKIP 仍可能写窄窗快照**、**(4) 2549 股 FULL chunk 占 ~48 min**（Run B 实库）。本方案分四支柱：**P0 数据质量门禁** → **P1 指标级执行图（打破 any-FULL 拖全股）** → **P2 物化尾窗 + 跨股向量化** → **P3 写入模型收敛（APPEND 只写新 bar）**。每层可独立开关、可 golden-master 验收。

**Tech Stack:** Python 3.9、DuckDB、pandas、numpy、pytest；沿用 `dws_calc_state` / `classify_calc_mode` / `insert_dws_batch_multi`。

**实库基线（2026-06-08/09 调查 + 2026-06-12 M4 签字）：**

| 场景 | 当前 | 根因 | 目标 |
|------|------|------|------|
| 假新日（calc_date>ODS max） | **72 min**，10M 行无效快照 | 无 fail-fast；5388 股全 chunk partial_run | **拒绝运行** |
| 真新日（20260608 Run B） | **62 min**（batch 10min + chunk 48min） | 2549 股 FULL chunk；周线窄窗重快照 | **≤5 min** |
| 真新日（20260610 签字跑） | **598s calc** / 977s E2E；chunk **5003** | kpattern 签名变更 FULL 尖峰 + 整股 chunk poison | calc **≤300s**，chunk **<400** |
| 真新日（稳定态 preflight） | 5183 batch_only / 341 chunk | 缺 state 136 + DDE 401 | chunk **≤400 股** |
| 同日复跑 | **319s**（20260610 silent-gap）/ 630s（--force） | partial skip 未全 SKIP | **≤60s** |
| 端到端 fetch+calc | fetch ~320s + calc | freshness 与 calc 串行 | **≤10 min**（calc≤5min） |

**前置依赖：** `CALC_APPEND=1`、`CALC_BATCH_APPEND=1`、`CALC_FAST_SKIP=1`；性能专项 Task 1–7（`insert_dws_batch_multi` 等）已合入或待合入。

**关联文档：**
- `docs/superpowers/specs/2026-06-07-calc-append-only-design.md`（双路径语义源）
- `docs/superpowers/plans/2026-06-08-calc-performance-special.md`（Gen4 批写，**不足以达 5min**）
- `docs/superpowers/plans/2026-06-08-cross-stock-batch-append.md`（Run B 实库数据）
- `docs/superpowers/plans/2026-06-08-calc-partial-skip-v2.md`（partial skip 基线）

**范围外（单独立项）：** DuckDB 多文件分片、GPU、改写 `v_*_latest` 为 mutable 表（Phase 5 可选，需 spec 修订）。

---

## 架构师判断（Honest Judgment）

### 已做优化（方向正确，未吃满）

| 代际 | 机制 | 实库效果 |
|------|------|----------|
| Gen1 | 指纹 SKIP + 多线程 | 同日复跑秒级 |
| Gen2 | CALC_APPEND 1-bar | 设计目标 ~49min |
| Gen3 | batch append + partial skip v2 | batch 2839 股 / chunk 2549 股 |
| Gen4 | 批 INSERT / 批 seed / state 降噪 | **ROI 限于 APPEND 路径** |

### 根本矛盾（三条）

1. **执行单元错配：** 路由按 **(股, 指标, 频)**，执行仍按 **(股)** — `any(FULL)` 把整股推入 chunk，chunk 内 `needs_full` 把同组 5 个 quote 指标一起宽窗加载。
2. **写入放大：** DWS INSERT-only 快照 + 窄窗 FULL → 新 `calc_date` 可写 **~950k 周线行**（无新 weekend bar 时仍发生，Run B）。
3. **无数据仍算：** `calc_date=datetime.now()` 与 `MAX(ods_daily)` 脱节 → 72min 灾难路径。

### 不建议的路径

- **加索引 / 加线程：** 索引已覆盖 `(ts_code, trade_date)`；DuckDB 单文件多写线程已用；边际 <10%。
- **回退快照模型：** `v_*_latest` 依赖 `calc_date` 快照，不可静默改语义。
- **弱签名换速度：** 245 尾窗 SHA256 是质量红线（除权 FULL 触发靠此）。

---

## 目标架构（四支柱）

```mermaid
flowchart TD
    A[run_calc 入口] --> B{calc_date <= ods_max?}
    B -->|否| X[FAIL 或 WARN+--force]
    B -->|是| C[resolve_effective_calc_date]
    C --> D[batch_load tails 物化尾窗或 SQL]
    D --> E[preflight 5524×12 路由矩阵]
    E --> F[SKIP: 零 DWS 写 + 可选 state 刷新]
    E --> G[APPEND: 跨股向量化批算 + 1-bar INSERT]
    E --> H[FULL: 仅该指标窄窗 入队]
    H --> I[chunk 仅处理 FULL 队列 非整股]
    G --> J[checkpoint]
    I --> J
    F --> J
```

---

## File Map

| 文件 | 职责 |
|------|------|
| `backend/etl/calc_gate.py` | **新建** — `resolve_calc_date` / `assert_data_ready` |
| `backend/cli.py` | calc/run 默认 calc_date 对齐 ODS max |
| `backend/etl/orchestrator.py` | 指标级 FULL 队列；chunk 不再按整股 poison |
| `backend/etl/calc_batch_append.py` | 向量化批算入口；按 `(indicator,freq)` 调度 |
| `backend/etl/calc_executor.py` | **新建** — `CalcWorkQueue` / `run_indicator_batch` |
| `backend/etl/build_dwd_tails.py` | **新建** — 物化 `dwd_quote_tail_245` |
| `backend/db/schema.py` | 尾窗表 DDL + 索引 |
| `backend/etl/calc_state_backfill.py` | 一次性 state 回填（`backfill-state` CLI） |
| `backend/config.py` | `CALC_STRICT_DATE=1`、`CALC_VECTOR_APPEND=1` |
| `tests/test_etl/test_calc_gate.py` | **新建** |
| `tests/test_etl/test_calc_executor.py` | **新建** |
| `tests/test_etl/test_vector_append.py` | **新建** — golden vs 逐股 oracle |
| `CLAUDE.md` / spec §12.7 | 文档 |

---

## Phase 0 — 数据质量门禁（✅ 已 ship 2026-06-11）

> **ROI：** 零公式改动；避免无效 10M 行写入。

### Task 0: `calc_gate` — calc_date 与 ODS 对齐（✅）

**Files:**
- Create: `backend/etl/calc_gate.py`
- Modify: `backend/etl/orchestrator.py`（`run_calc` 入口）
- Modify: `backend/cli.py`（`cmd_calc` / `cmd_run`）
- Test: `tests/test_etl/test_calc_gate.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_etl/test_calc_gate.py
import duckdb
import pytest

from backend.db.schema import create_all_tables
from backend.etl.calc_gate import resolve_effective_calc_date, assert_calc_date_ready


def test_resolve_effective_calc_date_caps_to_ods_max():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute("INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
                "VALUES ('000001.SZ', '20260608', 1,1,1,1,1,1)")
    eff = resolve_effective_calc_date(con, requested="20260609")
    assert eff == "20260608"


def test_assert_calc_date_ready_raises_when_ahead_of_ods():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    con.execute("INSERT INTO ods_daily (ts_code, trade_date, open, high, low, close, vol, amount) "
                "VALUES ('000001.SZ', '20260608', 1,1,1,1,1,1)")
    with pytest.raises(ValueError, match="calc_date.*20260609.*ods_max.*20260608"):
        assert_calc_date_ready(con, "20260609", strict=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_calc_gate.py -v`  
Expected: FAIL — `ModuleNotFoundError: calc_gate`

- [ ] **Step 3: Implement `calc_gate.py`**

```python
# backend/etl/calc_gate.py
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def get_ods_max_trade_date(con) -> Optional[str]:
    row = con.execute("SELECT MAX(trade_date) FROM ods_daily").fetchone()
    return row[0] if row and row[0] else None


def resolve_effective_calc_date(con, requested: str, cap_to_ods: bool = True) -> str:
    """Return min(requested, ods_max) when cap_to_ods and ods_max exists."""
    if not cap_to_ods:
        return requested
    ods_max = get_ods_max_trade_date(con)
    if ods_max and requested > ods_max:
        logger.warning(
            "calc_date %s ahead of ods_max %s — capping to ods_max",
            requested, ods_max,
        )
        return ods_max
    return requested


def assert_calc_date_ready(con, calc_date: str, strict: bool = True) -> None:
    ods_max = get_ods_max_trade_date(con)
    if ods_max and calc_date > ods_max:
        msg = (
            f"calc_date {calc_date} > ods_max {ods_max}: "
            "no market data for requested date. "
            "Run fetch first or use --date {ods_max}."
        )
        if strict:
            raise ValueError(msg)
        logger.warning(msg)
```

- [ ] **Step 4: Wire into `run_calc` and CLI**

在 `run_calc` 开头（`ensure_calc_state_table` 之后）：

```python
from backend.config import CALC_STRICT_DATE
from backend.etl.calc_gate import resolve_effective_calc_date, assert_calc_date_ready

if calc_date is None:
    from datetime import datetime
    calc_date = datetime.now().strftime("%Y%m%d")

if CALC_STRICT_DATE:
    assert_calc_date_ready(con, calc_date, strict=True)
else:
    calc_date = resolve_effective_calc_date(con, calc_date, cap_to_ods=True)
```

`backend/config.py` 追加：

```python
CALC_STRICT_DATE = os.getenv("CALC_STRICT_DATE", "1").strip() != "0"
```

CLI `cmd_calc` 帮助文本注明：默认 strict；`CALC_STRICT_DATE=0` 自动 cap 到 ODS max。

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_etl/test_calc_gate.py tests/test_cli.py -v`  
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/etl/calc_gate.py backend/config.py backend/etl/orchestrator.py backend/cli.py tests/test_etl/test_calc_gate.py
git commit -m "feat: reject calc_date ahead of ODS max to prevent phantom calc runs"
```

---

### Task 1: `ods_etl_log` 可观测性 — 写入路由统计（✅）

**Files:**
- Modify: `backend/etl/orchestrator.py`（`log_etl_end` data_completeness）
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: Write failing test** — mock `run_batch_append_phase` 返回 `chunk_codes`/`batch_only` 计数，断言 `data_completeness` JSON 含 `batch_only`、`chunk_stocks`、`ods_max`。

- [ ] **Step 2–5:** 在 `run_calc` 收尾 `log_etl_end` 增加：

```python
data_completeness={
    "calc_date": calc_date,
    "stocks": len(codes_to_calc),
    "ods_max": get_ods_max_trade_date(con),
    "batch_only": len(codes_to_calc) - len(chunk_codes),
    "chunk_stocks": len(chunk_codes),
}
```

- [ ] **Step 6: Commit** — `feat: log calc batch/chunk split in ods_etl_log`

---

## Phase 1 — State 回填 + Gen4 验收（Task 2 ✅；Task 3 稳态待复测）

> **根因：** 136 股缺 quote state、401 股缺 DDE state → 永久 FULL。Run B 2549 FULL 远高于稳定态 341。

### Task 2: 一次性 `backfill_calc_state` CLI（✅）

**Files:**
- Create: `backend/etl/calc_state_backfill.py`
- Modify: `backend/cli.py`（子命令 `backfill-state`）
- Test: `tests/test_etl/test_calc_state_backfill.py`

- [ ] **Step 1: Write failing test** — 内存库 2 股无 state，跑 backfill 后 `dws_calc_state` 行数 = 2×12。

- [ ] **Step 2: Implement** — 对无 state 的 `(ts_code,freq,indicator)` 调用现有 `calc_stock_pipeline` **一次**（或窄窗 FULL），写入 state；**不重复**若 state 已存在。

```python
def backfill_calc_state(con, ts_codes: list, calc_date: str) -> dict:
    """One-time FULL for missing state rows only."""
    from backend.etl.calc_state import load_calc_state_batch
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS
    # ... for each missing key, calc_stock_pipeline selective FULL only that indicator
```

- [ ] **Step 3: CLI**

```bash
python -m backend.cli backfill-state [--date YYYYMMDD] [--ts-code ...]
```

- [ ] **Step 4: 实库运维** — 全市场跑一次（预计 ~30min，**一次性**）；之后 chunk 应 ≈341 股。

- [ ] **Step 5: Commit** — `feat: backfill dws_calc_state for perpetual-FULL stocks`

---

### Task 3: Gen4 性能专项合入 + 真新日 benchmark

**Files:** 见 `2026-06-08-calc-performance-special.md`

- [ ] **Step 1:** 确认 Task 1–7 已合入 main；`pytest tests/test_etl/test_batch_append_calc.py -v` 全绿
- [ ] **Step 2:** 实库验收（**必须** `calc_date == MAX(ods_daily)`）：

```bash
python -m backend.cli fetch
ODS_MAX=$(python3 -c "import duckdb; print(duckdb.connect('data/tradeanalysis.duckdb').execute('select max(trade_date) from ods_daily').fetchone()[0])")
python -m backend.cli calc --date "$ODS_MAX" --force 2>&1 | tee /tmp/calc_bench.log
grep -E 'batch_append|calc ALL DONE|partial_skip' /tmp/calc_bench.log
```

- [ ] **Step 3:** 记录 `ods_etl_log` 行 — 目标：batch_only ≥90%，calc 本体 vs Run B 有下降（**未达 5min 则进入 Phase 2**）

---

## Phase 2 — 指标级执行图（P1，打破 any-FULL 拖全股）

> **核心改动：** chunk 不再处理「整股」，只处理 `CalcWorkItem(ts_code, indicator, freq, mode)` 队列。

### Task 4: `CalcWorkQueue` 数据结构（✅ 数据结构已 ship；消费在 Task 5）

**Files:**
- Create: `backend/etl/calc_executor.py`
- Test: `tests/test_etl/test_calc_executor.py`

- [ ] **Step 1: Write failing test**

```python
from backend.etl.calc_executor import build_work_queue

def test_build_work_queue_splits_by_indicator_not_stock():
    stock_modes = {
        "A.SZ": {
            ("macd", "daily"): ("SKIP", []),
            ("macd", "weekly"): ("FULL", []),
            ("ma", "daily"): ("APPEND", ["20260608"]),
        },
    }
    q = build_work_queue(stock_modes, completed_keys=set())
    assert ("A.SZ", "macd", "weekly", "FULL") in q.full_items
    assert ("A.SZ", "ma", "daily", "APPEND") in q.append_items
    assert all(x[0] != "A.SZ" or x[1] != "macd" or x[2] != "daily" for x in q.full_items)
```

- [ ] **Step 2–4:** 实现 `build_work_queue` / `CalcWorkQueue`（`skip_items` / `append_items` / `full_items`）

- [ ] **Step 5: Commit** — `refactor: introduce indicator-level CalcWorkQueue`

---

### Task 5: 重构 `run_batch_append_phase` + `_calc_stock_chunk` 消费队列

**Files:**
- Modify: `backend/etl/calc_batch_append.py`
- Modify: `backend/etl/orchestrator.py`
- Modify: `backend/etl/orchestrator.py` — `calc_stock_pipeline_selective` 移除 `needs_full` 组级 poison

**关键语义变更（质量不变）：**

```python
# 旧：同组任一 FULL → 整组宽窗
needs_full = any(run_modes.get((n,f),("FULL",[]))[0]=="FULL" for n,_ in specs)

# 新：按指标独立 load — APPEND/SKIP 用 245 tail；FULL 单独 load_quote_groups([ts_code], start=load_start)
for indicator_name, CalcCls in specs:
    mode = run_modes.get((indicator_name, freq), ("FULL", []))[0]
    if mode == "SKIP":
        continue
    if mode == "APPEND":
        df_ind = tail_frames.get((freq, source))  # 245 only
    else:
        df_ind = load_quote_groups(..., [ts_code], start_date=load_start).get(ts_code)
```

- [ ] **Step 1:** golden test — 混合 mode 股（macd SKIP + kpattern FULL）断言 macd 零 SQL、 kpattern 一次窄窗
- [ ] **Step 2:** 实现 selective 按指标 load
- [ ] **Step 3:** chunk worker 遍历 `full_items` 而非 `for ts_code in chunk`
- [ ] **Step 4:** `run_batch_append_phase` 不再 `chunk_codes.add` on any FULL — 改由 queue 驱动
- [ ] **Step 5:** `pytest tests/ -v` + `test_append_calc.py` golden 全绿
- [ ] **Step 6: Commit** — `perf: indicator-level FULL queue replaces whole-stock chunk poison`

**预期收益：** 341 股 × 平均 1–2 指标 FULL × 窄窗 ≈ **5–15 min → 2–5 min**（较 Run B 48min chunk 量降 10×+）。

**实施子计划：** `docs/superpowers/plans/2026-06-12-p4-indicator-chunk-impl.md`

---

### Task 5b: Batch FULL — 单指标 mass FULL 批算（新增，P0）

> **根因：** 20260610 kpattern 周线签名变更 → ~5000 股 FULL；APPEND 批路径只处理 APPEND/SKIP，FULL 仍落 stock chunk。

**Files:**
- Modify: `backend/etl/calc_batch_append.py` — `run_batch_full_phase` 或扩展 batch 阶段
- Modify: `backend/etl/orchestrator.py` — batch 后仅余少量 fallthrough 进 chunk worker
- Test: `tests/test_etl/test_batch_full_equiv.py`（**新建**）

**量化护栏（硬约束）：**
- 每股独立递推，禁止跨股 EMA/zone 状态串扰
- Batch FULL 输出 ≡ 逐股 `calc_stock_pipeline_selective` FULL（`atol=1e-9`）
- 按 `(indicator,freq)` 分批；单批失败不污染其他指标

- [ ] **Step 1:** golden — kpattern weekly mass FULL：batch vs 逐股 oracle
- [ ] **Step 2:** `group_by_indicator(wq.full_items)` → 共享 tail 窄窗批算 + `insert_dws_batch_multi`
- [ ] **Step 3:** `ods_etl_log` 增 `full_by_indicator`、`chunk_work_items`
- [ ] **Step 4:** 实库 — 迁移日墙钟 vs 20260610 基线对比
- [ ] **Step 5: Commit** — `perf: batch FULL for mass single-indicator migrations`

---

### Task 6: SKIP 零 DWS 写 硬断言（✅ 已 ship）

**Files:**
- Modify: `backend/etl/orchestrator.py` — `_route_calc` / batch SKIP 路径
- Test: `tests/test_etl/test_calc_zero_write_skip.py`（**新建**）

- [ ] **Step 1:** 测试 — 全 SKIP 股在 calc 前后 `COUNT(*) WHERE calc_date=X` 不变
- [ ] **Step 2:** 审计 `_route_calc` FULL 分支 — `mode==SKIP` 禁止调用 `insert_dws_batch*`
- [ ] **Step 3:** 实库 — 同日复跑 row_count 增量 **0**（已有 idempotent；新日 SKIP 指标同样 0）
- [ ] **Step 4: Commit** — `fix: enforce zero DWS writes on SKIP routing`

---

## Phase 3 — 物化尾窗（P2，消灭重复 tail SQL）

### Task 7: `dwd_quote_tail_245` 表

**Files:**
- Modify: `backend/db/schema.py`
- Create: `backend/etl/build_dwd_tails.py`
- Modify: `backend/etl/build_dwd.py` — `rebuild_all_dwd` 末尾调用 `rebuild_quote_tails`
- Modify: `backend/etl/calc_fast_skip.py` — 优先读尾窗表

- [ ] **Step 1: DDL**

```sql
CREATE TABLE IF NOT EXISTS dwd_quote_tail_245 (
    ts_code     VARCHAR NOT NULL,
    freq        VARCHAR NOT NULL,  -- daily | weekly
    trade_date  VARCHAR NOT NULL,
    -- quote columns mirror dwd_daily_quote / weekly subset
    PRIMARY KEY (ts_code, freq, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_tail245_code_freq ON dwd_quote_tail_245(ts_code, freq);
```

- [ ] **Step 2:** `rebuild_quote_tails(con, ts_codes)` — 从 DWD 刷 245 行/股（week-end filter 周线）
- [ ] **Step 3:** `batch_load_quote_tails` 改读 tail 表（fallback 旧 SQL 当表空）
- [ ] **Step 4:** 测试 — tail 表行 == `batch_load_quote_tails` 旧路径逐股相等
- [ ] **Step 5: Commit** — `perf: materialized 245-bar quote tail cache`

**预期：** preflight + batch 阶段 SQL 从 **O(stocks/400)** 降到 **O(1–2)**。

---

## Phase 4 — 跨股向量化 APPEND（P2，兑现设计 §5）

> 设计见 `2026-06-07-calc-append-only-design.md` §5；batch 已批读，**缺批算**。

### Task 8: 向量化 MACD EMA 递推

**Files:**
- Create: `backend/etl/vector/macd_batch.py`
- Modify: `backend/etl/calc_batch_append.py`
- Test: `tests/test_etl/test_vector_append.py`

- [ ] **Step 1:** golden — 250 股 × 1 new bar，vector vs 逐股 `append_calculate`，`atol=1e-9`
- [ ] **Step 2:** `(n_stocks, n_new_bars)` numpy 递推 + `load_ema_seeds_batch` 种子
- [ ] **Step 3:** micro-bench — 5000 股 × 1 bar **<500ms**（纯算，不含 INSERT）
- [ ] **Step 4:** 开关 `CALC_VECTOR_APPEND=1`
- [ ] **Step 5: Commit** — `perf: vectorized cross-stock MACD APPEND`

### Task 9: 向量化 MA / DDE / Volume zone（同上模式）

- [ ] Task 9a: MA slope + alignment batch
- [ ] Task 9b: DDE ddx2 EMA batch
- [ ] Task 9c: Volume zone seed + ratio batch
- [ ] Task 9d: PricePosition rolling min/max deque batch（复用 `rolling_window_minmax_deque`）

**预期：** 5183 股 daily APPEND 从 **~10 min → ~30–60s**（Run B batch 阶段）。

---

## Phase 5 — Fetch 解耦（P1 端到端）

### Task 10: `run` 快路径 + stale fetch 优化

**Files:**
- Modify: `backend/cli.py` — `cmd_run`
- Modify: `backend/fetch/ods_daily.py`

- [ ] **Step 1:** `run` Step1 fetch 返回 `ods_max`；Step2 calc 强制 `calc_date=ods_max`
- [ ] **Step 2:** `find_stale_ods_codes` — 当 `analysis_date > ods_max` 且全市场 uniform，**单次 date-batched** 而非 4860 股 stock-batched（Run A 48min DWD 教训）
- [ ] **Step 3:** 文档 + 实库 — 端到端计时拆分 fetch / calc / export
- [ ] **Step 4: Commit** — `perf: align run calc_date to fetch ods_max`

---

## 验收标准（双档 — 用户四条硬约束，2026-06-12 审批）

### 稳态真新日（合同门禁）

| 检查项 | 命令 | 目标 |
|--------|------|------|
| **整条链路 ≤30min（含 export）** | `python3 scripts/benchmark_run.py --date $ODS_MAX --run` | exit 0，墙钟 ≤1800s |
| 数据质量 | `python3 scripts/health_check.py` | 无 CRITICAL |
| calc 分项 | `ods_etl_log.calc_dws` | `chunk_stocks<400`，`calc_dws≤300s` |
| 等价性 | `pytest tests/test_etl/test_append_calc.py -v` | 全绿 atol=1e-9 |
| 假新日拒绝 | `calc --date 20991231` | ERROR，0 行 DWS |
| 不全库 rebuild | 日志 grep | 无 `dwd.rebuild stocks=all` |

### 迁移日（SIGNATURE/SPEC 变更）

| 检查项 | 命令 | 目标 |
|--------|------|------|
| 可交付 | `benchmark_run --run` + `health_check` | exit 0 + 无 CRITICAL |
| 截面一致 | L3 spot-check 脚本/手工 | 50 股 × 受影响指标，`atol=1e-9` |
| 观测 | `full_by_indicator` | 记录 mass FULL，**不卡 chunk** |

### Phase 3–4 完成后（stretch）

| 检查项 | 目标 |
|--------|------|
| calc 本体 | ≤5 min |
| 同日复跑 | 第二次 ≤60s |
| 向量化 | `test_vector_append.py` 全绿 |

---

## 实施顺序与里程碑

| 里程碑 | Phase | 预估 | 新日 calc | 风险 |
|--------|-------|------|-----------|------|
| M0 防灾难 | 0 | 1d | N/A | 低 |
| M1 chunk≤400 | 1–2 | 3–5d | ~15–25 min | 低 |
| M2 指标队列 | 2 | 3–5d | ~5–10 min | 中（golden 必须） |
| M3 向量化 | 3–4 | 1–2w | **≤5 min** | 中 |
| M4 端到端 | 5 | 2–3d | run ≤10min | 低 |

**推荐路径（2026-06-12 修订）：** ~~M0~~ ✅ → **Task 5 + 5b**（2 周）→ M3 尾窗 → M4 向量化 → Phase 5 fetch。

---

## Self-Review（plan vs 用户四条硬约束）

| # | 用户要求 | 任务 | 稳态可达 |
|---|---------|------|---------|
| 1 | 整条链路 ≤30min（含 export） | M1–M3 + Task 5/5b | **是**（典型 8–20min） |
| 2 | 数据质量 | 强签名 + golden + health_check | **是** |
| 3 | 无不必要计算 | Task 5/5b + DWD 增量 | **是**（迁移日 FULL 必要） |
| 4 | 不全库 rebuild | stale 子集（已落地） | **是** |

**Placeholder scan:** 无 TBD。

---

## Execution Handoff（2026-06-12 用户已审批）

**首个 Task：** ~~Phase 2 Task 5 + 5b~~ ✅ **已 ship**（2026-06-12）

**当前 Task（M1 P0）：** `docs/superpowers/plans/2026-06-13-calc-preflight-context-p0.md` — `CalcPreflightContext` 热路径

**下一 Task（M2 P1）：** 见下文 **附录 M2 — Vector Append 分层**（M1 签字后立项，**禁止与 M1 同 PR**）

**模式：** Subagent-Driven — 每 Task 后架构师 review + pytest + 稳态 benchmark（含 export）

---

## 附录 — 量化/架构验收合同（L1–L6，2026-06-13 整合）

| 级别 | 内容 | 门槛 | 失败 |
|------|------|------|------|
| **L1** | `pytest tests/ -v` | 全绿 | No-Go |
| **L2** | `test_append_calc.py` | APPEND≡FULL `atol=1e-9` | No-Go |
| **L3** | 结构背离 golden + smoke 25 | 无新增 diff | No-Go |
| **L4** | B4 硬门禁 12 列 | `diff_vs_123` 无 hard 回归 | No-Go |
| **L5** | `benchmark_run --run` | 稳态真新日墙钟 ≤1800s（含 export） | KPI（不替代 L1–L4） |
| **L6** | 路由不变量 | 稳态 `chunk=0`；迁移日 `full_by_indicator` 可观测 | No-Go |

**M1 附加 KPI：** `preflight_source=refresh` 时 `batch_tails`+`batch_preflight` 合计 **<15s**。

**M2 附加：** 随机 100 股 latest 行 blind diff（离散列完全相等，连续列 `atol=1e-9`）。

---

## 附录 M2 — Vector Append 分层（待 M1 后立项）

**触发：** M1 签字 + 真新日 E2E 仍 **>30min**（预期 ~58min）。

**实施顺序（分 PR）：**

| 波次 | 内容 | 文件 | 验收 |
|------|------|------|------|
| **M2a** | MACD/DDE EMA 跨股向量化；Volume `pct_rank` 向量化 | `backend/etl/vector/macd_batch.py`、`volume_batch.py`；`CALC_VECTOR_APPEND=1` | `tests/test_etl/test_vector_append.py` |
| **M2b** | 结构背离 **new_bars 输出裁剪**（全窗输入不变） | `divergence_structure.py` 扩展 `target_indices` | L3 golden |
| **M2c** | `volume_trend_v2` profiling → 做或不做 | spike only | 不阻塞 M2a/b 发布 |
| **并行** | 周五 `week_end` 真新日 benchmark | `benchmark_run` | 附录 B 单独一行 |

**禁止：** 关 `DWD_REBUILD_REFRESH_STATE`；缩 `SIG_WINDOW`；`vector/` 内复制 `b4_macd.py`（须 import 唯一源）。

**目标墙钟（周四真新日）：** M1+M2 E2E **15–20min**（含 export）。

---

## 附录 — 0fd66428 真新日基线（2026-06-11）

| 桶 | 秒 |
|----|-----|
| fetch+DWD | 29 |
| refresh_state | 344 |
| 路由重复（tails+preflight） | ~387 |
| batch_state | 345 |
| batch_compute（MACD/量能/DDE） | 2496 |
| export | 105 |
| **E2E** | **4078（68min）** |

路由：`chunk=0` `batch_only=5389` `batch_full=5`；`health_check` PASS。
