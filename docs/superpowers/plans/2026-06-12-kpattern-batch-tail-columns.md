# K 线形态 Batch 尾窗列集修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 M4 真新日 `batch_append_kpattern` 因 `quote_tail_columns` 缺 `pct_chg` 导致的 `KeyError`，使 batch 尾窗列集与 per-stock pipeline 一致，并补全 kpattern 路由签名。

**Architecture:** `quote_tail_columns(freq)` 委托 `quote_pipeline_columns(freq)` 作为 canonical quote 计算输入列；kpattern `SIGNATURE_COLS` 与 `CALC_ROUTE_SPECS` 增加 `pct_chg`（涨跌停过滤门禁列）；新增契约测试防止 batch/pipeline 列漂移。`quote_sig_col_union()` 保留供诊断，不再作为 batch 加载源。

**Tech Stack:** Python 3.9+、DuckDB、pandas、pytest

**背景（M4 第 4 轮）：** `run_batch_append_phase` → MACD/MA 完成后 kpattern 崩溃：`calc_kpattern.py:104 df["pct_chg"] KeyError`。per-stock 路径用 `quote_pipeline_columns`（含 `pct_chg`），batch 路径用 `quote_sig_col_union()`（缺 `pct_chg`）。

---

## File Structure

| 文件 | 职责 |
|------|------|
| `backend/etl/calc_indicators.py` | `quote_tail_columns` 委托；`CALC_ROUTE_SPECS` kpattern 签名列 |
| `backend/etl/calc_kpattern.py` | `SIGNATURE_COLS` 增加 `pct_chg` |
| `tests/test_etl/test_calc_indicators.py` | **新建** — 列集契约测试 |
| `tests/test_etl/test_batch_append_calc.py` | batch kpattern 用真实 `quote_tail_columns` 尾窗的集成测 |
| `CLAUDE.md` | batch 尾窗列语义说明 |
| `docs/superpowers/plans/2026-06-08-calc-fast-skip-preflight.md` | 修正「列集 = SIGNATURE 并集」表述 |
| `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md` | 附录 B 记录 M4 第 4 轮事故 |

不涉及：schema DDL、export_wide、orchestrator CALCULATORS 列表（无新 Calculator）。

---

### Task 1: 列集契约测试（TDD — 先红）

**Files:**
- Create: `tests/test_etl/test_calc_indicators.py`

- [ ] **Step 1: 新建测试文件**

```python
"""Contract tests: batch tail columns must cover all quote calculator compute inputs."""
import pytest

from backend.etl.calc_indicators import (
    CALC_ROUTE_SPECS,
    quote_pipeline_columns,
    quote_tail_columns,
    quote_sig_col_union,
)
from backend.etl.calc_kpattern import KPatternCalculator
from backend.etl.calc_ma import MACalculator
from backend.etl.calc_macd import MACDCalculator
from backend.etl.calc_price_position import PricePositionCalculator
from backend.etl.calc_volume import VolumeCalculator

# Columns each quote calculator reads from its input DataFrame at compute time.
# Keep in sync with calculator implementations — this is the regression gate.
QUOTE_COMPUTE_INPUT_COLS = {
    "macd": ["close_qfq"],
    "ma": ["close_qfq"],
    "kpattern": ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol", "pct_chg"],
    "volume": ["close_qfq", "vol"],  # weekly also uses active_days via SIGNATURE_COLS
    "priceposition": ["close_qfq"],
}


@pytest.mark.parametrize("freq", ["daily", "weekly"])
def test_quote_tail_columns_equals_pipeline_columns(freq):
    assert quote_tail_columns(freq) == quote_pipeline_columns(freq)


@pytest.mark.parametrize("freq", ["daily", "weekly"])
def test_pipeline_columns_cover_all_quote_compute_inputs(freq):
    pipeline = set(quote_pipeline_columns(freq))
    for indicator, cols in QUOTE_COMPUTE_INPUT_COLS.items():
        if freq == "weekly" and indicator == "volume":
            cols = cols + ["active_days"]
        missing = [c for c in cols if c not in pipeline]
        assert not missing, f"{indicator}/{freq} compute needs {missing} not in pipeline"


@pytest.mark.parametrize("indicator,freq,CalcCls,sig_cols,source", CALC_ROUTE_SPECS)
def test_signature_cols_subset_of_pipeline(indicator, freq, CalcCls, sig_cols, source):
    if source != "quote":
        pytest.skip("dde uses moneyflow columns")
    pipeline = set(quote_pipeline_columns(freq))
    missing = [c for c in sig_cols if c not in pipeline]
    assert not missing, f"{indicator}/{freq} SIGNATURE_COLS {missing} not in pipeline"


def test_kpattern_signature_includes_pct_chg():
    assert "pct_chg" in KPatternCalculator.SIGNATURE_COLS


def test_quote_sig_col_union_includes_pct_chg_after_fix():
    assert "pct_chg" in quote_sig_col_union()
```

