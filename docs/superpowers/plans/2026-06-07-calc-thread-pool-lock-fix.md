# Calc 多进程锁冲突修复（线程池回退）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `run_calc` 的 DWS 并行从 `multiprocessing.Pool` 回退到 `ThreadPoolExecutor`，消除 DuckDB 单文件跨进程写锁冲突（实库已炸），并顺带修复同函数内的行数统计虚高与 `CALC_WORKERS` 非法值崩溃。

**Architecture:** DuckDB 单文件仅允许一个 read-write 进程；`multiprocessing.Pool` 的 8 个 worker 各自 `duckdb.connect(DUCKDB_PATH)` 必然抢锁失败（`IOException: Could not set lock`）。改用同进程线程池：线程共享同一 DuckDB 实例，支持多写线程（MVCC），且 `backend/fetch/ods_daily.py` 的 3 线程 fetch 已用相同模式成功写入百万行。`CALC_WORKERS` 语义由「进程数」改为「线程数」。

**Tech Stack:** Python `concurrent.futures.ThreadPoolExecutor`、DuckDB、contextvars（run_id 传播）、pytest。

**Scope（本计划仅 Phase 0 + 同函数内顺带修复）：**
- ✅ Task 1：`run_calc` 线程池回退 + `resolve_calc_workers` 非法值防护（P0-1 + P2-3）
- ✅ Task 2：`_calc_stock_chunk` 行数统计按 chunk 内 ts_code 计（P1-1）
- ✅ Task 3：测试改造（Pool mock → 线程；行数 helper 单测）
- ✅ Task 4：实库验收 + 文档（CLAUDE.md / spec §12.7）
- ⏸ **延后到后续计划**：P1-2 DWD 重建后指纹失效、P2-1 `run_etl`→`run_calc` 统一、P2-2 `CALC_INCREMENTAL` 同日切换护栏

---

## File Structure

| 文件 | 职责 | 变更 |
|------|------|------|
| `backend/etl/orchestrator.py` | calc 编排、并行调度、行数统计 | 改 `run_calc` 并行段、`resolve_calc_workers`、`_calc_stock_chunk` 计数；删 `_calc_worker_init`；新增 `_count_calc_rows` helper |
| `backend/config.py` | 环境变量注释 | 更新 `CALC_WORKERS` 注释（进程→线程） |
| `tests/test_etl/test_orchestrator.py` | 并行调度 + worker 解析测试 | 改 2 个 Pool-相关测试为线程；新增非法值测试 |
| `tests/test_etl/test_incremental_calc.py` | 行数 helper 单测 | 新增 `_count_calc_rows` 测试 |
| `CLAUDE.md` | 架构说明 | P2 多进程 → 线程池 |
| `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` | §12.7 | `CALC_WORKERS` 语义 |

---

## Task 1: run_calc 线程池回退 + CALC_WORKERS 非法值防护

**Files:**
- Modify: `backend/etl/orchestrator.py:48-60`（`resolve_calc_workers` + 删 `_calc_worker_init`）
- Modify: `backend/etl/orchestrator.py:950-982`（`run_calc` 并行段）
- Test: `tests/test_etl/test_orchestrator.py`（Task 3 覆盖）

- [ ] **Step 1: 写失败测试 — 非法 CALC_WORKERS 回退默认**

在 `tests/test_etl/test_orchestrator.py` 的 `# ── P2: multiprocessing worker resolution ──` 区块后追加：

```python
def test_resolve_calc_workers_invalid_falls_back(monkeypatch):
    """非法 CALC_WORKERS（非数字）→ 回退默认值，不崩溃。"""
    import multiprocessing
    monkeypatch.setenv("CALC_WORKERS", "abc")
    from backend.etl.orchestrator import resolve_calc_workers
    expected = max(1, min(multiprocessing.cpu_count() - 1, 8))
    assert resolve_calc_workers() == expected
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_etl/test_orchestrator.py::test_resolve_calc_workers_invalid_falls_back -v`
Expected: FAIL — `ValueError: invalid literal for int() with base 10: 'abc'`

