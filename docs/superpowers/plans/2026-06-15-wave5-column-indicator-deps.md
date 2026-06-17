# Wave 5 — 列→指标映射（run 路径）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 当 `cli run` 因 ODS 小范围列变更进入 calc 时，仅跑受影响的指标路由（最多 12 条中的子集），跳过无关 batch tail/preflight/append/full，在 **结果等价** 前提下缩短墙钟（典型：`circ_mv` / `net_amount_dc` 补丁 → 仅 `dde` 日+周）。

**Architecture:** Wave 1 的 `FetchResult` 扩展 **列级变更事件**；新建 `column_indicator_deps.py` 维护 `(ods_table, column) → {indicator}` 映射与保守 fallback 规则；`PipelineContext` 产出 `run_indicator_filter`；`run_calc` / `run_batch_append_phase` 复用 refresh 已有的 `resolve_refresh_routes(indicator_filter)` 收窄 `CALC_ROUTE_SPECS` 循环，并按需跳过 quote/dde tail SQL。**refresh R1 不受影响**（仍 12 路由或显式 `--indicator`）。

**Tech Stack:** Python 3.9+、DuckDB、pytest、现有 `CALC_ROUTE_SPECS` / `FetchResult` / `run_batch_append_phase`

**Parent plan:** [`2026-06-15-change-driven-refresh-cli.md`](2026-06-15-change-driven-refresh-cli.md) §4.2 二期 C / Wave 5 Task 7

**Branch:** `feat/change-driven-refresh`（stack PR #7 → `main`）

**执行模式：** Subagent-Driven（用户选定）；**每里程碑完成后** 系统架构师 + 数据架构师双 review → 用户验收 → 下一里程碑。

---

## 里程碑与双架构 Review 门禁

| 里程碑 | Tasks | 交付物 | Review 焦点 |
|--------|-------|--------|-------------|
| **M1** 数据契约 | 1–3 | 列级 `ods_diff` + `FetchResult.changed_field_events` + D2 patch 事件 | **数据架构：** 事件粒度、INSERT/adj 语义、patch 不漏报；**系统架构：** merge 契约、向后兼容 `int(FetchResult)` |
| **M2** 映射层 | 4 | `column_indicator_deps.py` + G1–G8 | **数据架构：** 映射表 vs SIGNATURE_COLS 一致性、fallback；**系统架构：** 纯函数、无 IO、可单测 |
| **M3** 管道集成 | 5–6 | run handoff + batch/calc 收窄 | **系统架构：** run/refresh 分叉、chunk worker、tail 跳过；**数据架构：** qfq/stale/spec 合并不误窄 |
| **M4** 验收 | 7–8 | 文档、smoke、全量 pytest | **双架构：** 观测字段、等价性、回退开关 |

### 每里程碑 Review 输出模板（必填）

```markdown
## M{N} 双架构 Review

### 数据架构师
- 契约符合度：✅/⚠️/❌
- 数据流完整性：
- 风险项：
- 验收结论：通过 / 有条件通过 / 不通过

### 系统架构师
- 模块边界与依赖：
- 性能/范围：
- 测试覆盖：
- 验收结论：通过 / 有条件通过 / 不通过

### 用户验收
- [ ] 回复「好/可以」进入 M{N+1}
```

**规则：** M{N} 双架构均为「通过」或「有条件通过（条件已记录）」且用户确认后，方可开始 M{N+1}。

---

## 设计约束（质量 > 速度）

### 收窄启用条件（全部满足才 narrow）

| # | 条件 | 原因 |
|---|------|------|
| G1 | `CALC_COLUMN_NARROW=1`（默认 1） | 功能开关 |
| G2 | `mode=run` 且 `force=False` | refresh/force 走全路由 |
| G3 | `fetch_result.rows_written > 0` | 无 mutation 已短路 |
| G4 | 变更事件 **不含** `adj_factor` | qfq 全历史漂移 → 全部 quote 指标 |
| G5 | 变更事件 **无** `ods_daily` INSERT（新 PK） | 新上市行保守全算 |
| G6 | DWD rebuild **无** qfq refresh（`find_stocks_needing_qfq_refresh` 为空） | qfq UPDATE 隐含多列变 |
| G7 | `stale_dwd \ changed_codes` 为空 | 结构性 stale 超出 fetch delta |
| G8 | `resolve_affected_indicators()` 非空且 **真子集** 6 指标 | 空集或全集 → 不 narrow |

