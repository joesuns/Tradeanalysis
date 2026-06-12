# 跨股向量化 APPEND（Batch Append Calc）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将新交易日 calc 从 ~30–50 min（~1.6 stk/s 逐股 APPEND）压到 **低分钟级（目标 3–8 min）**，通过在 `run_calc` 内对 APPEND 资格股按 `(indicator, freq)` **跨股批处理**，消除 5388 次逐股 pipeline 固定开销。

**Architecture:** 保留现有 SKIP / FULL 逐股路径不变（FULL 作 oracle）。新增 `CALC_BATCH_APPEND=1`（默认开，需 `CALC_APPEND=1`）时：`run_calc` 先 batch 路由全体 APPEND 股，再按指标调用 `batch_append_calculate()`——一条 SQL 取尾窗、一条 SQL 取 EMA 种子、numpy 跨股算新 bar、`insert_dws_batch` 窄写。等价性：batch 结果 vs 现有 `append_calculate()` 逐股，`atol=1e-9`（扩展现有 `test_append_calc.py` 模式）。

**Tech Stack:** Python 3.9、DuckDB、numpy/pandas、pytest；复用 `load_quote_groups`、`resolve_ema_seeds`、`insert_dws_batch`、`classify_calc_mode`、`CALC_ROUTE_SPECS`。

**前置依赖：** `CALC_APPEND` + `dws_calc_state` + 逐股 `append_calculate()` 已上线（`2026-06-07-calc-append-only-impl.md`）。本 plan 是其实库瓶颈跟进项（设计 spec §5 + impl plan 范围提示）。

**关联文档：**
- `docs/superpowers/specs/2026-06-07-calc-append-only-design.md` §5
- `docs/superpowers/plans/2026-06-07-calc-append-only-impl.md`（逐股 APPEND 已完成）

---

## File Map

| 文件 | 职责 |
|------|------|
| `backend/config.py` | `CALC_BATCH_APPEND` flag |
| `backend/etl/calc_batch_append.py` | **新建** — 跨股 batch 路由 + 各指标 batch_append |
| `backend/etl/calc_batch_seeds.py` | **新建** — 批量 EMA/zone 种子 SQL |
| `backend/etl/orchestrator.py` | `run_calc` 接入 batch 路径；FULL/SKIP 仍走 `_calc_stock_chunk` |
| `backend/etl/calc_macd.py` 等 | 可选：抽取 `_compute_indicators` 供 batch 复用（最小 diff 优先） |
| `tests/test_etl/test_batch_append_calc.py` | **新建** — golden 等价性 |
| `CLAUDE.md` / spec §12.7 | 文档 |

---

## Phase 0 — 开关与 batch 路由骨架

### Task 1: `CALC_BATCH_APPEND` 配置项

**Files:**
- Modify: `backend/config.py`
- Test: `tests/test_etl/test_batch_append_calc.py`（新建空文件 + 首个测试）

- [x] **Step 1: Write the failing test**

创建 `tests/test_etl/test_batch_append_calc.py`：

```python
"""Golden tests: batch append vs per-stock append_calculate (atol=1e-9)."""
import os


def test_calc_batch_append_defaults_on():
    """CALC_BATCH_APPEND defaults to enabled when unset."""
    env = os.environ.pop("CALC_BATCH_APPEND", None)
    try:
        import importlib
        import backend.config as cfg
        importlib.reload(cfg)
        assert cfg.CALC_BATCH_APPEND is True
    finally:
        if env is not None:
            os.environ["CALC_BATCH_APPEND"] = env


def test_calc_batch_append_respects_zero():
    os.environ["CALC_BATCH_APPEND"] = "0"
    try:
        import importlib
        import backend.config as cfg
        importlib.reload(cfg)
        assert cfg.CALC_BATCH_APPEND is False
    finally:
        os.environ.pop("CALC_BATCH_APPEND", None)
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py -v`

Expected: FAIL（`CALC_BATCH_APPEND` 不存在）

- [x] **Step 3: Add to config.py**

在 `CALC_APPEND` 行后追加：

```python
# CALC_BATCH_APPEND: when on (default), new-day APPEND routes eligible stocks
# through cross-stock vectorized batch path. Requires CALC_APPEND.
CALC_BATCH_APPEND = os.getenv("CALC_BATCH_APPEND", "1").strip() != "0"
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/config.py tests/test_etl/test_batch_append_calc.py
git commit -m "feat: add CALC_BATCH_APPEND flag (default on)"
```