- [ ] **Step 3: 修复 `resolve_calc_workers`（非法值防护 + 语义注释）**

将 `backend/etl/orchestrator.py:48-53` 的函数替换为：

```python
def resolve_calc_workers() -> int:
    """Resolve calc parallelism: CALC_WORKERS env or min(cpu-1, 8).

    CALC_WORKERS controls the number of calc *threads* (not processes).
    DuckDB's single-file lock forbids concurrent read-write processes, so calc
    parallelism is thread-based, sharing one in-process DuckDB instance.
    Invalid (non-integer) values fall back to the default with a warning.
    """
    env = os.getenv("CALC_WORKERS", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            logger.warning("Invalid CALC_WORKERS=%r, falling back to default", env)
    return max(1, min(multiprocessing.cpu_count() - 1, 8))
```

- [ ] **Step 4: 删除 `_calc_worker_init`（多进程专用初始化，线程池不需要）**

删除 `backend/etl/orchestrator.py:56-60`：

```python
def _calc_worker_init(run_id: str):
    """Pool worker initializer — logging + trace ID in child processes."""
    from backend.log_config import setup_logging, set_run_id
    setup_logging("backend.etl.orchestrator")
    set_run_id(run_id)
```

> 线程共享主进程已配置的 root logger；run_id 用 `set_run_id` 作线程池 initializer 传播即可（见 Step 5）。`import multiprocessing` 仍由 `resolve_calc_workers()` 使用，**保留**。

- [ ] **Step 5: 改 `run_calc` 并行段为 ThreadPoolExecutor**

将 `backend/etl/orchestrator.py:950-974` 替换为：

```python
    # 4. 计算 DWS — ThreadPoolExecutor by stock chunk.
    #    DuckDB 单文件仅允许一个 read-write 进程；multiprocessing 会争文件锁
    #    （IOException: Could not set lock）。线程共享同一进程 DuckDB 实例，
    #    支持多写线程（MVCC），与 ods_daily fetch 的 3 线程模式一致。
    workers = resolve_calc_workers()
    chunk_size = max(1, (len(codes_to_calc) + workers - 1) // workers)
    chunks = [codes_to_calc[i:i + chunk_size]
              for i in range(0, len(codes_to_calc), chunk_size)]

    logger.info("calc %d stocks with %d threads (%d stocks/chunk)",
                len(codes_to_calc), workers, chunk_size)

    lid, t0 = log_etl_start(con, "calc_dws")
    calc_start = time.monotonic()

    from backend.config import CALC_INCREMENTAL
    from backend.log_config import _run_id, set_run_id
    from concurrent.futures import ThreadPoolExecutor

    rid = _run_id.get()
    with ThreadPoolExecutor(
        max_workers=workers,
        initializer=set_run_id,
        initargs=(rid,),
    ) as executor:
        results = list(executor.map(
            _calc_stock_chunk,
            chunks,
            [calc_date] * len(chunks),
            [CALC_INCREMENTAL] * len(chunks),
        ))
    grand_total = sum(results)
```

> 保留其后 `total_elapsed` / `logger.info("calc ALL DONE ...")` / `log_etl_end` / `run_checkpoint(con)` 不变（lines 976-982）。

- [ ] **Step 6: 运行 worker 解析全部测试，确认通过**

Run: `pytest tests/test_etl/test_orchestrator.py -k "resolve_calc_workers" -v`
Expected: PASS（4 项：env_override / env_minimum_one / default_capped / invalid_falls_back）

---

## Task 2: _calc_stock_chunk 行数统计按 chunk 内 ts_code 计（P1-1）

**Files:**
- Modify: `backend/etl/orchestrator.py:711-767`（新增 `_count_calc_rows` helper + 替换计数）
- Test: `tests/test_etl/test_incremental_calc.py`

- [ ] **Step 1: 写失败测试 — 行数 helper 按 ts_code 域计数**

在 `tests/test_etl/test_incremental_calc.py` 末尾追加：

