# P0 + P2 最终实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** P0 批量 `check_data_completeness`（5524 SQL→1）+ P2 多线程 calc（58min→~20min）。合计省 ~38 分钟 + ~30 秒固定开销。

**Architecture:** P0 纯 SQL 重构（不改函数签名）。P2 在 `run_calc` 中反转循环：股票分 N 片 → 每线程串行跑全部 Calculator → WAL 并发写入。

**Tech Stack:** Python 3.9, DuckDB WAL, ThreadPoolExecutor, concurrent.futures

**效率预估：** 总 calc 从 ~58min → ~20min（3 线程）。

---

## 文件改动清单

| 文件 | 改动类型 | 职责 |
|------|:----:|------|
| `backend/etl/orchestrator.py` | 修改 | P0: `check_data_completeness` 批量；P2: 多线程 `run_calc` |
| `tests/test_etl/test_orchestrator.py` | 修改 | +批量查询测试 |

---

### Task 1: P0 — `check_data_completeness` 批量查询

**Files:**
- Modify: `backend/etl/orchestrator.py:222-241`

#### 1.1 RED — 写测试

- [ ] **Step 1: 追加测试到 `tests/test_etl/test_orchestrator.py`**

```python
def test_check_data_completeness_batch_results():
    """Batch version should produce same results as per-stock version."""
    import duckdb
    from backend.etl.orchestrator import check_data_completeness

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT)""")
    # A.SZ: 260 rows (>= 250, OK)
    # B.SZ: 100 rows (< 250, missing)
    # C.SZ: 0 rows (not in DWD at all)
    for i in range(260):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('A.SZ', ?)",
                    (f"2026{i//12:02d}{i%12+1:02d}",))
    for i in range(100):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('B.SZ', ?)",
                    (f"2026{i//12:02d}{i%12+1:02d}",))

    result = check_data_completeness(
        con, ["A.SZ", "B.SZ", "C.SZ"], min_daily_rows=250)

    assert result["ok"] == ["A.SZ"]
    assert "B.SZ" in result["missing"]
    assert result["missing"]["B.SZ"]["dwd_rows"] == 100
    assert "C.SZ" in result["missing"]
    assert result["missing"]["C.SZ"]["dwd_rows"] == 0

    con.close()
```

- [ ] **Step 2: 运行确认通过（旧版也能通过）**

```bash
pytest tests/test_etl/test_orchestrator.py::test_check_data_completeness_batch_results -v
# 预期: PASS
```

- [ ] **Step 3: 替换 `check_data_completeness`**

