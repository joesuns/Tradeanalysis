# CalcPreflightContext（M1 P0）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除真新日 `cli run` 中 refresh_state 与 batch_append 之间的 **tail 双载 + preflight 双算 + APPEND 逐股 state UPSERT**，在 **零指标语义变更** 前提下节省 **~8–10min** 墙钟，并为 M2 向量化保留冷/热双路径。

**Architecture:** 引入 `CalcPreflightContext` 作为 `run → calc` 唯一接线契约。`refresh_calc_state_fingerprints` 在 DWD rebuild 后产出 tails + `stock_modes` + `fp_cache`；`cli run` 经进程内 `set_run_preflight_context` 传递给 `run_calc`；`run_batch_append_phase` 热路径跳过 `batch_tails`（~64s）与 `batch_preflight`（~189s），APPEND 后改用 `upsert_calc_state_batch`。独立 `cli calc` 走冷路径（`preflight_ctx=None`），行为与当前一致。

**Tech Stack:** Python 3.9、DuckDB、pandas、pytest；沿用 `SIG_WINDOW=245`、`DWD_REBUILD_REFRESH_STATE=1`、`upsert_calc_state_batch`。

**实库基线（run_id `0fd66428`，20260611 真新日）：**

| 桶 | 耗时 | M1 目标 |
|----|------|---------|
| refresh_state | 344s | 保留（必要 fp 对齐） |
| batch_tails | 64s | **<5s**（复用 ctx） |
| batch_preflight | 189s | **0s**（复用 ctx modes） |
| batch_state 逐股 | 345s | **<30s**（批写） |
| batch_compute | 2925s | 不变（M2） |
| **E2E** | **68min** | **~58min** |

**父计划：** `docs/superpowers/plans/2026-06-09-calc-fundamental-performance.md`（M1）；`docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md`（附录 B 基线）

**用户审批：** 2026-06-13（数据/量化/系统架构师整合评估「可以」）

**验收合同（M1 范围）：**

| 级别 | 内容 | 门槛 |
|------|------|------|
| L1 | `pytest tests/ -v` | 全绿 |
| L2 | `test_append_calc.py` | `atol=1e-9` 不变 |
| L6 | 路由不变量 | 真新日 `chunk=0`、append 计数同量级 |
| M1-KPI | 热路径日志 | `preflight_source=refresh`；`batch_tails`+`batch_preflight` 合计 **<15s** |

**范围外（M2 另立项）：** 跨股向量化、`volume_trend_v2` 递推、物化尾窗表。

---

## File Map

| 文件 | 职责 |
|------|------|
| `backend/etl/calc_preflight_context.py` | **新建** — `CalcPreflightContext` + `set/pop_run_preflight_context` |
| `backend/config.py` | `CALC_REUSE_REFRESH_CTX=1`（默认） |
| `backend/etl/calc_state_refresh.py` | refresh 产出 modes/fp_cache/tails；summary 扩展 |
| `backend/etl/calc_batch_append.py` | 热/冷双路径；APPEND state 批写 |
| `backend/etl/orchestrator.py` | `run_calc(..., preflight_ctx=...)`；观测字段 |
| `backend/cli.py` | run Step1 设 ctx；`cmd_calc` pop ctx |
| `tests/test_etl/test_calc_preflight_context.py` | **新建** |
| `tests/test_etl/test_calc_state_refresh.py` | ctx 产物 + 股集切片 |
| `tests/test_etl/test_batch_append_calc.py` | 热路径回归 |
| `CLAUDE.md` | CalcPreflightContext 说明 |
| `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md` | 附录 B 0fd66428 基线 |
| `docs/superpowers/plans/2026-06-09-calc-fundamental-performance.md` | M1 状态 + L1–L6 附录 |

---

### Task 1: `CalcPreflightContext` 数据结构与进程传递