---

### Task 2: batch 路由聚合器

**Files:**
- Create: `backend/etl/calc_batch_append.py`
- Test: `tests/test_etl/test_batch_append_calc.py`

- [x] **Step 1: Write the failing test**

```python
import pandas as pd
from backend.etl.calc_batch_append import partition_stocks_by_mode


def test_partition_stocks_by_mode_groups_append_and_full():
    modes = {
        "A.SZ": {("macd", "daily"): "APPEND", ("ma", "daily"): "SKIP"},
        "B.SZ": {("macd", "daily"): "FULL",  ("ma", "daily"): "APPEND"},
    }
    append, full, skip = partition_stocks_by_mode(modes, "macd", "daily")
    assert set(append) == {"A.SZ"}
    assert set(full) == {"B.SZ"}
    assert skip == []
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py::test_partition_stocks_by_mode_groups_append_and_full -v`

Expected: FAIL

- [x] **Step 3: Implement `calc_batch_append.py` skeleton**

```python
"""Cross-stock batch APPEND orchestration."""
from typing import Dict, List, Set, Tuple

ModeMap = Dict[str, Dict[Tuple[str, str], str]]


def partition_stocks_by_mode(
    stock_modes: ModeMap,
    indicator: str,
    freq: str,
) -> Tuple[List[str], List[str], List[str]]:
    """Split ts_codes into (append_list, full_list, skip_list) for one indicator×freq."""
    append, full, skip = [], [], []
    for ts_code, modes in stock_modes.items():
        m = modes.get((indicator, freq), "FULL")
        if m == "APPEND":
            append.append(ts_code)
        elif m == "SKIP":
            skip.append(ts_code)
        else:
            full.append(ts_code)
    return append, full, skip
```

- [x] **Step 4: Run test to verify it passes**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_append.py tests/test_etl/test_batch_append_calc.py
git commit -m "feat: add batch append stock partition helper"
```

---

## Phase 1 — 批量 EMA 种子（MACD tracer bullet）

### Task 3: `load_ema_seeds_batch`

**Files:**
- Create: `backend/etl/calc_batch_seeds.py`
- Test: `tests/test_etl/test_batch_append_calc.py`

- [x] **Step 1: Write the failing test**

使用 `:memory:` DuckDB + 最小 DWS 表，插入两只股各 2 行，断言 batch 取种与逐股 `resolve_ema_seeds` 一致。

```python
import duckdb
import pandas as pd
from backend.etl.calc_batch_seeds import load_ema_seeds_batch