```python
def test_count_calc_rows_scoped_by_ts_code():
    """_count_calc_rows 只统计指定 ts_code + calc_date 的行，不含其他股票。"""
    from backend.etl.orchestrator import _count_calc_rows

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_x (ts_code TEXT, trade_date TEXT, calc_date TEXT)
    """)
    con.execute("INSERT INTO dws_x VALUES ('A.SZ','20260101','20260605')")
    con.execute("INSERT INTO dws_x VALUES ('A.SZ','20260102','20260605')")
    con.execute("INSERT INTO dws_x VALUES ('B.SZ','20260101','20260605')")
    con.execute("INSERT INTO dws_x VALUES ('C.SZ','20260101','20260604')")  # 旧 calc_date

    assert _count_calc_rows(con, "dws_x", "20260605", ["A.SZ"]) == 2
    assert _count_calc_rows(con, "dws_x", "20260605", ["A.SZ", "B.SZ"]) == 3
    assert _count_calc_rows(con, "dws_x", "20260605", ["C.SZ"]) == 0
    assert _count_calc_rows(con, "dws_x", "20260605", []) == 0
    con.close()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_etl/test_incremental_calc.py::test_count_calc_rows_scoped_by_ts_code -v`
Expected: FAIL — `ImportError: cannot import name '_count_calc_rows'`

- [ ] **Step 3: 新增 `_count_calc_rows` helper**

在 `backend/etl/orchestrator.py` 的 `_calc_stock_chunk` 定义（line 711）**之前**插入：

```python
def _count_calc_rows(con, table: str, calc_date: str, ts_codes: list[str]) -> int:
    """Count DWS rows written for a specific calc_date AND ts_code set.

    Scoped by ts_code so per-chunk totals are disjoint — summing chunk results
    yields the true grand total (the old whole-table COUNT double-counted across
    threads/chunks).
    """
    if not ts_codes:
        return 0
    ph = ",".join(["?"] * len(ts_codes))
    return con.execute(
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE calc_date = ? AND ts_code IN ({ph})",
        [calc_date] + ts_codes,
    ).fetchone()[0]
```

- [ ] **Step 4: 在 `_calc_stock_chunk` 中改用 helper**

将 `backend/etl/orchestrator.py:748-752`：

```python
                n = con.execute(
                    f"SELECT COUNT(*) FROM {calc.dws_table} "
                    f"WHERE calc_date = ?", (calc_date,),
                ).fetchone()[0]
                chunk_total += n
```

替换为：

```python
                n = _count_calc_rows(con, calc.dws_table, calc_date, chunk)
                chunk_total += n
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `pytest tests/test_etl/test_incremental_calc.py::test_count_calc_rows_scoped_by_ts_code -v`
Expected: PASS

---

## Task 3: 测试改造 — Pool mock → 线程

**Files:**
- Modify: `tests/test_etl/test_orchestrator.py:490-535`（`test_run_calc_skips_rebuild_when_only_weekly_fetch_and_ods_full`）
- Modify: `tests/test_etl/test_orchestrator.py:624-662`（`test_run_calc_uses_multiprocessing_pool`）

- [ ] **Step 1: 改 `test_run_calc_skips_rebuild...` — 删除 `_SyncPool`/Pool mock**

将 `tests/test_etl/test_orchestrator.py:511-529` 这一段：

```python
    monkeypatch.setattr(orch, "_calc_stock_chunk", lambda *a, **k: 0)

    class _SyncPool:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starmap(self, fn, iterable):
            return [fn(*args) for args in iterable]

    monkeypatch.setattr(orch.multiprocessing, "Pool", _SyncPool)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start", lambda *a: ("lid", 0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)
    monkeypatch.setattr(orch, "run_checkpoint", lambda *a: None)
```

替换为：

```python
    monkeypatch.setattr(orch, "_calc_stock_chunk", lambda *a, **k: 0)
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start", lambda *a: ("lid", 0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)
    monkeypatch.setattr(orch, "run_checkpoint", lambda *a: None)