**Files:**
- Create: `backend/etl/calc_preflight_context.py`
- Create: `tests/test_etl/test_calc_preflight_context.py`
- Modify: `backend/config.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_etl/test_calc_preflight_context.py
from backend.etl.calc_preflight_context import (
    CalcPreflightContext,
    set_run_preflight_context,
    pop_run_preflight_context,
)


def test_pop_returns_none_when_unset():
    assert pop_run_preflight_context() is None


def test_set_and_pop_roundtrip():
    ctx = CalcPreflightContext(
        calc_date="20260611",
        source="refresh_state",
        stale_codes=["000001.SZ"],
        state_map={},
        daily_tails={"000001.SZ": None},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={"000001.SZ": {("macd", "daily"): ("APPEND", ["20260611"])}},
        fp_cache_by_stock={"000001.SZ": {("macd", "daily"): "abc123"}},
        refresh_summary={"keys_updated": 1},
    )
    set_run_preflight_context(ctx)
    got = pop_run_preflight_context()
    assert got is ctx
    assert pop_run_preflight_context() is None


def test_slice_for_calc_codes():
    from backend.etl.calc_preflight_context import slice_context_for_codes
    ctx = CalcPreflightContext(
        calc_date="20260611",
        source="refresh_state",
        stale_codes=["A.SZ", "B.SZ"],
        state_map={
            ("A.SZ", "daily", "macd"): {"last_trade_date": "20260610"},
            ("B.SZ", "daily", "macd"): {"last_trade_date": "20260610"},
        },
        daily_tails={"A.SZ": 1, "B.SZ": 2},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={
            "A.SZ": {("macd", "daily"): ("APPEND", ["20260611"])},
            "B.SZ": {("macd", "daily"): ("APPEND", ["20260611"])},
        },
        fp_cache_by_stock={
            "A.SZ": {("macd", "daily"): "fp_a"},
            "B.SZ": {("macd", "daily"): "fp_b"},
        },
        refresh_summary={},
    )
    sliced = slice_context_for_codes(ctx, ["A.SZ"])
    assert list(sliced.daily_tails.keys()) == ["A.SZ"]
    assert "B.SZ" not in sliced.stock_modes
    assert ("A.SZ", "daily", "macd") in sliced.state_map
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_calc_preflight_context.py -v`  
Expected: FAIL — `ModuleNotFoundError: calc_preflight_context`

- [x] **Step 3: Implement minimal module**

```python
# backend/etl/calc_preflight_context.py
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

Key = Tuple[str, str]  # (indicator_name, freq)
ModeEntry = Tuple[str, list]  # (mode, new_bars)

_RUN_CTX: Optional["CalcPreflightContext"] = None


@dataclass
class CalcPreflightContext:
    calc_date: str
    source: str  # "refresh_state" | "cold"
    stale_codes: List[str]
    state_map: Dict[Tuple[str, str, str], dict]
    daily_tails: dict
    weekly_tails: dict
    dde_daily: dict
    dde_weekly: dict
    stock_modes: Dict[str, Dict[Key, ModeEntry]]
    fp_cache_by_stock: Dict[str, Dict[Key, str]]
    refresh_summary: Dict[str, Any] = field(default_factory=dict)


def set_run_preflight_context(ctx: CalcPreflightContext) -> None:
    global _RUN_CTX
    _RUN_CTX = ctx


def pop_run_preflight_context() -> Optional[CalcPreflightContext]:
    global _RUN_CTX
    ctx = _RUN_CTX
    _RUN_CTX = None
    return ctx


def slice_context_for_codes(
    ctx: CalcPreflightContext,
    calc_codes: List[str],
) -> CalcPreflightContext:
    code_set = set(calc_codes)
    return CalcPreflightContext(
        calc_date=ctx.calc_date,
        source=ctx.source,
        stale_codes=[c for c in ctx.stale_codes if c in code_set],
        state_map={
            k: v for k, v in ctx.state_map.items() if k[0] in code_set
        },
        daily_tails={c: ctx.daily_tails[c] for c in calc_codes if c in ctx.daily_tails},
        weekly_tails={c: ctx.weekly_tails[c] for c in calc_codes if c in ctx.weekly_tails},
        dde_daily={c: ctx.dde_daily[c] for c in calc_codes if c in ctx.dde_daily},
        dde_weekly={c: ctx.dde_weekly[c] for c in calc_codes if c in ctx.dde_weekly},
        stock_modes={c: ctx.stock_modes[c] for c in calc_codes if c in ctx.stock_modes},
        fp_cache_by_stock={
            c: ctx.fp_cache_by_stock[c] for c in calc_codes if c in ctx.fp_cache_by_stock
        },
        refresh_summary=dict(ctx.refresh_summary),
    )
```

`backend/config.py` 追加：

