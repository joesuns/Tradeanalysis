# CalcPreflightContext Partial Merge (M1.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 `run_calc` 内 `auto_fetch` / `stale_dwd` 二次 DWD rebuild 后对 **整包** `preflight_ctx` 的粗粒度作废，改为 **patch 子集 merge**，真新日保留 M1 热路径（省 ~200s tails+preflight）。

**Architecture:** Step1 `cli run` 经 `set_run_preflight_context` 注入全市场 refresh 产物；`run_calc` 若仅 rebuild 子集（如 547 股），对该子集 `refresh_calc_state_fingerprints(return_artifacts=True)` 后 **`merge_context_patch`** 覆盖 patch 股的 tails/modes/fp/state_map，非 patch 股保留原 ctx。无 ctx 时行为与冷路径一致。Supersedes M1 plan L446 的 `preflight_ctx=None` 粗粒度 invalidate。

**Tech Stack:** Python 3.9、DuckDB、pandas、pytest；`CALC_REUSE_REFRESH_CTX=1`（默认）、`DWD_REBUILD_REFRESH_STATE=1`（默认）。

**父计划：** `2026-06-13-calc-preflight-context-p0.md`（M1）、`2026-06-09-pipeline-30min-optimization.md`

**禁止：** 与 M2d 同 PR；简单删除 `preflight_ctx=None` 而不 merge（会导致 patch 股脏 tails）。

---

## 实库基线（优化前，20260612 真新日）

| 桶 | 观测 |
|----|------|
| Step1 refresh_state | 173s / 5508 股 |
| calc auto_fetch + rebuild | 547 股 |
| batch_append | `preflight_source=cold`（应避免） |
| batch tails + preflight | ~203s |

**M1.1-KPI：** `preflight_source=refresh`；`preflight_elapsed_sec < 30s`；`tails_load_skipped=true`。

---

## File Map

| 文件 | 职责 |
|------|------|
| `backend/etl/calc_preflight_context.py` | 新增 `merge_context_patch` |
| `backend/etl/orchestrator.py` | `_merge_preflight_after_dwd_rebuild`；替换两处 `preflight_ctx=None` |
| `tests/test_etl/test_calc_preflight_context.py` | merge 单元测试 |
| `tests/test_etl/test_orchestrator.py` | auto_fetch 后 ctx 仍非 None |
| `tests/test_etl/test_batch_append_calc.py` | 混部热路径（patch + 非 patch） |
| `docs/superpowers/plans/2026-06-13-calc-preflight-context-p0.md` | 注明 L446 superseded |
| `CLAUDE.md` | M1.1 一行说明 |

---

### Task 1: `merge_context_patch` 单元测试

**Files:**
- Modify: `tests/test_etl/test_calc_preflight_context.py`
- Modify: `backend/etl/calc_preflight_context.py`

- [ ] **Step 1: Write the failing test**

```python
def test_merge_context_patch_overwrites_patch_codes_only():
    from backend.etl.calc_preflight_context import (
        CalcPreflightContext,
        build_context_from_refresh,
        merge_context_patch,
    )

    base = CalcPreflightContext(
        calc_date="20260612",
        source="refresh_state",
        stale_codes=["A.SZ", "B.SZ"],
        state_map={},
        daily_tails={"A.SZ": "old_a", "B.SZ": "old_b"},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={"A.SZ": {("macd", "daily"): ("SKIP", [])}},
        fp_cache_by_stock={"A.SZ": {("macd", "daily"): "fp_a"}},
    )
    patch_bundle = {
        "daily_tails": {"A.SZ": "new_a"},
        "weekly_tails": {"A.SZ": "w_a"},
        "dde_daily": {},
        "dde_weekly": {},
        "stock_modes": {"A.SZ": {("macd", "daily"): ("APPEND", ["20260612"])}},
        "fp_cache_by_stock": {"A.SZ": {("macd", "daily"): "fp_new"}},
        "state_map": {("A.SZ", "macd", "daily"): {"last_trade_date": "20260612"}},
    }
    merged = merge_context_patch(base, ["A.SZ"], patch_bundle)
    assert merged.daily_tails["A.SZ"] == "new_a"
    assert merged.daily_tails["B.SZ"] == "old_b"
    assert merged.stock_modes["A.SZ"][("macd", "daily")][0] == "APPEND"
    assert merged.stock_modes["B.SZ"][("macd", "daily")][0] == "SKIP"
    assert merged.source == "refresh_state"
    assert merged.calc_date == "20260612"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_calc_preflight_context.py::test_merge_context_patch_overwrites_patch_codes_only -v`  
Expected: FAIL `ImportError: cannot import name 'merge_context_patch'`

- [ ] **Step 3: Implement `merge_context_patch`**

Add to `backend/etl/calc_preflight_context.py`:

