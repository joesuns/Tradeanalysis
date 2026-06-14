# DWD Rebuild 后自动 refresh-state Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在任意 DWD rebuild 之后、calc 路由之前，自动对齐 `dws_calc_state.history_fp`，使真新日走 APPEND/batch 而非 FULL/chunk 灾难路径，满足 M4 SLA（≤1800s、chunk<400）。

**Architecture:** 抽取共享函数 `maybe_refresh_state_after_dwd_rebuild()`，仅在「DWD 实际写入」且 `DWD_REBUILD_REFRESH_STATE=1`（默认）时调用已有 `refresh_calc_state_fingerprints()`。刷新范围 = 本次 rebuild 的 stale 子集（非全市场）。接线点：`cli run` Step 1 rebuild 后、`run_calc` 内 auto-fetch rebuild 与 G3 stale DWD rebuild 后。不修改 Calculator、不改 `last_trade_date`、不重算 DWS。

**Tech Stack:** Python 3.9、DuckDB、pytest、现有 `calc_state_refresh` / `calc_router` / `run_batch_append_phase`

**背景证据（M4 事故 2026-06-11）：**

| 指标 | 事故值 | 目标 |
|------|--------|------|
| batch_append chunk | 5374 | <400 |
| batch_compute 日志 | 0 次 | 12 指标均有 append |
| calc.stocks ETA | ~8496s | 1–5 min |
| 根因 | DWD rebuild 后 `history_fp` 过期 → 全股 FULL | refresh 后单股验证 FULL→APPEND |

---

## File Map

| 文件 | 职责 |
|------|------|
| `backend/config.py` | 新增 `DWD_REBUILD_REFRESH_STATE` 开关 |
| `backend/etl/calc_state_refresh.py` | 共享 gate 函数 + 日志 |
| `backend/cli.py` | `run`：rebuild 后条件 refresh；`_rebuild_dwd_for_run` 返回 stale 列表 |
| `backend/etl/orchestrator.py` | `run_calc`：两处 rebuild 后 refresh |
| `backend/etl/calc_gate.py` | `run_refresh_state` 纳入 mutating steps |
| `scripts/benchmark_run.py` | SLA 报表增加 `run_refresh_state` 步骤 |
| `tests/test_etl/test_calc_state_refresh.py` | gate 函数单测 |
| `tests/test_cli.py` | run 链路：rebuild 触发 / 跳过 refresh |
| `tests/test_etl/test_orchestrator.py` | run_calc rebuild 后 refresh |
| `CLAUDE.md` | 数据流 + 环境变量 |
| `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md` | 附录 B 更新 |

---

### Task 1: 配置开关

**Files:**
- Modify: `backend/config.py`
- Test: `tests/test_etl/test_calc_state_refresh.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_etl/test_calc_state_refresh.py` 末尾追加：

```python
def test_maybe_refresh_skipped_when_flag_off(monkeypatch):
    from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

    calls = []

    def fake_refresh(con, codes, calc_date, dry_run=False):
        calls.append((codes, calc_date))
        return {"records_written": 1}

    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.refresh_calc_state_fingerprints",
        fake_refresh,
    )
    monkeypatch.setattr("backend.config.DWD_REBUILD_REFRESH_STATE", False)

    import duckdb
    con = duckdb.connect(":memory:")
    out = maybe_refresh_state_after_dwd_rebuild(
        con, ["A.SZ"], "20260610", {"daily_quote": 1},
    )
    assert out is None
    assert calls == []
    con.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_etl/test_calc_state_refresh.py::test_maybe_refresh_skipped_when_flag_off -v
```

Expected: FAIL `ImportError` 或 `AttributeError: maybe_refresh_state_after_dwd_rebuild`

- [ ] **Step 3: Add config flag**

在 `backend/config.py` 的 `DWD_INCREMENTAL` 行后追加：