- [ ] **Step 2: 运行测试确认 FAIL**

```bash
pytest tests/test_etl/test_calc_indicators.py -v
```

Expected FAIL 示例：
- `test_quote_tail_columns_equals_pipeline_columns` — tail 缺 `pct_chg`
- `test_kpattern_signature_includes_pct_chg` — SIGNATURE_COLS 无 `pct_chg`
- `test_quote_sig_col_union_includes_pct_chg_after_fix` — union 无 `pct_chg`

---

### Task 2: 修复 `calc_indicators.py` 列定义

**Files:**
- Modify: `backend/etl/calc_indicators.py`

- [ ] **Step 1: 更新 kpattern CALC_ROUTE_SPECS 签名列**

将第 21–24 行 kpattern 条目改为（daily + weekly 各一处）：

```python
    ("kpattern", "daily", KPatternCalculator,
     ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol", "pct_chg"], "quote"),
    ("kpattern", "weekly", KPatternCalculator,
     ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol", "pct_chg"], "quote"),
```

- [ ] **Step 2: `quote_tail_columns` 委托 `quote_pipeline_columns`**

替换 `quote_tail_columns` 函数体：

```python
def quote_tail_columns(freq: str = "daily") -> list:
    """Columns for batch_load_quote_tails — same as per-stock pipeline quote load."""
    return quote_pipeline_columns(freq)
```

- [ ] **Step 3: 更新 `quote_sig_col_union` docstring**

```python
def quote_sig_col_union() -> list:
    """Union of SIGNATURE_COLS for quote-sourced indicators (diagnostics only).

    Batch tail loading uses quote_pipeline_columns(), not this union.
    """
```

- [ ] **Step 4: 更新 `quote_pipeline_columns` docstring**

```python
def quote_pipeline_columns(freq: str) -> list:
    """Canonical quote compute-input columns (per-stock pipeline + batch tails)."""
```

- [ ] **Step 5: 运行 Task 1 测试（部分应绿）**

```bash
pytest tests/test_etl/test_calc_indicators.py -v
```

Expected: `test_quote_tail_columns_equals_pipeline_columns` PASS；`test_kpattern_signature_includes_pct_chg` 仍 FAIL（Task 3 修）。

---

### Task 3: 修复 `calc_kpattern.py` SIGNATURE_COLS

**Files:**
- Modify: `backend/etl/calc_kpattern.py:26`

- [ ] **Step 1: 增加 `pct_chg`**

```python
    SIGNATURE_COLS = [
        "open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol", "pct_chg",
    ]
```

- [ ] **Step 2: 运行契约测试全绿**

```bash
pytest tests/test_etl/test_calc_indicators.py -v
```

Expected: 全部 PASS

---

### Task 4: Batch kpattern 真实尾窗集成测试

**Files:**
- Modify: `tests/test_etl/test_batch_append_calc.py`

- [ ] **Step 1: 在 `test_batch_append_kpattern_matches_per_stock_append` 之后新增测试**

```python
def test_batch_append_kpattern_via_quote_tail_columns():
    """Regression: batch tails from quote_tail_columns must include pct_chg."""
    import duckdb

    from backend.etl.calc_batch_append import batch_append_kpattern
    from backend.etl.calc_fast_skip import batch_load_quote_tails
    from backend.etl.calc_indicators import quote_tail_columns
    from backend.etl.calc_kpattern import KPatternCalculator

    codes = ["KP1.SZ"]
    con = duckdb.connect(":memory:")
    dates = _setup_quote_baseline(con, KPatternCalculator, codes, n=200, ohlcv=True)
    quote_tails = batch_load_quote_tails(
        con, codes, "daily", quote_tail_columns("daily"), window=80,
    )
    assert "pct_chg" in quote_tails[codes[0]].columns

    new_td = _append_new_bar(con, codes, dates, quote_tails)
    calc_date = new_td
    # Must not raise KeyError
    batch_append_kpattern(
        con, "daily", codes, calc_date, quote_tails, {c: [new_td] for c in codes},
    )
    cnt = con.execute(
        "SELECT COUNT(*) FROM dws_kpattern_daily "
        "WHERE ts_code = ? AND trade_date = ? AND calc_date = ?",
        [codes[0], new_td, calc_date],
    ).fetchone()[0]
    assert cnt == 1
    con.close()
```

- [ ] **Step 2: 运行新测试**

```bash
pytest tests/test_etl/test_batch_append_calc.py::test_batch_append_kpattern_via_quote_tail_columns -v
```

Expected: PASS

- [ ] **Step 3: 运行相关回归套件**

