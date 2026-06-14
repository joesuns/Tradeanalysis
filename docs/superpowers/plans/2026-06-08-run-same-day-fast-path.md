# Run 同日复跑快路径 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让生产路径 `cli run --date X`（X 已成功跑过）从「无条件 rebuild DWD + export」降为「stale 检测跳过 rebuild + 可选跳过 export」，calc 幂等保持不变（<1s）。

**Architecture:** 复用 `run_calc` 已有的 `find_stale_dwd_codes()`（G3 语义）：`n_fetch > 0` 时 rebuild 全量 codes；`n_fetch == 0` 时仅 rebuild stale 股，stale 为空则跳过。`cmd_run` 三步各写 `ods_etl_log`（`run_fetch` / `run_rebuild_dwd` / `run_export`），便于端到端 benchmark。新增 `--skip-export` 供同日复跑只更新数据不导 Excel。

**Tech Stack:** Python 3.9、DuckDB、pytest、现有 `log_etl_start`/`log_etl_end`。

**关联评审：** 系统架构师 2026-06-08 方案评审（否决裸 `n_fetch==0` 跳过，改用 stale_dwd）。

**范围外：** 除权日 adj 回填但未触发 fetch 的 DWD 重标定（需运维 `--force` 或单独 repair）；export 内容指纹自动跳过（本期只做显式 `--skip-export`）。

---

## File Map

| 文件 | 职责 |
|------|------|
| `backend/cli.py` | `cmd_run` stale 条件 rebuild；三步 ETL 日志；`--skip-export` |
| `backend/etl/orchestrator.py` | 导出 `find_stale_dwd_codes`（已存在，仅 import） |
| `tests/test_cli.py` | stale 跳过 / stale 重建 / skip-export 单测 |
| `CLAUDE.md` | run 同日复跑 runbook + 新 flag |
| `docs/superpowers/plans/2026-06-08-run-same-day-fast-path.md` | 本 plan |

---

### Task 1: stale_dwd 条件 rebuild（核心）

**Files:**
- Modify: `backend/cli.py:231-245`
- Test: `tests/test_cli.py`

- [x] **Step 1: Write the failing test**

在 `tests/test_cli.py` 末尾追加：

```python
def test_cmd_run_skips_rebuild_when_fetch_zero_and_dwd_fresh(monkeypatch):
    """n_fetch=0 且 find_stale_dwd_codes 空 → 不得 rebuild_all_dwd。"""
    import argparse
    from backend import cli

    rebuild_calls = []

    class FakeCon:
        def execute(self, *args, **kwargs):
            return type("R", (), {"fetchone": lambda self: ("20260605",)})()

        def close(self):
            pass

    def fake_fetch(*_a, **_k):
        return 0

    def fake_rebuild(con, codes):
        rebuild_calls.append(list(codes))

    def fake_stale(con, codes, date):
        assert date == "20260605"
        return []

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ", "B.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", fake_fetch)
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_all_dwd", fake_rebuild)
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes", fake_stale)
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: 1)
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=False,
    )
    cli.cmd_run(args)
    assert rebuild_calls == []


def test_cmd_run_rebuilds_stale_subset_when_fetch_zero(monkeypatch):
    """n_fetch=0 但 stale 非空 → 只 rebuild stale 股。"""
    import argparse
    from backend import cli

    rebuild_calls = []

    class FakeCon:
        def execute(self, *args, **kwargs):
            return type("R", (), {"fetchone": lambda self: ("20260605",)})()

        def close(self):
            pass

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ", "B.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes",
                        lambda con, codes, date: ["B.SZ"])
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_all_dwd",
                        lambda con, codes: rebuild_calls.append(list(codes)))
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: 1)
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=False,
    )
    cli.cmd_run(args)
    assert rebuild_calls == [["B.SZ"]]


def test_cmd_run_always_rebuilds_when_fetch_gt_zero(monkeypatch):
    """n_fetch>0 → rebuild 全部 codes（行为不变）。"""
    import argparse
    from backend import cli

    rebuild_calls = []

    class FakeCon:
        def execute(self, *args, **kwargs):
            return type("R", (), {"fetchone": lambda self: ("20260605",)})()

        def close(self):
            pass

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ", "B.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: 100)
    monkeypatch.setattr("backend.etl.build_dwd.rebuild_all_dwd",
                        lambda con, codes: rebuild_calls.append(list(codes)))
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: 1)
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=False,
    )
    cli.cmd_run(args)
    assert rebuild_calls == [["A.SZ", "B.SZ"]]
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cli.py::test_cmd_run_skips_rebuild_when_fetch_zero_and_dwd_fresh tests/test_cli.py::test_cmd_run_rebuilds_stale_subset_when_fetch_zero tests/test_cli.py::test_cmd_run_always_rebuilds_when_fetch_gt_zero -v`