```python
# DWD_REBUILD_REFRESH_STATE: after DWD rebuild, realign dws_calc_state history_fp
# before calc routing (prevents new-day FULL/chunk explosion). =0 disables.
DWD_REBUILD_REFRESH_STATE = os.getenv("DWD_REBUILD_REFRESH_STATE", "1").strip() != "0"
```

- [ ] **Step 4: Implement gate function stub**

在 `backend/etl/calc_state_refresh.py` 末尾追加：

```python
def maybe_refresh_state_after_dwd_rebuild(
    con,
    ts_codes: List[str],
    calc_date: str,
    dwd_result: dict,
) -> Optional[dict]:
    """Realign calc state fingerprints after a DWD rebuild, before calc routing.

    No-op when DWD_REBUILD_REFRESH_STATE=0, dwd_result empty, or ts_codes empty.
    Does not advance last_trade_date or recalculate DWS.
    """
    from backend.config import DWD_REBUILD_REFRESH_STATE

    if not DWD_REBUILD_REFRESH_STATE:
        return None
    if not dwd_result or not ts_codes:
        return None
    if not any(dwd_result.values()):
        return None
    return refresh_calc_state_fingerprints(con, ts_codes, calc_date, dry_run=False)
```

文件顶部 `from typing` 确保含 `Optional`。

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_etl/test_calc_state_refresh.py::test_maybe_refresh_skipped_when_flag_off -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/config.py backend/etl/calc_state_refresh.py tests/test_etl/test_calc_state_refresh.py
git commit -m "feat: add DWD rebuild refresh-state gate and config flag"
```

---

### Task 2: Gate 函数完整行为单测

**Files:**
- Modify: `backend/etl/calc_state_refresh.py`
- Test: `tests/test_etl/test_calc_state_refresh.py`

- [ ] **Step 1: Write failing tests**

```python
def test_maybe_refresh_runs_when_dwd_result_nonempty(monkeypatch):
    from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

    calls = []

    def fake_refresh(con, codes, calc_date, dry_run=False):
        calls.append((list(codes), calc_date, dry_run))
        return {"records_written": 3, "chunk_stocks": 0}

    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.refresh_calc_state_fingerprints",
        fake_refresh,
    )
    monkeypatch.setattr("backend.config.DWD_REBUILD_REFRESH_STATE", True)

    import duckdb
    con = duckdb.connect(":memory:")
    summary = maybe_refresh_state_after_dwd_rebuild(
        con, ["A.SZ", "B.SZ"], "20260610",
        {"daily_quote": 2, "weekly_quote": 0, "moneyflow": 1},
    )
    assert summary["records_written"] == 3
    assert calls == [(["A.SZ", "B.SZ"], "20260610", False)]
    con.close()


def test_maybe_refresh_skipped_when_dwd_result_empty(monkeypatch):
    from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

    calls = []
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.refresh_calc_state_fingerprints",
        lambda *a, **k: calls.append(1),
    )
    monkeypatch.setattr("backend.config.DWD_REBUILD_REFRESH_STATE", True)

    import duckdb
    con = duckdb.connect(":memory:")
    assert maybe_refresh_state_after_dwd_rebuild(con, ["A.SZ"], "20260610", {}) is None
    assert maybe_refresh_state_after_dwd_rebuild(
        con, ["A.SZ"], "20260610",
        {"daily_quote": 0, "weekly_quote": 0, "moneyflow": 0},
    ) is None
    assert calls == []
    con.close()
```

- [ ] **Step 2: Run tests — expect PASS**（Task 1 实现已满足）

```bash
pytest tests/test_etl/test_calc_state_refresh.py -v -k "maybe_refresh"
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_etl/test_calc_state_refresh.py
git commit -m "test: cover DWD rebuild refresh-state gate behaviors"
```

---

### Task 3: `cli run` 接线

**Files:**
- Modify: `backend/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing test**

在 `tests/test_cli.py` 追加：