```

> `_calc_stock_chunk` 已 mock 为 no-op，ThreadPoolExecutor 直接调用它即可，无需 Pool mock。

- [ ] **Step 2: 替换 `test_run_calc_uses_multiprocessing_pool` 为线程版**

将 `tests/test_etl/test_orchestrator.py:624-662` 整个函数替换为：

```python
def test_run_calc_uses_thread_pool(monkeypatch):
    """run_calc should dispatch chunks via ThreadPoolExecutor (not multiprocessing)."""
    import duckdb
    from backend.etl.orchestrator import run_calc

    con = duckdb.connect(":memory:")
    calls = []

    def fake_chunk(chunk, calc_date, incremental):
        calls.append((tuple(chunk), calc_date, incremental))
        return 0

    monkeypatch.setattr("backend.etl.orchestrator._calc_stock_chunk", fake_chunk)
    monkeypatch.setattr("backend.etl.orchestrator.resolve_calc_workers", lambda: 2)
    monkeypatch.setattr(
        "backend.etl.orchestrator.check_data_completeness",
        lambda *a, **k: {"ok": ["A.SZ", "B.SZ"], "missing": {}, "weekly_fetch": {}},
    )
    monkeypatch.setattr("backend.etl.orchestrator._filter_delisted", lambda *a, **k: (a[1], {}))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_start", lambda *a: (1, 0))
    monkeypatch.setattr("backend.etl.error_handler.log_etl_end", lambda *a, **k: None)
    monkeypatch.setattr("backend.etl.orchestrator.run_checkpoint", lambda *a: None)

    run_calc(con, ts_codes=["A.SZ", "B.SZ"], calc_date="20260605", auto_fetch=False)

    # 2 workers, 2 stocks → chunk_size=1 → 2 个单股 chunk
    assert len(calls) == 2
    assert {c[0] for c in calls} == {("A.SZ",), ("B.SZ",)}
    assert all(c[1] == "20260605" for c in calls)
    con.close()
```

- [ ] **Step 3: 运行改造后的 run_calc 测试，确认通过**

Run: `pytest tests/test_etl/test_orchestrator.py -k "run_calc" -v`
Expected: PASS（含 `test_run_calc_uses_thread_pool` 与 `test_run_calc_skips_rebuild_when_only_weekly_fetch_and_ods_full`）

- [ ] **Step 4: 运行 orchestrator + incremental 全量测试**

Run: `pytest tests/test_etl/test_orchestrator.py tests/test_etl/test_incremental_calc.py -q`
Expected: PASS（全绿，无 `multiprocessing` 残留引用报错）

---

## Task 4: 实库验收 + 文档更新

**Files:**
- Modify: `backend/config.py:17`
- Modify: `CLAUDE.md`（calc 流程 P2 行 + 多进程 calc 条目）
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`（§12.7 CALC_WORKERS）

- [ ] **Step 1: 全量测试**

Run: `pytest tests/ -q`
Expected: PASS（基线 338 → 应为 339+，新增 2 测试、改名 1 测试）

- [ ] **Step 2: 实库第一次 calc（应跑通，不再锁冲突）**

Run: `python3 -m backend.cli calc --date 20260601`
Expected: 无 `IOException: Could not set lock`；日志出现 `calc N stocks with M threads`；`calc ALL DONE — <真实行数> rows`（不再 ×线程数虚高）

- [ ] **Step 3: 实库第二次 calc（指纹 skip，应秒级）**

Run: `python3 -m backend.cli calc --date 20260601`
Expected: 大量 `fingerprint_match` skip，calc 段墙钟显著低于第一次

- [ ] **Step 4: 健康体检**

Run: `python3 -m scripts.health_check`
Expected: Section I（成熟股最新 week-end 截面）无异常

- [ ] **Step 5: 更新 `backend/config.py:17` 注释**

将：

```python
# CALC_WORKERS: optional override for calc multiprocessing (default min(cpu-1, 8))
```

替换为：

```python
# CALC_WORKERS: optional override for calc thread-pool size (default min(cpu-1, 8)).
# DuckDB single-file lock forbids multi-process writes, so calc parallelism is
# thread-based (shared in-process instance), not multiprocessing.
```