Expected: FAIL（当前无条件 rebuild）

- [x] **Step 3: Implement `_rebuild_dwd_for_run` helper in cli.py**

在 `backend/cli.py` 的 `cmd_run` 上方新增：

```python
def _rebuild_dwd_for_run(con, codes: list[str], date: str, n_fetch: int) -> dict:
    """Rebuild DWD after run fetch step.

    n_fetch > 0: rebuild all codes (new ODS data).
    n_fetch == 0: rebuild only find_stale_dwd_codes; skip if none stale.
    Returns rebuild_all_dwd result dict, or {} if skipped.
    """
    from backend.etl.build_dwd import rebuild_all_dwd

    if n_fetch > 0:
        logger.info("Rebuilding DWD for %d stocks (fetch wrote %d ODS rows)", len(codes), n_fetch)
        return rebuild_all_dwd(con, codes)

    from backend.etl.orchestrator import find_stale_dwd_codes

    stale = find_stale_dwd_codes(con, codes, date)
    if not stale:
        logger.info("DWD fresh for %s — skip rebuild (%d stocks checked)", date, len(codes))
        return {}
    logger.info("DWD stale for %d/%d stocks on %s — rebuilding subset",
                len(stale), len(codes), date)
    return rebuild_all_dwd(con, stale)
```

- [x] **Step 4: Replace unconditional rebuild in `cmd_run`**

将 `cmd_run` Step 1 中：

```python
        rebuild_all_dwd(con, codes)
```

替换为：

```python
        dwd_result = _rebuild_dwd_for_run(con, codes, date, n_fetch)
```