```python
def test_cmd_run_refreshes_state_after_dwd_rebuild(monkeypatch):
    """DWD rebuild 非空 → 对 stale 子集调用 refresh-state，再 calc。"""
    import argparse
    from backend import cli

    events = []

    class FakeCon:
        def execute(self, *args, **kwargs):
            return type("R", (), {"fetchone": lambda self: ("20260610",)})()

        def close(self):
            pass

    def fake_rebuild_incremental(con, codes, d):
        events.append(("rebuild", list(codes)))
        return {"daily_quote": 10, "weekly_quote": 5, "moneyflow": 3}

    def fake_refresh(con, codes, calc_date, dwd_result):
        events.append(("refresh", list(codes), calc_date, dwd_result))
        return {"records_written": 12}

    def fake_calc(_args, skip_stale_fetch=False):
        events.append("calc")

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ", "B.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: 100)
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes",
                        lambda con, codes, date: ["B.SZ"])
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_dwd_incremental", fake_rebuild_incremental)
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.maybe_refresh_state_after_dwd_rebuild",
        fake_refresh,
    )
    monkeypatch.setattr(cli, "cmd_calc", fake_calc)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: 1)
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260610", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=True,
    )
    cli.cmd_run(args)
    assert ("rebuild", ["B.SZ"]) in events
    refresh_events = [e for e in events if isinstance(e, tuple) and e[0] == "refresh"]
    assert len(refresh_events) == 1
    assert refresh_events[0][1] == ["B.SZ"]
    assert refresh_events[0][2] == "20260610"
    assert events[-1] == "calc"


def test_cmd_run_skips_refresh_when_dwd_skipped(monkeypatch):
    import argparse
    from backend import cli

    refresh_calls = []

    class FakeCon:
        def execute(self, *args, **kwargs):
            return type("R", (), {"fetchone": lambda self: ("20260610",)})()

        def close(self):
            pass

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes", lambda *a, **k: [])
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.maybe_refresh_state_after_dwd_rebuild",
        lambda *a, **k: refresh_calls.append(1),
    )
    monkeypatch.setattr(cli, "cmd_calc", lambda *a, **k: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: 1)
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260610", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=True,
    )
    cli.cmd_run(args)
    assert refresh_calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_cli.py::test_cmd_run_refreshes_state_after_dwd_rebuild tests/test_cli.py::test_cmd_run_skips_refresh_when_dwd_skipped -v
```

Expected: FAIL（refresh 未被调用）

- [ ] **Step 3: Refactor `_rebuild_dwd_for_run` 返回 stale 列表**

将 `backend/cli.py` 中 `_rebuild_dwd_for_run` 改为返回 `tuple[dict, list]`：

```python
def _rebuild_dwd_for_run(con, codes: list[str], date: str, n_fetch: int) -> tuple:
    """Returns (dwd_result_dict, stale_codes_rebuilt).

    Empty dict + [] when rebuild skipped.
    """
    from backend.etl.orchestrator import find_stale_dwd_codes

    stale = find_stale_dwd_codes(con, codes, date)
    if not stale:
        if n_fetch > 0:
            logger.info(
                "DWD already fresh for %s after fetch (%d ODS rows) — skip rebuild",
                date, n_fetch,
            )
        else:
            logger.info(
                "DWD fresh for %s — skip rebuild (%d stocks checked)",
                date, len(codes),
            )
        return {}, []

    from backend.etl.build_dwd import rebuild_dwd_for_stale

    if n_fetch > 0:
        logger.info(
            "Rebuilding DWD for %d stale stocks (fetch wrote %d ODS rows)",
            len(stale), n_fetch,
        )
    else:
        logger.info(
            "DWD stale for %d/%d stocks on %s — rebuilding subset",
            len(stale), len(codes), date,
        )
    result = rebuild_dwd_for_stale(con, stale, date)
    return result, stale
```

- [ ] **Step 4: Wire refresh in `cmd_run`**

在 `cmd_run` Step 1 的 rebuild 块中，替换为：