```python
# CALC_REUSE_REFRESH_CTX: cli run passes refresh tails+modes into calc (skip batch reload)
CALC_REUSE_REFRESH_CTX = os.getenv("CALC_REUSE_REFRESH_CTX", "1").strip() != "0"
```

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_etl/test_calc_preflight_context.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_preflight_context.py backend/config.py tests/test_etl/test_calc_preflight_context.py
git commit -m "feat: add CalcPreflightContext for run-to-calc handoff"
```

---

### Task 2: `refresh_calc_state_fingerprints` 产出 modes + fp_cache

**Files:**
- Modify: `backend/etl/calc_state_refresh.py`
- Modify: `tests/test_etl/test_calc_state_refresh.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_etl/test_calc_state_refresh.py (append)
def test_refresh_builds_preflight_context(monkeypatch):
    """After refresh, CalcPreflightContext has stock_modes and fp_cache."""
    from backend.etl import calc_state_refresh as mod
    from backend.etl.calc_preflight_context import build_context_from_refresh

    # ... reuse fake_quote_tails / fake_dde_tails from test_refresh_updates_stale_fingerprint
    summary, tails_bundle = refresh_calc_state_fingerprints(
        con, ["SA.SZ"], "20260609", dry_run=False, return_artifacts=True,
    )
    ctx = build_context_from_refresh(
        calc_date="20260609",
        stale_codes=["SA.SZ"],
        summary=summary,
        state_map=load_calc_state_batch(con, ["SA.SZ"]),
        tails_bundle=tails_bundle,
        stock_modes=tails_bundle["stock_modes"],
        fp_cache_by_stock=tails_bundle["fp_cache_by_stock"],
    )
    assert ctx.source == "refresh_state"
    assert "SA.SZ" in ctx.stock_modes
    assert ("macd", "daily") in ctx.stock_modes["SA.SZ"]
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_calc_state_refresh.py::test_refresh_builds_preflight_context -v`  
Expected: FAIL — `return_artifacts` / `build_context_from_refresh` 不存在

- [x] **Step 3: Implement — 合并尾部 preflight 为产物**

在 `refresh_calc_state_fingerprints` 尾部（现有 `preflight_stock_modes_v2` 循环）改为 `preflight_stock_modes_with_fps`，填充：

```python
stock_modes: Dict[str, Dict[Key, ModeEntry]] = {}
fp_cache_by_stock: Dict[str, Dict[Key, str]] = {}
for ts_code in ts_codes:
    modes, fps = preflight_stock_modes_with_fps(
        ts_code, fresh_state,
        daily_tails.get(ts_code), weekly_tails.get(ts_code),
        dde_daily.get(ts_code), dde_weekly.get(ts_code),
    )
    if modes is None:
        chunk_stocks.add(ts_code)
        continue
    stock_modes[ts_code] = modes
    fp_cache_by_stock[ts_code] = fps
```

新增参数 `return_artifacts: bool = False`。为 True 时额外返回：

```python
tails_bundle = {
    "daily_tails": daily_tails,
    "weekly_tails": weekly_tails,
    "dde_daily": dde_daily,
    "dde_weekly": dde_weekly,
    "stock_modes": stock_modes,
    "fp_cache_by_stock": fp_cache_by_stock,
}
return summary, tails_bundle
```

在 `calc_preflight_context.py` 增加 `build_context_from_refresh(...)` 工厂函数。

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_etl/test_calc_state_refresh.py -v`  
Expected: PASS（含既有用例）

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_state_refresh.py backend/etl/calc_preflight_context.py tests/test_etl/test_calc_state_refresh.py
git commit -m "feat: refresh_state returns preflight modes and tail artifacts"
```

---

### Task 3: `cli run` → `cmd_calc` 传递 Context

**Files:**
- Modify: `backend/cli.py`
- Modify: `backend/etl/orchestrator.py`（`run_calc` 签名）

- [x] **Step 1: Write the failing test**

```python
# tests/test_cli.py (append)
def test_cmd_run_sets_preflight_context(monkeypatch):
    """After run rebuild+refresh, calc receives popped context when CALC_REUSE_REFRESH_CTX=1."""
    captured = {}

    def fake_run_calc(con, **kwargs):
        captured["preflight_ctx"] = kwargs.get("preflight_ctx")

    monkeypatch.setattr("backend.etl.orchestrator.run_calc", fake_run_calc)
    monkeypatch.setattr("backend.config.CALC_REUSE_REFRESH_CTX", True)
    # ... minimal mock fetch/rebuild/refresh returning artifacts
    # assert captured["preflight_ctx"] is not None
    # assert captured["preflight_ctx"].source == "refresh_state"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cli.py::test_cmd_run_sets_preflight_context -v`  
Expected: FAIL

- [ ] **Step 3: Wire cli.run**

`cli.py` Step 1 refresh 后：

```python
from backend.config import CALC_REUSE_REFRESH_CTX
from backend.etl.calc_preflight_context import (
    build_context_from_refresh,
    set_run_preflight_context,
)

if CALC_REUSE_REFRESH_CTX and refresh_summary:
    summary, tails_bundle = maybe_refresh_state_after_dwd_rebuild(
        con, stale_rebuilt, date, dwd_result, return_artifacts=True,
    )
    ctx = build_context_from_refresh(...)
    set_run_preflight_context(ctx)
```