- [x] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_cli.py::test_cmd_run_skips_rebuild_when_fetch_zero_and_dwd_fresh tests/test_cli.py::test_cmd_run_rebuilds_stale_subset_when_fetch_zero tests/test_cli.py::test_cmd_run_always_rebuilds_when_fetch_gt_zero -v`

Expected: PASS

- [x] **Step 6: Commit**

```bash
git add backend/cli.py tests/test_cli.py
git commit -m "perf: skip DWD rebuild on same-day run when find_stale_dwd empty"
```

---

### Task 2: run 三步 ETL 观测（ods_etl_log）

**Files:**
- Modify: `backend/cli.py`（`cmd_run`）
- Test: `tests/test_cli.py`

- [x] **Step 1: Write the failing test**

```python
def test_cmd_run_logs_fetch_rebuild_export_steps(monkeypatch):
    """cmd_run 应对 fetch/rebuild/export 各写一条 ods_etl_log。"""
    import argparse
    from backend import cli

    logged_steps = []

    class FakeCon:
        def execute(self, *args, **kwargs):
            return type("R", (), {"fetchone": lambda self: ("20260605",)})()

        def close(self):
            pass

    def fake_log_end(con, lid, step_name, t0, status, row_count=0, **kw):
        logged_steps.append((step_name, status, row_count, kw.get("data_completeness")))

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes", lambda *a, **k: [])
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel", lambda *a, **k: 42)
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", fake_log_end)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=False,
    )
    cli.cmd_run(args)
    step_names = [s[0] for s in logged_steps]
    assert step_names == ["run_fetch", "run_rebuild_dwd", "run_export"]
    assert logged_steps[0][2] == 0          # n_fetch
    assert logged_steps[1][3]["skipped"] is True  # rebuild skipped
    assert logged_steps[2][2] == 42         # export rows
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cli.py::test_cmd_run_logs_fetch_rebuild_export_steps -v`

Expected: FAIL

- [x] **Step 3: Instrument `cmd_run` with log_etl_start/end**

在 Step 1/3 分别包裹（示意，嵌入现有 `cmd_run`）：

```python
    from backend.etl.error_handler import log_etl_start, log_etl_end

    # Step 1
    con = get_connection()
    try:
        codes = ts_codes or get_all_active_codes(con)
        lid, t0 = log_etl_start(con, "run_fetch")
        if ts_codes:
            ...
            n_fetch = fetch_stocks_incremental(...)
        else:
            n_fetch = fetch_by_date_range_parallel(...)
        log_etl_end(con, lid, "run_fetch", t0, "success", row_count=n_fetch,
                    data_completeness={"analysis_date": date, "stocks": len(codes)})

        lid, t0 = log_etl_start(con, "run_rebuild_dwd")
        dwd_result = _rebuild_dwd_for_run(con, codes, date, n_fetch)
        rebuild_rows = sum(dwd_result.values()) if dwd_result else 0
        log_etl_end(
            con, lid, "run_rebuild_dwd", t0, "success", row_count=rebuild_rows,
            data_completeness={
                "analysis_date": date,
                "skipped": not dwd_result,
                "n_fetch": n_fetch,
            },
        )
    finally:
        con.close()

    # Step 3 (after calc)
    if not getattr(args, "skip_export", False):
        con = get_connection()
        try:
            lid, t0 = log_etl_start(con, "run_export")
            n = export_wide_to_excel(...)
            log_etl_end(con, lid, "run_export", t0, "success", row_count=n,
                        data_completeness={"analysis_date": date})
        finally:
            con.close()
    else:
        logger.info("Skipping export (--skip-export)")
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_cli.py::test_cmd_run_logs_fetch_rebuild_export_steps -v`

Expected: PASS

- [x] **Step 5: Commit**

```bash
git add backend/cli.py tests/test_cli.py
git commit -m "feat: log run_fetch/rebuild/export steps to ods_etl_log"
```

---

### Task 3: `--skip-export` flag

**Files:**
- Modify: `backend/cli.py`（parser + `cmd_run`）
- Test: `tests/test_cli.py`

- [x] **Step 1: Write the failing test**

```python
def test_cmd_run_skip_export(monkeypatch):
    """--skip-export → 不调用 export_wide_to_excel。"""
    import argparse
    from backend import cli

    export_called = []

    class FakeCon:
        def execute(self, *args, **kwargs):
            return type("R", (), {"fetchone": lambda self: ("20260605",)})()

        def close(self):
            pass

    monkeypatch.setattr("backend.db.connection.get_connection", lambda: FakeCon())
    monkeypatch.setattr("backend.fetch.ods_daily.get_all_active_codes", lambda _c: ["A.SZ"])
    monkeypatch.setattr("backend.fetch.ods_daily.fetch_by_date_range_parallel", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.orchestrator.find_stale_dwd_codes", lambda *a, **k: [])
    monkeypatch.setattr(cli, "cmd_calc", lambda _a, skip_stale_fetch=False: None)
    monkeypatch.setattr("backend.export_wide.export_wide_to_excel",
                        lambda *a, **k: export_called.append(True) or 1)
    monkeypatch.setattr(cli, "_warn_export_coverage", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start",
                        lambda *a, **k: ("lid", 0.0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)

    args = argparse.Namespace(
        date="20260605", ts_code=None, output="out.xlsx",
        include_st=False, no_index=False, db_path=None,
        force=False, skip_export=True,
    )
    cli.cmd_run(args)
    assert export_called == []
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cli.py::test_cmd_run_skip_export -v`

Expected: FAIL

- [x] **Step 3: Add parser flag**

在 `run` subparser（约 line 416）追加：

```python
    rp.add_argument("--skip-export", action="store_true",
                    help="Skip Excel export (same-day rerun when report unchanged)")
```

- [x] **Step 4: Run full cli tests**

Run: `python3 -m pytest tests/test_cli.py -v`

Expected: PASS（含既有 `test_cmd_run_closes_date_connection_before_calc`，需补 `skip_export=False` 到其 args）

- [x] **Step 5: Commit**

```bash
git add backend/cli.py tests/test_cli.py
git commit -m "feat: add --skip-export to cli run"
```

---

### Task 4: 文档与 runbook

**Files:**
- Modify: `CLAUDE.md`

- [x] **Step 1: 更新 CLAUDE.md「run」段**

在 CLI 三层架构 `run` 段追加：

```markdown
- **同日复跑快路径：** Step 1 fetch 若 0 rows 且 `find_stale_dwd_codes` 空 → **跳过 rebuild**；Step 2 calc 幂等闸门秒退；`--skip-export` 跳过 Excel。运维：仅需 Excel 时用 `cli export --date X`。
- **run 观测：** `ods_etl_log` 新增 `run_fetch` / `run_rebuild_dwd` / `run_export` 三步；`run_rebuild_dwd.data_completeness.skipped=true` 表示未重建。
- **边界：** 除权/adj 回填后若未触发 fetch，须 `fetch` + `calc --force` 或手动 rebuild；同日复跑不覆盖此场景。
```

- [x] **Step 2: 更新常用命令**

```bash
python -m backend.cli run --date 20260605              # 同日复跑（rebuild 可能跳过）
python -m backend.cli run --date 20260605 --skip-export  # 同日复跑不导 Excel
python -m backend.cli export --date 20260605           # 仅导出（最快同日复跑）
```

- [ ] **Step 3: 本 plan 状态 → 已完成（实库验收后）**

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/plans/2026-06-08-run-same-day-fast-path.md
git commit -m "docs: run same-day fast path runbook and observability"
```

---

### Task 5: 实库验收

- [ ] **Step 1: 同日复跑 benchmark**

```bash
# 基线（已实现 calc 幂等）
python3 -m backend.cli run --date 20260605

# 第二次同日复跑（应 skip rebuild + calc idempotent）
python3 -m backend.cli run --date 20260605 --skip-export
```

| 检查项 | 目标 |
|--------|------|
| 日志 `DWD fresh for ... — skip rebuild` | ✅ |
| 日志 `calc idempotent skip` | ✅ |
| `ods_etl_log.run_rebuild_dwd` skipped=true | ✅ |
| 墙钟 | 显著低于首次 run（export 除外时更快） |

- [ ] **Step 2: 回归全量测试**

Run: `python3 -m pytest tests/ -v`

Expected: 全绿

- [ ] **Step 3: 更新本 plan 第六节验收 checkbox**

---

## 验收标准

- [x] `n_fetch=0` + DWD fresh → 不 rebuild（单测锁定）
- [x] `n_fetch=0` + stale 非空 → 只 rebuild stale 股（单测锁定）
- [x] `n_fetch>0` → rebuild 全 codes（行为不变，单测锁定）
- [x] `--skip-export` 生效
- [x] `run_fetch` / `run_rebuild_dwd` / `run_export` 写入 `ods_etl_log`
- [x] `pytest tests/ -v` 绿（398 passed）
- [x] CLAUDE.md 已更新

## 八、Code Review 结论（2026-06-08）

**裁决：可合并；无数据损坏级 bug；快路径触发比设计预期窄。**

| 级别 | 发现 | 状态 |
|------|------|------|
| **P1** | `n_fetch>0` 作 rebuild 闸门过粗：fetch 返回行数≠「有新数据」；全市场 per-stock 整日覆盖才 `n_fetch=0` | **待 Plan 1.5** |
| **P2** | `n_fetch>0` 仍 rebuild 全部 codes，非 stale/受影响子集 | **待 Plan 1.5** |
| **P2** | ETL 三步无 `try/log_etl_error`，异常时 `ods_etl_log` 残留 running | 待修 |
| **P3** | `--skip-export` 不写 `run_export` 日志 | 待修 |
| **P3** | `cmd_run` docstring 未反映条件 rebuild | 待修 |
| **P3** | 缺 `--ts-code` 路径集成测、help 测 `--skip-export` | 待补 |

**通过项：** `find_stale_dwd_codes` 复用 G3 语义；`--force`/`skip_stale_fetch` 传递正确；mock 单测覆盖三路径。

### 实库验收（Task 5）

`run --date 20260605 --skip-export`（第二次）：

| 步骤 | 墙钟 | 结果 |
|------|------|------|
| run_fetch | 1.7s | 21749 rows（整日未跳过 → **未触发 rebuild 快路径**） |
| run_rebuild_dwd | **~14.8 min** | 5524 股全量 rebuild |
| calc | <1s | `calc idempotent skip` ✅ |
| export | 跳过 | `--skip-export` ✅ |
| **合计** | **~14m53s** | calc/export 达标；rebuild 仍为瓶颈 |

**结论：** Plan 1 逻辑正确；生产同日复跑要快，需 **fetch 返回 0** 或后续 Plan 1.5 改闸门。运维最快路径：`cli export --date X`。

## 风险

| 风险 | 缓解 |
|------|------|
| adj 变更未触发 fetch → DWD 旧 | 文档边界 + 运维 `--force` |
| stale_dwd 漏检 | 与 `run_calc` G3 同一函数，已生产验证 |
| 既有 test args 缺字段 | Task 3 Step 4 补 `skip_export=False` |