```python
        lid, t0 = log_etl_start(con, "run_rebuild_dwd")
        dwd_result, stale_rebuilt = _rebuild_dwd_for_run(con, codes, date, n_fetch)
        rebuild_rows = sum(dwd_result.values()) if dwd_result else 0
        log_etl_end(
            con, lid, "run_rebuild_dwd", t0, "success", row_count=rebuild_rows,
            data_completeness={
                "analysis_date": date,
                "skipped": not dwd_result,
                "n_fetch": n_fetch,
                "stale_count": len(stale_rebuilt),
            },
        )

        if dwd_result and stale_rebuilt:
            from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

            lid2, t02 = log_etl_start(con, "run_refresh_state")
            try:
                refresh_summary = maybe_refresh_state_after_dwd_rebuild(
                    con, stale_rebuilt, date, dwd_result,
                )
                log_etl_end(
                    con, lid2, "run_refresh_state", t02, "success",
                    row_count=(refresh_summary or {}).get("records_written", 0),
                    data_completeness={
                        "analysis_date": date,
                        "stale_count": len(stale_rebuilt),
                        **(refresh_summary or {}),
                    },
                )
            except Exception:
                log_etl_end(con, lid2, "run_refresh_state", t02, "failed")
                raise
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_cli.py -v -k "refresh"
```

Expected: 两个新测试 PASS；既有 run 测试仍 PASS（无行为回归）

- [ ] **Step 6: Commit**

```bash
git add backend/cli.py tests/test_cli.py
git commit -m "feat: auto refresh-state after DWD rebuild in cli run"
```

---

### Task 4: `run_calc` 两处 rebuild 接线

**Files:**
- Modify: `backend/etl/orchestrator.py`
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: Write failing test**

在 `tests/test_etl/test_orchestrator.py` 追加（复用现有 `temp_db` / monkeypatch 模式）：

```python
def test_run_calc_refreshes_state_after_stale_dwd_rebuild(temp_db, monkeypatch):
    """G3 stale DWD rebuild 后应 refresh affected codes。"""
    from backend.etl.orchestrator import run_calc

    refresh_calls = []

    monkeypatch.setattr(
        "backend.etl.orchestrator.check_data_completeness",
        lambda con, codes, calc_date=None: {"ok": ["A.SZ"], "missing": {}},
    )
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_ods_codes", lambda *a, **k: [])
    monkeypatch.setattr(
        "backend.etl.orchestrator.find_stale_dwd_codes",
        lambda con, codes, calc_date: ["A.SZ"],
    )

    def fake_rebuild(con, codes, trade_date):
        return {"daily_quote": 1, "weekly_quote": 0, "moneyflow": 0}

    monkeypatch.setattr("backend.etl.build_dwd.rebuild_dwd_for_stale", fake_rebuild)
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.maybe_refresh_state_after_dwd_rebuild",
        lambda con, codes, calc_date, dwd_result: refresh_calls.append(
            (list(codes), calc_date, dwd_result)
        ) or {"records_written": 1},
    )
    monkeypatch.setattr(
        "backend.etl.calc_batch_append.run_batch_append_phase",
        lambda *a, **k: {"chunk_codes": [], "completed_keys": set(), "agg_by_key": {}},
    )
    monkeypatch.setattr("backend.etl.orchestrator.resolve_calc_workers", lambda: 1)

    con = temp_db
    run_calc(con, ts_codes=["A.SZ"], auto_fetch=True, calc_date="20260610", skip_stale_fetch=True)
    assert len(refresh_calls) == 1
    assert refresh_calls[0][0] == ["A.SZ"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_etl/test_orchestrator.py::test_run_calc_refreshes_state_after_stale_dwd_rebuild -v
```

Expected: FAIL `assert len(refresh_calls) == 1` → 0

- [ ] **Step 3: Add helper in orchestrator**