```python
def merge_context_patch(
    ctx: CalcPreflightContext,
    patch_codes: List[str],
    patch_bundle: Dict[str, Any],
) -> CalcPreflightContext:
    """Merge refresh artifacts for patch_codes into existing ctx (in-place copy)."""
    if ctx is None:
        return build_context_from_refresh(
            calc_date=patch_bundle.get("calc_date", ""),
            stale_codes=list(patch_codes),
            summary=patch_bundle.get("summary", {}),
            state_map=patch_bundle.get("state_map", {}),
            tails_bundle=patch_bundle,
        )
    patch_set = set(patch_codes)
    daily_tails = dict(ctx.daily_tails)
    weekly_tails = dict(ctx.weekly_tails)
    dde_daily = dict(ctx.dde_daily)
    dde_weekly = dict(ctx.dde_weekly)
    stock_modes = dict(ctx.stock_modes)
    fp_cache = dict(ctx.fp_cache_by_stock)
    state_map = dict(ctx.state_map)

    for c in patch_codes:
        if c in patch_bundle.get("daily_tails", {}):
            daily_tails[c] = patch_bundle["daily_tails"][c]
        if c in patch_bundle.get("weekly_tails", {}):
            weekly_tails[c] = patch_bundle["weekly_tails"][c]
        if c in patch_bundle.get("dde_daily", {}):
            dde_daily[c] = patch_bundle["dde_daily"][c]
        if c in patch_bundle.get("dde_weekly", {}):
            dde_weekly[c] = patch_bundle["dde_weekly"][c]
        if c in patch_bundle.get("stock_modes", {}):
            stock_modes[c] = patch_bundle["stock_modes"][c]
        if c in patch_bundle.get("fp_cache_by_stock", {}):
            fp_cache[c] = patch_bundle["fp_cache_by_stock"][c]

    for k, v in patch_bundle.get("state_map", {}).items():
        if k[0] in patch_set:
            state_map[k] = v

    stale = list(dict.fromkeys(list(ctx.stale_codes) + list(patch_codes)))
    summary = dict(ctx.refresh_summary)
    summary["merged_patch"] = list(patch_codes)

    return CalcPreflightContext(
        calc_date=ctx.calc_date,
        source="refresh_state",
        stale_codes=stale,
        state_map=state_map,
        daily_tails=daily_tails,
        weekly_tails=weekly_tails,
        dde_daily=dde_daily,
        dde_weekly=dde_weekly,
        stock_modes=stock_modes,
        fp_cache_by_stock=fp_cache,
        refresh_summary=summary,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_etl/test_calc_preflight_context.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_preflight_context.py tests/test_etl/test_calc_preflight_context.py
git commit -m "feat(calc): merge_context_patch for preflight ctx partial refresh"
```

---

### Task 2: Orchestrator merge helper

**Files:**
- Modify: `backend/etl/orchestrator.py`

- [ ] **Step 1: Write failing orchestrator test**

Add to `tests/test_etl/test_orchestrator.py`:

```python
def test_run_calc_auto_fetch_merges_preflight_ctx(monkeypatch):
    """auto_fetch DWD rebuild must merge ctx, not set preflight_ctx=None."""
    import duckdb
    from backend.db.schema import create_all_tables, ensure_calc_state_table
    from backend.etl.calc_preflight_context import CalcPreflightContext, set_run_preflight_context

    captured = {"preflight_ctx": None, "batch_preflight_source": None}

    base_ctx = CalcPreflightContext(
        calc_date="20260612",
        source="refresh_state",
        stale_codes=["KEEP.SZ"],
        state_map={},
        daily_tails={"KEEP.SZ": "keep_tail"},
        weekly_tails={},
        dde_daily={},
        dde_weekly={},
        stock_modes={"KEEP.SZ": {("macd", "daily"): ("SKIP", [])}},
        fp_cache_by_stock={"KEEP.SZ": {("macd", "daily"): "fp_keep"}},
    )
    set_run_preflight_context(base_ctx)

    def fake_refresh(con, ts_codes, calc_date, dwd_result, return_artifacts=False):
        if return_artifacts:
            bundle = {
                "daily_tails": {"PATCH.SZ": "patch_tail"},
                "weekly_tails": {},
                "dde_daily": {},
                "dde_weekly": {},
                "stock_modes": {"PATCH.SZ": {("macd", "daily"): ("APPEND", ["20260612"])}},
                "fp_cache_by_stock": {"PATCH.SZ": {("macd", "daily"): "fp_patch"}},
                "state_map": {},
            }
            return {"stocks": 1, "records_written": 0}, bundle
        return {"stocks": 1, "records_written": 0}

    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.maybe_refresh_state_after_dwd_rebuild",
        fake_refresh,
    )
    # ... stub completeness, auto_fetch n_fetched>0, run_calc until batch_append capture
    # Assert captured preflight_ctx is not None and KEEP.SZ tails preserved
```