**任一不满足 → `indicator_filter=None`（12 路由，现网行为）。**

### 与 spec stale 合并

```python
run_filter = union(column_affected, spec_stale_indicator_names)
# 若 run_filter == 全部 6 类 → 传 None（观测仍写 calc_routes_narrowed=false）
```

`spec_stale_indicator_names` 来自 `count_spec_stale_by_indicator` 的 key 前缀（`ma_daily` → `ma`）。

### INDICATOR_COLUMN_DEPS（定稿映射表）

依据各 Calculator `SIGNATURE_COLS`（`calc_indicators.py` / `calc_dde.py`）与 DWD 列来源：

| ODS 表 | 列 | 受影响 indicator |
|--------|-----|------------------|
| `ods_daily` | `open`, `high`, `low` | `kpattern` |
| `ods_daily` | `close` | `macd`, `ma`, `kpattern`, `volume`, `priceposition`, `dde` |
| `ods_daily` | `vol` | `kpattern`, `volume` |
| `ods_daily` | `pct_chg` | `kpattern` |
| `ods_daily` | `adj_factor` | **禁止 narrow**（G4） |
| `ods_daily` | `amount` | （无 SIGNATURE 依赖；单独变更 → 空集 → fallback 全路由） |
| `ods_daily_basic` | `circ_mv` | `dde` |
| `ods_daily_basic` | `total_mv`, `pe_ttm`, `turnover_rate`, `volume_ratio` | （export 用，无 DWS calc） |
| `ods_moneyflow` | 全部 `ODS_MONEYFLOW_DIFF_COLS` | `dde` |

`QUOTE_INDICATORS = frozenset({"macd", "ma", "kpattern", "volume", "priceposition"})`

---

## File Structure

| 文件 | 职责 |
|------|------|
| `backend/fetch/ods_diff.py` | 列级 diff + `PartitionResult` |
| `backend/fetch/fetch_result.py` | `ChangedFieldEvent` + merge |
| `backend/fetch/ods_daily.py` | 三表 diff + D2 patch 事件 |
| `backend/etl/column_indicator_deps.py` | **新建** 映射 + `resolve_affected_indicators` + `resolve_run_calc_indicator_filter` |
| `backend/etl/pipeline_context.py` | DWD meta + filter 解析 |
| `backend/etl/calc_preflight_context.py` | 可选 `RunCalcContext` handoff（indicator_filter + dwd_meta） |
| `backend/etl/calc_batch_append.py` | `indicator_filter` 收窄路由 + 条件 tail load |
| `backend/etl/calc_fast_skip.py` | preflight 已支持 `specs=` 参数，传过滤后 specs |
| `backend/etl/orchestrator.py` | `run_calc(..., indicator_filter=)` |
| `backend/cli.py` | run → calc handoff |
| `backend/config.py` | `CALC_COLUMN_NARROW` |
| `tests/test_fetch/test_ods_diff.py` | 列 diff golden |
| `tests/test_etl/test_column_indicator_deps.py` | **新建** 映射 + fallback |
| `tests/test_etl/test_batch_append_column_narrow.py` | **新建** batch 跳过路由 |
| `tests/test_cli/test_refresh_run.py` | run narrow 集成 |

---

### Task 1: 列级 ODS diff

**Files:**
- Modify: `backend/fetch/ods_diff.py`
- Test: `tests/test_fetch/test_ods_diff.py`

- [ ] **Step 1: Write the failing test**