在 `backend/etl/orchestrator.py` 顶部 import 区附近或 `run_calc` 前添加：

```python
def _refresh_state_after_dwd_rebuild(con, ts_codes: list, calc_date: str, dwd_result: dict) -> None:
    from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

    summary = maybe_refresh_state_after_dwd_rebuild(con, ts_codes, calc_date, dwd_result)
    if summary:
        logger.info(
            "refresh_state after DWD rebuild: stocks=%d written=%d chunk_stocks=%d",
            summary.get("stocks", len(ts_codes)),
            summary.get("records_written", 0),
            summary.get("chunk_stocks", -1),
        )
```

- [ ] **Step 4: Wire auto-fetch rebuild path**（`run_calc` ~L1376）

将：

```python
                log_timed_step(
                    "calc.auto_fetch", "rebuild_dwd",
                    lambda: rebuild_dwd_for_stale(con, fetched_codes, calc_date),
                    stocks=len(fetched_codes),
                )
```

改为：

```python
                dwd_result = log_timed_step(
                    "calc.auto_fetch", "rebuild_dwd",
                    lambda: rebuild_dwd_for_stale(con, fetched_codes, calc_date),
                    stocks=len(fetched_codes),
                )
                _refresh_state_after_dwd_rebuild(
                    con, fetched_codes, calc_date, dwd_result or {},
                )
```

- [ ] **Step 5: Wire G3 stale DWD path**（`run_calc` ~L1409）

将：

```python
                log_timed_step(
                    "calc.stale_dwd", "rebuild",
                    lambda: rebuild_dwd_for_stale(con, stale_dwd, calc_date),
                    stocks=len(stale_dwd),
                )
```

改为：

```python
                dwd_result = log_timed_step(
                    "calc.stale_dwd", "rebuild",
                    lambda: rebuild_dwd_for_stale(con, stale_dwd, calc_date),
                    stocks=len(stale_dwd),
                )
                _refresh_state_after_dwd_rebuild(
                    con, stale_dwd, calc_date, dwd_result or {},
                )
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_etl/test_orchestrator.py::test_run_calc_refreshes_state_after_stale_dwd_rebuild -v
pytest tests/test_etl/test_orchestrator.py -v --tb=short -q
```

Expected: PASS，无 orchestrator 回归

- [ ] **Step 7: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "feat: refresh calc state after run_calc DWD rebuild paths"
```

---

### Task 5: calc_gate + benchmark_run 观测

**Files:**
- Modify: `backend/etl/calc_gate.py`
- Modify: `scripts/benchmark_run.py`
- Test: `tests/test_etl/test_calc_gate.py`（若存在）或 `tests/test_cli.py`

- [ ] **Step 1: Add mutating step**

在 `backend/etl/calc_gate.py` 的 `_MUTATING_ETL_STEPS` 元组中追加 `"run_refresh_state"`（在 `"run_rebuild_dwd"` 之后）。

- [ ] **Step 2: Extend benchmark RUN_STEPS**

在 `scripts/benchmark_run.py` 将：

```python
RUN_STEPS = ("run_fetch", "run_rebuild_dwd", "calc_dws", "run_export")
```

改为：

```python
RUN_STEPS = (
    "run_fetch",
    "run_rebuild_dwd",
    "run_refresh_state",
    "calc_dws",
    "run_export",
)
```

说明：`run_refresh_state` 仅在新日 rebuild 时出现；缺失时 benchmark 打印 `—` 即可（现有 `_fetch_step_row` 已处理 None）。

- [ ] **Step 3: Verify**

```bash
python3 -c "from backend.etl.calc_gate import _MUTATING_ETL_STEPS; assert 'run_refresh_state' in _MUTATING_ETL_STEPS"
pytest tests/ -v -k "calc_gate or benchmark" --tb=short 2>/dev/null || pytest tests/test_cli.py -v -q
```

- [ ] **Step 4: Commit**

```bash
git add backend/etl/calc_gate.py scripts/benchmark_run.py
git commit -m "chore: observe run_refresh_state in gate and benchmark"
```

---

### Task 6: 文档更新

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md`