`cmd_calc` 开头：

```python
from backend.etl.calc_preflight_context import pop_run_preflight_context
preflight_ctx = pop_run_preflight_context()
run_calc(..., preflight_ctx=preflight_ctx)
```

`run_calc` 签名增加 `preflight_ctx=None`；传入 `run_batch_append_phase(con, codes, calc_date, force, preflight_ctx=preflight_ctx)`。

`maybe_refresh_state_after_dwd_rebuild` 透传 `return_artifacts` 到 `refresh_calc_state_fingerprints`。

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_cli.py -v -k preflight`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/cli.py backend/etl/orchestrator.py backend/etl/calc_state_refresh.py tests/test_cli.py
git commit -m "feat: cli run passes CalcPreflightContext into calc"
```

---

### Task 4: `run_batch_append_phase` 热路径跳过 tails + preflight

**Files:**
- Modify: `backend/etl/calc_batch_append.py`
- Modify: `tests/test_etl/test_batch_append_calc.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_etl/test_batch_append_calc.py (append)
def test_batch_append_reuses_preflight_ctx(monkeypatch, memory_con):
    """When preflight_ctx provided, batch_load_quote_tails must not be called."""
    calls = {"quote_tails": 0}

    def counting_quote_tails(*args, **kwargs):
        calls["quote_tails"] += 1
        return {}

    monkeypatch.setattr(
        "backend.etl.calc_fast_skip.batch_load_quote_tails",
        counting_quote_tails,
    )
    ctx = _make_minimal_preflight_ctx()  # helper in test file
    run_batch_append_phase(memory_con, ["000001.SZ"], "20260611", preflight_ctx=ctx)
    assert calls["quote_tails"] == 0
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py::test_batch_append_reuses_preflight_ctx -v`  
Expected: FAIL — `batch_load_quote_tails` 仍被调用

- [x] **Step 3: Implement hot path**

`run_batch_append_phase` 签名增加 `preflight_ctx: Optional[CalcPreflightContext] = None`。

```python
from backend.config import CALC_REUSE_REFRESH_CTX
from backend.etl.calc_preflight_context import slice_context_for_codes

if (
    CALC_REUSE_REFRESH_CTX
    and preflight_ctx is not None
    and preflight_ctx.source == "refresh_state"
):
    sliced = slice_context_for_codes(preflight_ctx, codes)
    state_map = sliced.state_map
    daily_tails = sliced.daily_tails
    weekly_tails = sliced.weekly_tails
    dde_daily = sliced.dde_daily
    dde_weekly = sliced.dde_weekly
    stock_modes = sliced.stock_modes
    fp_cache_by_stock = sliced.fp_cache_by_stock
    logger.info(
        "progress calc.batch_append: 热路径 | preflight_source=refresh | %d股",
        len(codes),
    )
else:
    # 现有冷路径：batch_tails + preflight 循环
    ...
```

热路径 **跳过** `batch_preflight` 循环与 `build_skip_state_records` 前的 modes 构建；仍执行 SKIP 的 `upsert_calc_state_batch`（用 ctx 的 fp_cache）。

G3/auto-fetch 后若发生 **二次 DWD rebuild**，~~须在 orchestrator 调用 `invalidate`：`pop_run_preflight_context()` 或置 `preflight_ctx=None`（ctx 已失效）~~ **已由 M1.1 取代**（`merge_context_patch` + `_merge_preflight_after_dwd_rebuild`，见 `2026-06-13-calc-preflight-merge-m1.1.md`）。

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_append.py tests/test_etl/test_batch_append_calc.py
git commit -m "perf: batch append hot path reuses refresh preflight context"
```

---

### Task 5: APPEND 后 `upsert_calc_state_batch` 替代逐股 UPSERT

**Files:**
- Modify: `backend/etl/calc_batch_append.py`
- Modify: `backend/etl/calc_state.py`（可选 helper）
- Modify: `tests/test_etl/test_batch_append_calc.py`

- [x] **Step 1: Write the failing test**

```python
def test_batch_append_state_uses_batch_upsert(monkeypatch, memory_con):
    upsert_calls = {"n": 0, "records": 0}

    def counting_upsert(con, records):
        upsert_calls["n"] += 1
        upsert_calls["records"] += len(records)
        return len(records)

    monkeypatch.setattr(
        "backend.etl.calc_state.upsert_calc_state_batch",
        counting_upsert,
    )
    monkeypatch.setattr(
        "backend.etl.calc_state.write_calc_state_from_df",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("per-stock write forbidden")),
    )
    # run minimal APPEND batch for one indicator
    assert upsert_calls["n"] >= 1
    assert upsert_calls["records"] >= 1
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py::test_batch_append_state_uses_batch_upsert -v`  
Expected: FAIL — 仍调用 `write_calc_state_from_df`

- [x] **Step 3: Implement batch state records from compute fp**

在各 `batch_append_*` 返回前，`stock_rows` 已含 `(ts_code, out, fp, write_start, write_end)`。在 `run_batch_append_phase` APPEND 循环末尾：

```python
def build_append_state_records(
    append_codes, indicator_name, freq, stock_rows, calc_date, spec_version,
):
    records = []
    for ts_code, out, fp, w0, w1 in stock_rows:
        anchor = str(out["trade_date"].max())
        records.append((
            ts_code, freq, indicator_name, anchor, fp,
            calc_date, None, spec_version,
        ))
    return records