```python
def test_partition_changed_rows_reports_column_names(con_with_ods):
    con, td = con_with_ods
    existing = {
        "ts_code": "000001.SZ", "trade_date": td,
        "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
        "vol": 1000.0, "amount": 5000.0, "pct_chg": 1.0, "adj_factor": 1.0,
    }
    con.execute("INSERT INTO ods_daily SELECT * FROM ...")  # use fixture helper
    incoming = dict(existing, vol=2000.0)
    changed, unchanged, events = partition_changed_rows_detailed(
        con, "ods_daily", ODS_DAILY_DIFF_COLS, [incoming], trade_date=td,
    )
    assert len(changed) == 1
    assert unchanged == 0
    assert ("000001.SZ", td, "ods_daily", "vol", False) in events


def test_insert_row_marks_is_insert_true(con_with_ods):
    # PK 不存在 → is_insert=True，events 含该表全部 diff_cols
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fetch/test_ods_diff.py::test_partition_changed_rows_reports_column_names -v`  
Expected: FAIL — `partition_changed_rows_detailed` not defined

- [ ] **Step 3: Implement `partition_changed_rows_detailed`**

```python
def diff_changed_columns(incoming: dict, existing: Optional[dict], cols: Sequence[str]) -> List[str]:
    if existing is None:
        return list(cols)
    return [c for c in cols if not values_equal(incoming.get(c), existing.get(c))]


def partition_changed_rows_detailed(...) -> Tuple[List[dict], int, List[Tuple[str, str, str, str, bool]]]:
    # returns (changed_rows, unchanged_count, events)
    # event = (ts_code, trade_date, table_name, column, is_insert)
```

- [ ] **Step 4: Keep backward compat wrappers**