- [ ] **Step 1: CLAUDE.md**

在「CLI 三层架构」`run` 小节补充：

```markdown
- **DWD rebuild 后自动 refresh-state（`DWD_REBUILD_REFRESH_STATE=1` 默认）：** `run_rebuild_dwd` 实际写入后，对 stale 子集调用 `refresh_calc_state_fingerprints`（`run_refresh_state` 审计步），对齐 `history_fp` 再进 calc；防止真新日 fp 漂移 → 全股 FULL/chunk。`run_calc` 内 auto-fetch / G3 stale DWD rebuild 同理。`=0` 回退旧行为（仅运维 `cli refresh-state`）。
```

在环境变量区追加 `DWD_REBUILD_REFRESH_STATE`。

- [ ] **Step 2: 附录 B 更新**

在 `2026-06-09-pipeline-30min-optimization.md` 附录 B 表格追加：

| 项 | 状态 |
|----|------|
| M4 真新日事故根因 | DWD rebuild 后未 refresh → chunk=5374 |
| 修复 plan | `2026-06-11-dwd-rebuild-auto-refresh-state.md` |
| M4 重跑 | 修复后恢复备份再 benchmark |

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md docs/superpowers/plans/2026-06-11-dwd-rebuild-auto-refresh-state.md
git commit -m "docs: DWD rebuild auto refresh-state and M4 recovery notes"
```

---

### Task 7: 全量测试 + M4 重验收（运维）

**Files:** 无代码变更（实库操作）

- [ ] **Step 1: 恢复污染库**

```bash
cp data/tradeanalysis.pre-20260611.duckdb data/tradeanalysis.duckdb
```

若无备份，改跑全市场 `python3 -m backend.cli refresh-state --date 20260609` 后再继续（次优）。

- [ ] **Step 2: 全量 pytest**

```bash
pytest tests/ -v --tb=short
```

Expected: 全绿

- [ ] **Step 3: M4 benchmark**

```bash
cp data/tradeanalysis.duckdb data/tradeanalysis.pre-m4-retry.duckdb
python3 scripts/benchmark_run.py --date 20260610 --run --skip-export
python3 scripts/health_check.py
```

**PASS 标准：**

| 检查项 | 阈值 |
|--------|------|
| exit code | 0 |
| 总墙钟 | ≤1800s |
| `chunk_stocks`（日志 `batch_append: done`） | <400 |
| `run_refresh_state` | 存在且 `records_written>0` |
| health_check | 无 CRITICAL |

- [ ] **Step 4: 记录结果到附录 B**

将实测墙钟、chunk、batch_only 写入 `2026-06-09-pipeline-30min-optimization.md` 附录 B M4 签字段。

---

## Self-Review

| Spec 要求 | 对应 Task |
|-----------|-----------|
| DWD rebuild 后自动 fp 对齐 | Task 1–4 |
| 仅 rebuild 时触发（不做多余计算） | Task 1 gate + Task 3 skip 测试 |
| `cli run` 主路径 | Task 3 |
| `run_calc` G3 / auto-fetch | Task 4 |
| 环境变量开关 | Task 1 |
| M4 重验收 | Task 7 |
| 文档 | Task 6 |
| 数据质量（不改 DWS / last_td） | Architecture 声明 + 现有 refresh 语义 |

**Placeholder scan:** 无 TBD/TODO/“适当处理”。

**类型一致性：** `maybe_refresh_state_after_dwd_rebuild` 签名全任务统一；`_rebuild_dwd_for_run` 返回 `tuple[dict, list]`。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-11-dwd-rebuild-auto-refresh-state.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每 Task 派 fresh subagent，Task 间 review

**2. Inline Execution** — 本会话按 Task 1→7 连续实施，Task 5/7 为检查点

Which approach?