```bash
pytest tests/test_etl/test_calc_indicators.py \
       tests/test_etl/test_batch_append_calc.py::test_batch_append_kpattern_matches_per_stock_append \
       tests/test_etl/test_batch_append_calc.py::test_batch_append_kpattern_via_quote_tail_columns \
       tests/test_etl/test_calc_fast_skip.py -v
```

Expected: 全部 PASS（fast_skip 尾窗列变宽后仍应与 slow path 一致）

---

### Task 5: 文档更新

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/plans/2026-06-08-calc-fast-skip-preflight.md`
- Modify: `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md`

- [ ] **Step 1: CLAUDE.md — calc 增量优化段补一句**

在 `CALC_BATCH_APPEND` 或 batch 尾窗相关段落增加：

```markdown
- **batch 尾窗列：** `quote_tail_columns(freq)` 与 `quote_pipeline_columns(freq)` 相同（含 `pct_chg`），供 kpattern 涨跌停过滤；≠ 各指标 `SIGNATURE_COLS` 并集 alone。
```

- [ ] **Step 2: fast-skip 计划修正列集表述**

`docs/superpowers/plans/2026-06-08-calc-fast-skip-preflight.md` 第 86 行表格「列集」行改为：

```markdown
| 列集 | quote 用 `quote_pipeline_columns`（canonical 计算输入列，含 `pct_chg`）；DDE 用其 7 列 |
```

同文件 Task 1 `quote_sig_col_union` 注释改为「诊断用，batch 加载见 `quote_tail_columns` → `quote_pipeline_columns`」。

- [ ] **Step 3: pipeline 附录 B 记录 M4 第 4 轮**

在 `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md` 附录 B 表格追加：

```markdown
| M4 事故（第 4 轮） | ❌ exit=1 @1185s；kpattern `KeyError: pct_chg` |
| 根因 | batch `quote_tail_columns` 用 SIGNATURE 并集，缺 kpattern 计算列 `pct_chg` |
| 修复 | `2026-06-12-kpattern-batch-tail-columns.md` |
| 续跑 | 修后 `cli calc --date 20260610`（不必重跑 fetch/DWD） |
```

---

### Task 6: 全量测试与 M4 续跑（可选运维）

**Files:** 无代码变更

- [ ] **Step 1: 全量 pytest**

```bash
pytest tests/ -v
```

Expected: 全部 PASS

- [ ] **Step 2: M4 calc 续跑（实库）**

```bash
PYTHONUNBUFFERED=1 python3 -m backend.cli calc --date 20260610
```

Expected：
- 无 `KeyError: pct_chg`
- 12 指标 batch_compute 顺序完成
- 日志 `calc ALL DONE`
- kpattern 可能 `FULL` 一轮（`SIGNATURE_COLS` 增列导致 fp 变）— 属预期

- [ ] **Step 3: 可选 benchmark 签字**

```bash
PYTHONUNBUFFERED=1 python3 scripts/benchmark_run.py --date 20260610 --run --skip-export
python3 scripts/health_check.py
```

PASS 标准见附录 B：`elapsed≤1800s` + `chunk_stocks<400` + health_check 无 CRITICAL。

- [ ] **Step 4: Commit（用户明确要求时）**

```bash
git add backend/etl/calc_indicators.py backend/etl/calc_kpattern.py \
        tests/test_etl/test_calc_indicators.py tests/test_etl/test_batch_append_calc.py \
        CLAUDE.md docs/superpowers/plans/
git commit -m "$(cat <<'EOF'
fix: align batch quote tail columns with pipeline for kpattern pct_chg

Batch append crashed on KeyError because quote_tail_columns omitted pct_chg
required for limit-up/down filtering. Unify tail and pipeline column sets and
add contract tests to prevent drift.
EOF
)"
```

---

## Self-Review

| 检查项 | 结果 |
|--------|------|
| Spec 覆盖：batch/pipeline 列一致 | Task 2 |
| Spec 覆盖：kpattern 涨跌停过滤 | Task 3 + Task 4 |
| Spec 覆盖：SIGNATURE 路由完整性 | Task 2 + Task 3 |
| 无 TBD/占位符 | ✅ |
| 类型/函数名一致 | `quote_tail_columns` / `quote_pipeline_columns` 全计划统一 |
| 注册表（orchestrator/schema/export） | 无需改动 |

## 运维说明

- **kpattern 一次性 FULL：** 存量 `dws_calc_state` 中 kpattern `history_fp` 会变；安全方向，符合 append-only 设计。
- **已写入的 MACD/MA（M4 第 4 轮）：** INSERT-only 快照，续跑不重复污染。
- **20% 涨跌停板：** 既有 `non_st_limit=9.9` 局限，本计划不处理。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-12-kpattern-batch-tail-columns.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每 Task 派生子 agent，任务间 review

**2. Inline Execution** — 本会话按 Task 1→6 顺序实施，检查点汇报

Which approach?