（实施时按现有 `test_orchestrator` mock 风格补全 stub；核心断言：`preflight_ctx is not None` 且 `KEEP.SZ` 仍在 `daily_tails`。）

- [ ] **Step 2: Run test — expect FAIL**（当前 `preflight_ctx=None`）

Run: `python3 -m pytest tests/test_etl/test_orchestrator.py::test_run_calc_auto_fetch_merges_preflight_ctx -v`  
Expected: FAIL

- [ ] **Step 3: Add `_merge_preflight_after_dwd_rebuild` in orchestrator.py**

Replace `_refresh_state_after_dwd_rebuild` with merge-capable version:

```python
def _merge_preflight_after_dwd_rebuild(
    con,
    ts_codes: list,
    calc_date: str,
    dwd_result: dict,
    preflight_ctx,
):
    from backend.config import CALC_REUSE_REFRESH_CTX
    from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild
    from backend.etl.calc_preflight_context import merge_context_patch

    if not CALC_REUSE_REFRESH_CTX:
        return None
    result = maybe_refresh_state_after_dwd_rebuild(
        con, ts_codes, calc_date, dwd_result, return_artifacts=True,
    )
    if not result:
        return preflight_ctx
    summary, tails_bundle = result if isinstance(result, tuple) else (result, None)
    if tails_bundle is None:
        return preflight_ctx
    logger.info(
        "refresh_state merge: patch_stocks=%d ctx_before=%s",
        len(ts_codes), "set" if preflight_ctx else "none",
    )
    return merge_context_patch(preflight_ctx, ts_codes, tails_bundle)
```

- [ ] **Step 4: Replace two `preflight_ctx = None` sites**

In `run_calc` auto_fetch block (~1515–1518):

```python
                preflight_ctx = _merge_preflight_after_dwd_rebuild(
                    con, fetched_codes, calc_date, dwd_result or {}, preflight_ctx,
                )
```

In stale_dwd block (~1549–1552): same pattern with `stale_dwd`.

- [ ] **Step 5: Run orchestrator test — expect PASS**

- [ ] **Step 6: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "perf(calc): merge preflight ctx after calc auto_fetch DWD rebuild"
```

---

### Task 3: Batch append 混部热路径测试

**Files:**
- Modify: `tests/test_etl/test_batch_append_calc.py`

- [ ] **Step 1: Write test**

```python
def test_batch_append_hot_path_after_partial_ctx_merge(monkeypatch):
    """ctx covers KEEP; PATCH merged in — only PATCH may hit cold merge."""
    # Extend _make_minimal_preflight_ctx: codes=["KEEP.SZ","PATCH.SZ"]
    # stock_modes only KEEP; PATCH absent → _merge_cold_tails_and_preflight fills PATCH
    # Assert preflight_source==refresh, quote_tails call count == 0 or only for PATCH path
```

- [ ] **Step 2–4: Implement minimal mock + run**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py::test_batch_append_hot_path_after_partial_ctx_merge -v`

- [ ] **Step 5: Commit**

```bash
git add tests/test_etl/test_batch_append_calc.py
git commit -m "test(calc): batch append hot path with partial ctx merge"
```

---

### Task 4: 文档与收尾

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/plans/2026-06-13-calc-preflight-context-p0.md`（L446 加注 superseded by M1.1）
- Modify: `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md`（附录 M1.1 签字槽）

- [ ] **Step 1: CLAUDE.md** — calc 段增加：auto_fetch 后 `merge_context_patch`，非整包 cold。

- [ ] **Step 2: 全量 pytest**

Run: `python3 -m pytest tests/ -v`  
Expected: PASS

- [ ] **Step 3: Commit docs**

```bash
git add CLAUDE.md docs/superpowers/plans/
git commit -m "docs: M1.1 preflight partial merge runbook"
```

---

## 验收合同（M1.1）

| ID | 门槛 |
|----|------|
| E1 | `pytest tests/ -v` 全绿 |
| E4 | 真新日含 auto_fetch 时 `preflight_source=refresh` |
| E4 | `preflight_elapsed_sec < 30s`（对比 20260612 冷路径 ~144s） |

**范围外：** M2d；`batch_compute_sec_by_key` 观测（可选后续 PR）。

---

## Execution Handoff

**Plan saved to `docs/superpowers/plans/2026-06-13-calc-preflight-merge-m1.1.md`.**

**合入顺序：** M1.1 **先于** `2026-06-13-calc-macd-b4-weekly-m2d.md`。

**Two execution options:**

1. **Subagent-Driven (recommended)** — 每 Task 派 subagent + 两阶段 review  
2. **Inline Execution** — 本会话按 Task 执行，检查点审批

**Which approach?**