```

替换 `write_calc_state_from_df` 循环为单次 `upsert_calc_state_batch(con, records)`。

`run_batch_full_phase` 内同理（Task 5b 路径已有类似循环，一并改）。

- [x] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py tests/test_etl/test_append_calc.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_append.py tests/test_etl/test_batch_append_calc.py
git commit -m "perf: batch APPEND state refresh via upsert_calc_state_batch"
```

---

### Task 6: 观测字段与 `ods_etl_log`

**Files:**
- Modify: `backend/etl/orchestrator.py`
- Modify: `backend/cli.py`

- [ ] **Step 1: 扩展 `log_etl_end` data_completeness**

`run_calc` / `calc_dws` 写入：

```python
{
    "preflight_source": "refresh" | "cold",
    "tails_load_skipped": true | false,
    "preflight_elapsed_sec": 0.0,
    "state_upsert_mode": "batch",
}
```

- [ ] **Step 2: 日志行**

```
progress calc.batch_append: 热路径 | preflight_source=refresh | 5389股
progress calc.batch_append: 冷路径 | preflight_source=cold | 5389股
```

- [x] **Step 3: Run full pytest**

Run: `python3 -m pytest tests/ -v`  
Expected: 全绿（允许既有 skipped）

- [ ] **Step 4: Commit**

```bash
git add backend/etl/orchestrator.py backend/cli.py
git commit -m "chore: log preflight_source and batch state mode for SLA attribution"
```

---

### Task 7: 文档与 M1 签字

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md`（附录 B）
- Modify: `docs/superpowers/plans/2026-06-09-calc-fundamental-performance.md`

- [x] **Step 1: CLAUDE.md** — 增加 `CalcPreflightContext`、`CALC_REUSE_REFRESH_CTX`、热/冷路径说明

- [ ] **Step 2: 附录 B** — 记入 `0fd66428` 基线与 M1 KPI

- [ ] **Step 3: M1 实库签字（可选，有 ODS 新日时）**

```bash
python3 -m backend.cli run --date $ODS_MAX 2>&1 | tee /tmp/run_m1.log
grep -E 'preflight_source|batch_preflight|batch_tails|batch_append: 完成' /tmp/run_m1.log
python3 scripts/health_check.py
```

期望：热路径 `batch_tails`+`batch_preflight` <15s；E2E 较 68min 降 ~8–10min；`chunk=0`。

- [ ] **Step 4: Commit docs**

```bash
git add CLAUDE.md docs/superpowers/plans/
git commit -m "docs: M1 CalcPreflightContext plan and 0fd66428 baseline"
```

---

## 决策树合规自检

| 检查 | 结论 |
|------|------|
| 全库 rebuild | ❌ 未引入 |
| 弱签名 | ❌ 未改动 `SIG_WINDOW=245` |
| 跳过 refresh | ❌ `DWD_REBUILD_REFRESH_STATE` 保留 |
| 最小范围 | ✅ 仅编排/I/O；compute 公式不变 |
| 冷路径 | ✅ `cli calc` 独立可用 |

---

## Self-Review

| 项 | 状态 |
|----|------|
| Spec 覆盖（整合评估 M1 四条） | ✅ Task 1–7 |
| Placeholder 扫描 | ✅ 无 TBD |
| 类型一致 | ✅ `CalcPreflightContext` 全程统一 |
| M2 未混入本 plan | ✅ 范围外声明 |

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-13-calc-preflight-context-p0.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — 每 Task 派生子 agent + pytest + 架构师 review

**2. Inline Execution** — 本会话按 Task 1→7 顺序落地，Task 4/5 后跑全量 pytest

**Which approach?**

**M2（向量化）** 待 M1 签字后，按 `calc-fundamental-performance.md` 附录 **M2 Vector Append** 立项，**禁止与 M1 同 PR**。