将 [orchestrator.py:222-241](backend/etl/orchestrator.py#L222-L241) 的 `check_data_completeness` 函数体替换为：

```python
    ok = []
    missing = {}

    if not ts_codes:
        return {"ok": ok, "missing": missing}

    # Batch query: one GROUP BY instead of per-stock loop
    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(f"""
        SELECT ts_code, COUNT(*), MIN(trade_date), MAX(trade_date)
        FROM dwd_daily_quote WHERE ts_code IN ({placeholders})
        GROUP BY ts_code
    """, ts_codes).fetchall()

    dwd_data = {r[0]: {"dwd_rows": r[1], "min_date": r[2], "max_date": r[3]}
                for r in rows}

    for ts_code in ts_codes:
        info = dwd_data.get(ts_code)
        if info is not None and info["dwd_rows"] >= min_daily_rows:
            ok.append(ts_code)
        else:
            missing[ts_code] = {
                "dwd_rows": info["dwd_rows"] if info else 0,
                "min_date": info["min_date"] if info else None,
                "max_date": info["max_date"] if info else None,
            }

    return {"ok": ok, "missing": missing}
```

- [ ] **Step 4: 运行全部 orchestrator 测试**

```bash
pytest tests/test_etl/test_orchestrator.py -v
# 预期: 全部 PASS（含新测试）
```

- [ ] **Step 5: 提交**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "perf: batch check_data_completeness — 5524 SQL -> 1 GROUP BY

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: P2 — 多线程 calc

**Files:**
- Modify: `backend/etl/orchestrator.py:542-581`（`run_calc` 的 calc 段）

#### 2.1 设计确认

不改变 `run_calc` 的前半部分（退市过滤 → 完整度检查 → auto-fetch → DWD 重建）。只改后半部分的计算循环。

```python
# 改前（line 542-581）:
for CalcCls in CALCULATORS:          # sequential
    for freq in ("daily", "weekly"):
        for batch in batches:        # 每批 100 只
            calc.calculate(batch)

# 改后:
# 股票分 3 片，每片跑全部 Calculator（并行）
```

每个线程函数：
```python
def _calc_stock_chunk(chunk, calc_date, chunk_id):
    con = duckdb.connect(DUCKDB_PATH)
    try:
        for CalcCls in CALCULATORS:
            for freq in ("daily", "weekly"):
                calc = CalcCls(con, freq)
                for i in range(0, len(chunk), batch_size):
                    calc.calculate(chunk[i:i+batch_size], calc_date)
    finally:
        con.close()
```

#### 2.2 RED — 写测试（无法做传统 TDD）

多线程测试在内存 DB 中不实际（WAL 多连接需要文件系统）。**采用代码审查 + 端到端验证**。

#### 2.3 GREEN — 实现

- [ ] **Step 1: 在 `run_calc` 文件中添加 `_calc_stock_chunk` 函数**

在 `run_calc` 之前（或作为模块级函数）添加：

```python
def _calc_stock_chunk(chunk: list[str], calc_date: str) -> int:
    """Worker: run all calculators for one stock chunk in a dedicated connection."""
    import duckdb
    from backend.config import DUCKDB_PATH
    from backend.etl.error_handler import _write_skip_log_batch

    con = duckdb.connect(DUCKDB_PATH)
    try:
        chunk_total = 0
        for CalcCls in CALCULATORS:
            indicator_name = CalcCls.__name__.replace("Calculator", "").lower()
            for freq in ("daily", "weekly"):
                calc = CalcCls(con, freq)
                agg_result = CalcResult()

                for i in range(0, len(chunk), 100):
                    batch = chunk[i:i + 100]
                    batch_result = calc.calculate(batch, calc_date)
                    agg_result.calculated += batch_result.calculated
                    for reason, items in batch_result.skipped.items():
                        for ts_code, detail in items:
                            agg_result.add_skip(reason, ts_code, detail)

                _write_skip_log_batch(con, calc_date, indicator_name, freq,
                                      agg_result.skipped)

                n = con.execute(
                    f"SELECT COUNT(*) FROM {calc.dws_table} "
                    f"WHERE calc_date = ?", (calc_date,),
                ).fetchone()[0]
                chunk_total += n

                skip_parts = []
                for reason in SkipReason:
                    items = agg_result.skipped.get(reason, [])
                    if items:
                        skip_parts.append(f"{reason.value}={len(items)}")
                skip_str = ", ".join(skip_parts) if skip_parts else "none skipped"
                logger.info(
                    "calc %-30s DONE — %d rows (%d calculated), %s",
                    f"{CalcCls.__name__} {freq}", n,
                    agg_result.calculated, skip_str,
                )
        return chunk_total
    finally:
        con.close()
```

- [ ] **Step 2: 替换 `run_calc` 第 542-582 行（calc 段）**

```python
    # 4. 计算 DWS — multi-threaded by stock chunk
    import concurrent.futures
    WORKERS = 3
    chunk_size = max(1, (len(codes_to_calc) + WORKERS - 1) // WORKERS)
    chunks = [codes_to_calc[i:i + chunk_size]
              for i in range(0, len(codes_to_calc), chunk_size)]

    logger.info("calc %d stocks with %d threads (%d stocks/thread)",
                len(codes_to_calc), WORKERS, chunk_size)

    lid, t0 = log_etl_start(con, "calc_dws")
    grand_total = 0
    calc_start = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_calc_stock_chunk, chunk, calc_date)
                   for chunk in chunks]
        for f in concurrent.futures.as_completed(futures):
            grand_total += f.result()

    total_elapsed = time.monotonic() - calc_start
    logger.info("calc ALL DONE — %d total DWS rows across %d indicator×freq pairs, %.0fs",
                grand_total, len(CALCULATORS) * 2, total_elapsed)
    log_etl_end(con, lid, "calc_dws", t0, "success", row_count=grand_total)
```

- [ ] **Step 3: 运行全量测试**

```bash
pytest tests/ --tb=short
# 预期: 新增 PASS，既有 4 个失败不受影响
```

- [ ] **Step 4: 提交**

```bash
git add backend/etl/orchestrator.py
git commit -m "feat: multi-threaded DWS calc — 3 threads, ~3x speedup

Invert loop order: stock chunks parallelized, each thread runs all
calculators sequentially on its chunk. DuckDB WAL allows concurrent
writers. Uses ThreadPoolExecutor, same pattern as fetch_by_date_range_parallel.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 全量回归 + CLAUDE.md

- [ ] **Step 1: 运行全量测试**

```bash
pytest tests/ -v --tb=short
# 预期: 既有 4 个失败不变，0 新增失败
```

- [ ] **Step 2: 更新 CLAUDE.md**

在 `calc` 段描述中更新多线程信息，并在已知问题追加：

```markdown
- **多线程 calc:** `run_calc` 将股票分 3 片并行计算，WAL 并发写入。
  每线程独立 DuckDB 连接，跑全部 12 个 indicator×freq 组合。
  总 calc 从 ~58min → ~20min（3 线程）。
```

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: document batch completeness check + multi-threaded calc

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 端到端验证

- [ ] **Step 1: 运行 run 命令**

```bash
python -m backend.cli run
```

关注日志：
```
calc N stocks with 3 threads (M stocks/thread)
```

- [ ] **Step 2: 验证总耗时**

```bash
# 预期: "calc ALL DONE" < 1200s（~20min）
```

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "chore: end-to-end verification — P0+P2"
```

---

## 自检

- ✅ P0 函数签名不变，返回结构不变
- ✅ P0 边界情况：股票无 DWD 数据 → `dwd_data.get()` 返回 None → missing 记录
- ✅ P2 使用 `ThreadPoolExecutor`，同已有 `fetch_by_date_range_parallel` 模式
- ✅ P2 每线程独立 `duckdb.connect()`，WAL 安全
- ✅ P2 并发写同一 `calc_date`，PK 幂等保证安全
- ✅ `run_etl`（legacy）不受影响
- ✅ Calculator 不感知多线程（每个 Calculator 实例单连接单线程）