```python
def partition_changed_daily(con, rows):
    changed, unchanged, _ = partition_changed_rows_detailed(
        con, "ods_daily", ODS_DAILY_DIFF_COLS, rows,
    )
    return changed, unchanged
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_fetch/test_ods_diff.py -v`  
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/fetch/ods_diff.py tests/test_fetch/test_ods_diff.py
git commit -m "feat(fetch): ODS diff returns per-column change events"
```

---

### Task 2: FetchResult 扩展 + 三表 write 路径

**Files:**
- Modify: `backend/fetch/fetch_result.py`
- Modify: `backend/fetch/ods_daily.py` (`_write_ods_*_diff`, `FetchResult.merge`)
- Test: `tests/test_fetch/test_ods_diff.py`

- [ ] **Step 1: Write failing test for merge dedupe**

```python
def test_fetch_result_merge_field_events():
    a = FetchResult(
        rows_written=1,
        changed_pairs=[("000001.SZ", "20260612")],
        changed_field_events=[("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False)],
    )
    b = FetchResult(
        rows_written=1,
        changed_pairs=[("000001.SZ", "20260612")],
        changed_field_events=[("000001.SZ", "20260612", "ods_moneyflow", "net_amount_dc", False)],
    )
    m = a.merge(b)
    assert len(m.changed_field_events) == 2
```

- [ ] **Step 2: Run test — expect FAIL**

- [ ] **Step 3: Add field to FetchResult**

```python
@dataclass
class FetchResult:
    ...
    changed_field_events: List[Tuple[str, str, str, str, bool]] = field(default_factory=list)

    def merge(self, other):
        # dedupe events by full tuple
        ...
```

- [ ] **Step 4: Wire `_write_ods_daily_diff` / basic / moneyflow**

```python
changed, unchanged, events = partition_changed_rows_detailed(...)
pairs = [(r["ts_code"], r["trade_date"]) for r in changed]
return FetchResult(..., changed_field_events=events)
```

- [ ] **Step 5: Run tests + commit**

```bash
git commit -m "feat(fetch): propagate ODS column events through FetchResult"
```

---

### Task 3: D2 — patch 路径写入列事件

**Files:**
- Modify: `backend/fetch/ods_daily.py` (`_apply_net_amount_dc_patch`, `_apply_circ_mv_patch`, callers)
- Test: `tests/test_fetch/test_ods_diff.py` or new `tests/test_fetch/test_ods_patch_events.py`

- [ ] **Step 1: Failing test — dc patch produces events**

```python
def test_net_amount_dc_patch_emits_field_events(con):
    # seed ods_moneyflow row with net_amount_dc NULL
    # call _apply_net_amount_dc_patch with dc value
    result = _apply_net_amount_dc_patch_as_fetch_result(con, df)
    assert result.rows_written == 1
    assert any(e[3] == "net_amount_dc" for e in result.changed_field_events)
```

- [ ] **Step 2: Refactor patch helpers to return `FetchResult`**

```python
def _apply_net_amount_dc_patch(con, df, register_name="_dc_patch") -> FetchResult:
    # after UPDATE, SELECT affected (ts_code, trade_date) pairs
    # emit (code, td, "ods_moneyflow", "net_amount_dc", False)
```

- [ ] **Step 3: Update `_backfill_net_amount_dc_*` / stock incremental `need_dc` branch**

Replace `FetchResult(rows_written=n_dc)` bare merge with patch `FetchResult` including events.

- [ ] **Step 4: Same for `_apply_circ_mv_patch` → `ods_daily_basic.circ_mv`**

- [ ] **Step 5: pytest + commit**

```bash
git commit -m "fix(fetch): dc/circ_mv patches emit column events for calc narrowing"
```

---

### Task 4: INDICATOR_COLUMN_DEPS 注册表

**Files:**
- Create: `backend/etl/column_indicator_deps.py`
- Test: `tests/test_etl/test_column_indicator_deps.py`

- [ ] **Step 1: Failing tests**

```python
from backend.etl.column_indicator_deps import resolve_affected_indicators, QUOTE_INDICATORS

def test_circ_mv_only_affects_dde():
    events = [("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False)]
    assert resolve_affected_indicators(events) == {"dde"}


def test_adj_factor_returns_none_for_narrow():
    events = [("000001.SZ", "20260612", "ods_daily", "adj_factor", False)]
    assert resolve_affected_indicators(events) is None  # conservative block


def test_daily_insert_returns_none():
    events = [("000001.SZ", "20260612", "ods_daily", "close", True)]
    assert resolve_affected_indicators(events) is None


def test_moneyflow_vol_column_affects_dde_only():
    events = [("000001.SZ", "20260612", "ods_moneyflow", "buy_lg_vol", False)]
    assert resolve_affected_indicators(events) == {"dde"}
```

- [ ] **Step 2: Implement mapping + resolver**

```python
ODS_COLUMN_TO_INDICATORS: Dict[Tuple[str, str], FrozenSet[str]] = {
    ("ods_daily", "open"): frozenset({"kpattern"}),
    ("ods_daily", "close"): frozenset({"macd", "ma", "kpattern", "volume", "priceposition", "dde"}),
    # ... full table from Design section ...
}

ALL_INDICATORS = frozenset({"macd", "ma", "kpattern", "volume", "priceposition", "dde"})

def resolve_affected_indicators(
    events: Sequence[Tuple[str, str, str, str, bool]],
) -> Optional[Set[str]]:
    if not events:
        return None
    if any(e[3] == "adj_factor" for e in events):
        return None
    if any(e[2] == "ods_daily" and e[4] for e in events):
        return None
    out: Set[str] = set()
    for _code, _td, table, col, _ins in events:
        inds = ODS_COLUMN_TO_INDICATORS.get((table, col), frozenset())
        out |= set(inds)
    if not out:
        return None
    if out == ALL_INDICATORS:
        return None
    return out
```

- [ ] **Step 3: `resolve_run_calc_indicator_filter(...)` with G1–G8**

```python
def resolve_run_calc_indicator_filter(
    con,
    fetch_result: FetchResult,
    *,
    changed_codes: List[str],
    stale_extra_codes: List[str],
    qfq_codes: List[str],
    force: bool = False,
) -> Optional[List[str]]:
    from backend.config import CALC_COLUMN_NARROW
    if not CALC_COLUMN_NARROW or force:
        return None
    if fetch_result.rows_written <= 0:
        return None
    if qfq_codes or stale_extra_codes:
        return None
    col_inds = resolve_affected_indicators(fetch_result.changed_field_events)
    if col_inds is None:
        return None
    spec_inds = _spec_stale_indicator_names(con)
    merged = set(col_inds) | spec_inds
    if merged == ALL_INDICATORS:
        return None
    return sorted(merged)
```

- [ ] **Step 4: pytest + commit**

```bash
git commit -m "feat(calc): ODS column to indicator dependency registry"
```

---

### Task 5: PipelineContext + run handoff

**Files:**
- Modify: `backend/etl/pipeline_context.py`
- Modify: `backend/cli.py` (`_rebuild_dwd_for_run`, `_cmd_run_single_day`)
- Modify: `backend/etl/calc_preflight_context.py`（或新建轻量 `RunCalcHandoff`）
- Test: `tests/test_etl/test_pipeline_context.py`

- [ ] **Step 1: Failing test — filter attached after rebuild**

```python
def test_pipeline_context_run_indicator_filter_circ_mv_only(con, monkeypatch):
    fr = FetchResult(
        rows_written=1,
        changed_pairs=[("000001.SZ", "20260612")],
        changed_field_events=[("000001.SZ", "20260612", "ods_daily_basic", "circ_mv", False)],
    )
    filt = resolve_run_calc_indicator_filter(
        con, fr, changed_codes=["000001.SZ"], stale_extra_codes=[], qfq_codes=[],
    )
    assert filt == ["dde"]
```

- [ ] **Step 2: `_rebuild_dwd_for_run` 返回 dwd meta**

```python
def _rebuild_dwd_for_run(...) -> tuple:
    ...
    qfq_codes = find_stocks_needing_qfq_refresh(con, to_rebuild, date) if to_rebuild else []
    stale_extra = sorted(set(stale) - set(changed))
    return result, to_rebuild, {"qfq_codes": qfq_codes, "stale_extra_codes": stale_extra}
```

- [ ] **Step 3: Before `cmd_calc`, set handoff**

```python
from backend.etl.calc_preflight_context import set_run_calc_handoff

set_run_calc_handoff({
    "indicator_filter": resolve_run_calc_indicator_filter(con, fr, ...),
    "calc_routes_narrowed": True/False,
})
```

- [ ] **Step 4: `cmd_calc` / `run_calc` pop handoff**

- [ ] **Step 5: `to_completeness()` 增加 `run_indicator_filter`, `calc_routes_narrowed`**

- [ ] **Step 6: pytest + commit**

```bash
git commit -m "feat(pipeline): resolve run-path indicator filter from ODS column events"
```

---

### Task 6: run_batch_append_phase 收窄

**Files:**
- Modify: `backend/etl/calc_batch_append.py`
- Modify: `backend/etl/orchestrator.py`
- Test: `tests/test_etl/test_batch_append_column_narrow.py`

- [ ] **Step 1: Failing test — only dde routes invoked**

```python
def test_run_batch_append_phase_respects_indicator_filter(monkeypatch, con):
    seen_routes = []
    orig = run_batch_full_phase
    def spy(con, calc_date, full_groups, batch_ctx):
        seen_routes.extend(full_groups.keys())
        return orig(con, calc_date, full_groups, batch_ctx)
    monkeypatch.setattr("backend.etl.calc_batch_append.run_batch_full_phase", spy)
    # force all stocks FULL for dde only scenario
    run_batch_append_phase(con, codes, calc_date, indicator_filter=["dde"])
    assert all(k[0] == "dde" for k in seen_routes)
    assert not any(k[0] == "macd" for k in seen_routes)
```

- [ ] **Step 2: Add param to `run_batch_append_phase`**

```python
def run_batch_append_phase(..., indicator_filter: Optional[List[str]] = None):
    from backend.etl.refresh_pipeline import resolve_refresh_routes
    active_routes = resolve_refresh_routes(indicator_filter)
    active_keys = set(active_routes)
    route_specs = [
        spec for spec in CALC_ROUTE_SPECS
        if (spec[0], spec[1]) in active_keys
    ]
```

- [ ] **Step 3: Conditional tail load**

```python
need_quote = indicator_filter is None or QUOTE_INDICATORS & set(indicator_filter)
need_dde = indicator_filter is None or "dde" in indicator_filter
if need_quote:
    daily_tails = batch_load_quote_tails(...)
else:
    daily_tails, weekly_tails = {}, {}
```

- [ ] **Step 4: Pass filtered `specs=` into `preflight_stock_modes_with_fps`**

- [ ] **Step 5: `run_calc` forward `indicator_filter` to batch + chunk worker**

Chunk worker：`_calc_stock_chunk` / selective pipeline 仅处理 filter 内路由（复用 refresh 的 completed_keys 语义）。

- [ ] **Step 6: pytest + commit**

```bash
git commit -m "feat(calc): narrow batch append/full to run-path indicator filter"
```

---

### Task 7: config + 观测 + 文档

**Files:**
- Modify: `backend/config.py`
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/plans/2026-06-15-change-driven-refresh-cli.md`（Wave 5 checklist + 链到本文）
- Modify: `docs/superpowers/plans/2026-06-09-daily-runbook.md`（可选 smoke 条目）

- [ ] **Step 1: Add env**

```python
CALC_COLUMN_NARROW = os.getenv("CALC_COLUMN_NARROW", "1") == "1"
```

- [ ] **Step 2: Log + ods_etl_log**

`calc_dws.data_completeness` 增加：

```json
{
  "run_indicator_filter": ["dde"],
  "calc_routes_narrowed": true,
  "active_routes": ["dde_daily", "dde_weekly"]
}
```

- [ ] **Step 3: CLAUDE.md** — run 路径列→指标收窄说明 + `CALC_COLUMN_NARROW=0` 回退

- [ ] **Step 4: Update parent plan Wave 5 checkboxes**

- [ ] **Step 5: Commit**

```bash
git commit -m "docs: Wave 5 column-indicator narrowing observability"
```

---

### Task 8: 实库 smoke + 全量 pytest

- [ ] **Step 1: 扩展 `scripts/smoke_change_driven_refresh.sh`**

```bash
# #7 circ_mv-only: UPDATE ods_daily_basic SET circ_mv=... WHERE ts_code='000543.SZ'
# run → assert calc_dws.data_completeness.run_indicator_filter=["dde"]
# assert full_by_indicator 无 macd_*
python -m backend.cli run --date 20260612 --skip-export --ts-code 000543.SZ
# restore circ_mv
```

- [ ] **Step 2: Scoped pytest**

```bash
pytest tests/test_fetch/test_ods_diff.py \
       tests/test_etl/test_column_indicator_deps.py \
       tests/test_etl/test_batch_append_column_narrow.py \
       tests/test_etl/test_pipeline_context.py \
       tests/test_cli/test_refresh_run.py -v
```

- [ ] **Step 3: Full regression**

```bash
pytest tests/ -v
```

Expected: all pass

- [ ] **Step 4: Commit smoke script**

```bash
git commit -m "test: smoke for run-path column-indicator narrowing"
```

---

## 验收标准

| # | 场景 | 期望 |
|---|------|------|
| A | 同 day 0 diff shortcut | 与 Wave 2 一致，`calc_routes_narrowed=false` |
| B | 仅 `circ_mv` 变更 1 股 | `run_indicator_filter=["dde"]`；macd batch 无写入 |
| C | `adj_factor` 变更 | 全 12 路由（narrow 禁用） |
| D | `refresh --indicator ma` | 行为不变（不受 C 影响） |
| E | spec stale ma + circ_mv | filter = `{dde, ma}` |
| F | `CALC_COLUMN_NARROW=0` | 全 12 路由 |

---

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 映射遗漏导致漏算 | 空集/不确定 → fallback 全路由；等价性测试 + smoke |
| patch 无 events（D2） | Task 3 强制 patch 返回 events |
| chunk worker 漏收窄 | Task 6 显式测试 full_items 路由 |
| 性能收益低于预期 | 观测 `active_routes` + batch tail skip 日志 |

---

## Self-Review（plan 作者自检）

| 检查项 | 结果 |
|--------|------|
| Spec §4.2 二期 C | Task 4–6 覆盖 |
| refresh 不受影响 | 明确不修改 `run_refresh_calc` |
| 无 placeholder | 映射表/门禁/文件路径已写死 |
| 类型一致 | `indicator_filter: Optional[List[str]]` 与 refresh 一致 |
| 质量门禁 | fallback 全路由 + pytest + smoke |

---

## 审批

- [x] 用户批准 Wave 5 实施（2026-06-15「好」）
- [x] M1–M4 实施完成（2026-06-15）