- [ ] **Step 6: 更新 `CLAUDE.md` — calc 流程 P2 行**

将 `CLAUDE.md` 中：

```
│   ├── P2 多进程：`multiprocessing.Pool` + `resolve_calc_workers()`（默认 `min(cpu-1, 8)`，`CALC_WORKERS` 可覆盖）
```

替换为：

```
│   ├── P2 多线程：`ThreadPoolExecutor` + `resolve_calc_workers()`（默认 `min(cpu-1, 8)`，`CALC_WORKERS` 可覆盖）。DuckDB 单文件禁跨进程写，故线程池共享同一实例
```

- [ ] **Step 7: 更新 `CLAUDE.md` — 多进程 calc 条目**

将 `CLAUDE.md` 中：

```
- **多进程 calc（P2）:** `run_calc` 用 `multiprocessing.Pool` 按股分片并行（默认 `min(cpu-1, 8)` 进程，
  `CALC_WORKERS=N` 覆盖），每 worker 独立 DuckDB 连接，绕过 GIL。墙钟记 `ods_etl_log`（观测项，非硬 KPI）。
```

替换为：

```
- **多线程 calc（P2）:** `run_calc` 用 `ThreadPoolExecutor` 按股分片并行（默认 `min(cpu-1, 8)` 线程，
  `CALC_WORKERS=N` 覆盖），每线程独立 DuckDB 连接、共享同一进程实例（MVCC 多写）。
  **DuckDB 单文件仅允许一个 read-write 进程**，故禁用 `multiprocessing.Pool`（会触发
  `IOException: Could not set lock`）。与 `ods_daily` fetch 的多线程写模式一致。
  墙钟记 `ods_etl_log`（观测项，非硬 KPI）。
```

- [ ] **Step 8: 更新 spec §12.7 — CALC_WORKERS 语义**

将 `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` §12.7 表格中：

```
| `CALC_WORKERS` | 多进程并行度，默认 `min(cpu-1, 8)` |
```

替换为：

```
| `CALC_WORKERS` | 计算**线程**并行度，默认 `min(cpu-1, 8)`（DuckDB 单文件禁跨进程写，故用线程池） |
```

- [ ] **Step 9: 最终全量测试 + 收尾**

Run: `pytest tests/ -q`
Expected: PASS

---

## Self-Review

**Spec coverage:**
- P0-1（锁冲突）→ Task 1 Step 5（ThreadPoolExecutor）✅
- P1-1（行数虚高）→ Task 2 ✅
- P2-3（CALC_WORKERS 非法值）→ Task 1 Step 1-3 ✅
- 测试改造（Pool mock 失效）→ Task 3 ✅
- 实库验收 + 文档 → Task 4 ✅
- P1-2 / P2-1 / P2-2 → 明确延后（Scope 段已声明）

**Placeholder scan:** 无 TBD/TODO；每个代码步骤含完整替换代码与精确行号。

**Type consistency:**
- `_count_calc_rows(con, table, calc_date, ts_codes)` 定义（Task 2 Step 3）与调用（Task 2 Step 4）签名一致。
- `set_run_id(run_id)` 作 `ThreadPoolExecutor(initializer=set_run_id, initargs=(rid,))`，单参数匹配。
- `_calc_stock_chunk(chunk, calc_date, incremental)` 签名未变，`executor.map(fn, chunks, [calc_date]*n, [incr]*n)` 逐位对应。
- 删除 `_calc_worker_init` 后，`import multiprocessing` 仍被 `resolve_calc_workers` 的 `multiprocessing.cpu_count()` 使用，保留。

**风险点（执行者注意）：**
- DuckDB 多线程并发 `INSERT OR REPLACE` 到同一 DWS 表：各 chunk 处理**互不相交的 ts_code**，行级无冲突；`ods_daily` 3 线程写已验证可行。若实库偶发 `TransactionContext` 冲突，可临时 `CALC_WORKERS=1` 串行确认数据正确，再排查。
- `run_checkpoint(con)` 在 executor 退出（所有线程连接已关闭）后执行，避免 WAL 锁竞争。