def test_load_ema_seeds_batch_matches_single():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_macd_daily (
            ts_code TEXT, trade_date TEXT, calc_date TEXT,
            ema_12 DOUBLE, ema_26 DOUBLE, dea DOUBLE
        )
    """)
    con.execute("""
        INSERT INTO dws_macd_daily VALUES
        ('A.SZ','20260101','20260105', 10.0, 20.0, 0.1),
        ('A.SZ','20260102','20260105', 10.5, 20.2, 0.12),
        ('B.SZ','20260101','20260105', 30.0, 40.0, 0.2),
        ('B.SZ','20260102','20260105', 30.1, 40.1, 0.21)
    """)
    recalc_start = "20260102"
    batch = load_ema_seeds_batch(
        con, "dws_macd_daily", ["A.SZ", "B.SZ"], recalc_start,
        ("ema_12", "ema_26", "dea"),
    )
    assert batch["A.SZ"]["ema_12"] == 10.0  # bar before 20260102
    assert batch["B.SZ"]["dea"] == 0.2
    con.close()
```

- [x] **Step 2: Run test to verify it fails**

Expected: FAIL

- [x] **Step 3: Implement `load_ema_seeds_batch`**

```python
"""Batch load EMA/state seeds for cross-stock APPEND."""
from typing import Dict, List, Tuple


def load_ema_seeds_batch(
    con,
    dws_table: str,
    ts_codes: List[str],
    before_trade_date: str,
    seed_cols: Tuple[str, ...],
) -> Dict[str, dict]:
    """Return {ts_code: {col: value}} for the latest DWS row with trade_date < before_trade_date."""
    if not ts_codes:
        return {}
    ph = ",".join(["?"] * len(ts_codes))
    cols = ", ".join(seed_cols)
    rows = con.execute(f"""
        SELECT ts_code, {cols} FROM (
            SELECT ts_code, {cols},
                   ROW_NUMBER() OVER (
                       PARTITION BY ts_code ORDER BY calc_date DESC, trade_date DESC
                   ) AS rn
            FROM {dws_table}
            WHERE ts_code IN ({ph}) AND trade_date < ?
        ) WHERE rn = 1
    """, list(ts_codes) + [before_trade_date]).fetchall()
    out = {}
    for row in rows:
        code = row[0]
        out[code] = {seed_cols[i]: row[i + 1] for i in range(len(seed_cols))}
    return out
```

- [x] **Step 4: Run test to verify it passes**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_seeds.py tests/test_etl/test_batch_append_calc.py
git commit -m "feat: batch EMA seed loader for cross-stock append"
```

---

### Task 4: MACD `batch_append_macd`

**Files:**
- Modify: `backend/etl/calc_batch_append.py`
- Test: `tests/test_etl/test_batch_append_calc.py`

- [x] **Step 1: Write golden test（扩展现有 `_make_df` 模式）**

对 3 只模拟股、各 300 bar：先 FULL 建 DWS state，追加 1 根新 bar；逐股 `append_calculate` vs `batch_append_macd`，断言新 bar 全列 `atol=1e-9`。

```python
def test_batch_append_macd_matches_per_stock_append(con_with_macd_tables):
    """Batch MACD append == per-stock append_calculate on new bar."""
    # Setup: 3 stocks, FULL baseline, add 1 bar each, compare outputs.
    ...
    for col in ("ema_12", "ema_26", "dif", "dea", "macd_bar", "trend_strength"):
        assert abs(batch_val[col] - single_val[col]) <= 1e-9 or (
            pd.isna(batch_val[col]) and pd.isna(single_val[col])
        )
```

（实现时使用 `tests/test_etl/test_append_calc.py` 的 `_make_df` / fixture 模式；完整 fixture 在本 task 写全，不得留 `...` 占位——工程师实施时复制 `_make_df` 并建 `:memory:` schema。）

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py::test_batch_append_macd_matches_per_stock_append -v`

Expected: FAIL

- [x] **Step 3: Implement `batch_append_macd`**

核心逻辑（在 `calc_batch_append.py`）：

```python
def batch_append_macd(
    con, freq: str, ts_codes: list, calc_date: str,
    quote_groups: dict, state_map: dict, new_bars_map: dict,
) -> "CalcResult":
    """Cross-stock MACD APPEND for one freq. quote_groups pre-loaded via load_quote_groups."""
    from backend.etl.calc_macd import MACDCalculator
    from backend.etl.calc_batch_seeds import load_ema_seeds_batch
    from backend.etl.base import compute_history_signature, insert_dws_batch
    from backend.etl.orchestrator import CalcResult

    calc = MACDCalculator(con, freq)
    agg = CalcResult()
    # Group by first new bar date for seed batch (usually 1 date on daily update)
    for ts_code in ts_codes:
        df = quote_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or not new_bars:
            continue
        seeds = load_ema_seeds_batch(
            con, calc.dws_table, [ts_code], new_bars[0],
            ("ema_12", "ema_26", "dea"),
        ).get(ts_code)
        seed_tuple = (
            seeds.get("ema_12") if seeds else None,
            seeds.get("ema_26") if seeds else None,
            seeds.get("dea") if seeds else None,
        )
        out = calc._compute_indicators(df, ema_seeds=seed_tuple)
        fp = compute_history_signature(out, calc.SIGNATURE_COLS)
        if calc._insert(ts_code, out, calc_date, input_fingerprint=fp,
                        write_start=new_bars[0], write_end=new_bars[-1]):
            agg.calculated += 1
    return agg
```

**优化注：** Task 4 先 correctness（循环内逐股，但共享 `quote_groups` 已省 N 次 SQL）；Task 5 再把 `_compute_indicators` 改为真跨股 numpy。

- [x] **Step 4: Run test to verify it passes**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_append.py tests/test_etl/test_batch_append_calc.py
git commit -m "feat: batch MACD append with shared quote load"
```

> **实现注记：** 种子加载用 `df.iloc[0]["trade_date"]`（对齐 `resolve_ema_seeds` 锚点），非 `new_bars[0]`。

---

## Phase 2 — 其余 quote 指标 + DDE

### Task 5: batch append — MA / KPattern / Volume / PricePosition

**Files:**
- Modify: `backend/etl/calc_batch_append.py`
- Test: `tests/test_etl/test_batch_append_calc.py`

- [x] **Step 1:** 每个 Calculator 加一个 golden test（复制 Task 4 模式，复用 `test_append_calc.py` helpers）
- [x] **Step 2:** 实现 `batch_append_ma`, `batch_append_kpattern`, `batch_append_volume`, `batch_append_priceposition`
- [x] **Step 3:** `python3 -m pytest tests/test_etl/test_batch_append_calc.py -v` 全绿（11 passed）
- [ ] **Step 4: Commit** `feat: batch append for quote-based indicators`

### Task 6: batch append — DDE daily/weekly

**Files:**
- Modify: `backend/etl/calc_batch_append.py`, `backend/etl/calc_dde.py`
- Test: `tests/test_etl/test_batch_append_calc.py`

- [x] **Step 1:** golden test（moneyflow 帧 + ddx2 种子 batch）
- [x] **Step 2:** `batch_append_dde` 复用 `_load_daily_batch` / `_load_weekly_batch(tail_window=245)` 一次取整组
- [x] **Step 3:** pytest 绿
- [ ] **Step 4: Commit** `feat: batch append for DDE indicators`

---

## Phase 3 — 编排接入

### Task 7: `run_calc` batch 路径

**Files:**
- Modify: `backend/etl/orchestrator.py`
- Test: `tests/test_etl/test_batch_append_calc.py` + `tests/test_etl/test_orchestrator.py`

- [x] **Step 1: Write integration test**

`test_run_batch_append_phase_pure_append_empty_chunk`：全 APPEND 时 `chunk_codes==[]` 且 batch 函数被调用一次。

- [x] **Step 2: Implement batch path in `run_calc`**

在 `codes_to_calc` 确定后、ThreadPoolExecutor 之前：

```python
    from backend.config import CALC_APPEND, CALC_BATCH_APPEND

    if CALC_APPEND and CALC_BATCH_APPEND and not user_subset:
        # 1. batch load all calc_state
        # 2. batch load quote tails (daily + weekly) for all codes_to_calc
        # 3. classify_calc_mode per (stock, indicator, freq) → stock_modes
        # 4. For each (indicator, freq) in CALC_ROUTE_SPECS:
        #      append_codes → batch_append_*()
        #      full_codes   → defer to _calc_stock_chunk selective/full
        #      skip_codes   → record fingerprint_match
        # 5. Only full_codes (+ fallthrough) enter ThreadPoolExecutor
```

**关键：** FULL 股仍走现有 `_calc_stock_chunk`；SKIP 直接记 skip；APPEND 走 batch 函数。

- [x] **Step 3: Run tests**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py tests/test_etl/test_orchestrator.py tests/test_etl/test_append_calc.py -v`

Expected: PASS（全库 409 passed）

- [ ] **Step 4: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_batch_append_calc.py tests/test_etl/test_orchestrator.py
git commit -m "feat: wire CALC_BATCH_APPEND into run_calc orchestration"
```

---

### Task 8: 真跨股 numpy 向量化（性能二期，可选）

**Files:**
- Modify: `backend/etl/calc_batch_append.py`, `backend/etl/calc_macd.py`

- [ ] **Step 1:** MACD EMA 递推改为 `(n_stocks, n_new_bars)` 数组运算
- [ ] **Step 2:** micro-benchmark：250 股 × 1 bar < 100ms
- [ ] **Step 3:** golden 仍绿
- [ ] **Step 4: Commit** `perf: vectorize cross-stock MACD EMA recurrence`

> YAGNI：Task 7 共享 batch load 已显著减 SQL；若实库仍 >8min 再做 Task 8。

---

## Phase 4 — 文档与实库验收

### Task 9: 文档

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` §12.7

- [x] **Step 1: CLAUDE.md calc 段追加**（§calc 三层架构 + 进度前缀）
- [x] **Step 2: spec 配置表加 `CALC_BATCH_APPEND`**（§12.7）
- [x] **Step 3: Commit** `docs: document CALC_BATCH_APPEND batch path`（随 Plan 2 feat commit）

---

### Task 10: 实库验收（场景 2 — 端到端日更）

- [x] **Step 1: 新日首跑 benchmark**（2026-06-08 实库，**部分完成** — 用户中断）

**Run A（脏场景，`calc --date 20260608`，无 `--force`）** — 已 kill

| 阶段 | 耗时 | 说明 |
|------|------|------|
| warmup fetch | ~5 min | 673 股缺失 |
| stale ODS 4860 股 × 27 天 | ~19s fetch + **~48 min DWD 重建**（无进度心跳，看似卡死） |
| calc chunk | ~1.0 stk/s | **batch_append 未触发**（全 FULL）；kill @ 49% |

**Run B（干净场景，`calc --date 20260608 --force`）** — ✅ **完成**（`real 3729s` ≈ **62 min**）

| 阶段 | 耗时 | 观测 |
|------|------|------|
| 前置 fetch/rebuild | ~1 min | stale 仅 11 股；weekly_fetch 537 股 0 行 |
| **batch_append 阶段** | **~10 min**（20:12→20:22） | `2839 stocks fully handled (APPEND/SKIP)` |
| chunk FULL 阶段 | **~48 min**（2911s） | `2549 stocks need chunk`；`calc ALL DONE` 8,416,313 rows |

| 检查项 | 目标 | Run B 结果 |
|--------|------|------------|
| 日志含 batch append 路径 | ✅ | ✅ `batch_append:` + 12×`BATCH` 行 |
| `calculated > 0` | ✅ | ✅ 8,416,313 rows written |
| calc 墙钟 | **<8 min** | ❌ **62 min**（calc 段 2911s） |
| stk/s | **>10** | ❌ 全市场 **~1.8 stk/s**（5388/2911s）；chunk 段 **~0.9 stk/s** |

**根因：** 上次大规模 DWD 重建（Run A 的 27 天回补）使 **47% 股（2549）尾窗签名变 → FULL**；`--force` 加重 chunk 负担。稳态日更（ODS 已齐、无 `--force`、仅 +1 bar）APPEND 比例应显著更高——需下一轮 benchmark 验证。

- [ ] **Step 2: 等价性抽检**

```bash
CALC_BATCH_APPEND=0 python3 -m backend.cli calc --date <同一新日> --force
# 抽样 10 股对比 v_dws_*_latest 新 bar 列，atol=1e-9
```

- [ ] **Step 3: 端到端 run**

```bash
python3 -m backend.cli run --date <新交易日>
```

查询：

```sql
SELECT step_name,
       row_count,
       json_extract_string(data_completeness, '$.analysis_date') AS ad
FROM ods_etl_log
WHERE step_name IN ('run_fetch','run_rebuild_dwd','calc_dws','run_export')
ORDER BY started_at DESC LIMIT 10;
```

- [ ] **Step 4: 全量 pytest**

Run: `python3 -m pytest tests/ -v`

Expected: 绿

---

## Self-Review

| 设计 spec §5 要求 | 任务 |
|------------------|------|
| 批量取尾窗 | Task 7 共享 `load_quote_groups` |
| 批量 EMA 种子 | Task 3 |
| 向量化算新 bar | Task 4–6（correctness）→ Task 8（perf） |
| 只 INSERT 新 bar | 复用 `_insert` / `insert_dws_batch` |
| FULL oracle 等价 | 各 Task golden `atol=1e-9` |
| 三层开关 | `CALC_BATCH_APPEND` → 回退逐股 APPEND → `CALC_INCREMENTAL=0` |

## 风险

| 风险 | 缓解 |
|------|------|
| batch vs 逐股不等价 | golden 每指标锁定 |
| FULL 股仍慢（除权日） | 预期；除权股占比低 |
| 编排复杂度 | feature flag 一键回退 |
| DuckDB 写冲突 | APPEND 写量小；沿用线程池仅处理 FULL 股 |

## 验收标准

- [ ] `CALC_BATCH_APPEND=1` 新日 calc <8 min（实库）— **未达标**；batch 路径已验证，瓶颈在 FULL 股比例 + chunk 逐股 I/O
- [ ] `CALC_BATCH_APPEND=0` 与改造前行为一致
- [x] 6 指标 × 2 频 golden 全绿（409 passed）
- [x] `pytest tests/ -v` 绿
- [x] CLAUDE.md + spec §12.7 已更新
