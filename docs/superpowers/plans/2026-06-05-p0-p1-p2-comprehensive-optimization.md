# P0+P1+P2 综合优化方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** P0 批量化 `check_data_completeness`（5524 SQL→1），P1 批量化 `_compute_fetch_range`（~16500 SQL→~400），P2 多线程 calc（58min→~20min）。三项合计省 ~30 分钟 + ~70 秒固定开销。

**Architecture:** P0+P1 纯 SQL 重构（不改函数签名）。P2 在 `run_calc` 中反转循环顺序（股票分片→全计算器），每线程独立 DuckDB 连接写入 WAL。

**Tech Stack:** Python 3.9, DuckDB WAL, ThreadPoolExecutor, 现有 Calculator 不改

**效率预估：** 总 calc 从 ~58min → ~20min（P2）+ ~70s 消除（P0+P1）。

---

## 文件改动清单

| 文件 | 改动类型 | 职责 |
|------|:----:|------|
| `backend/etl/orchestrator.py` | 修改 | P0: batch `check_data_completeness`；P1: batch `_compute_fetch_range`；P2: 多线程 `run_calc` |
| `tests/test_etl/test_orchestrator.py` | 修改 | +批量化测试 |

---

### P0: 批量 `check_data_completeness`

**Files:**
- Modify: `backend/etl/orchestrator.py:225-241`

**现状：** 5524 次 `SELECT COUNT(*), MIN, MAX FROM dwd_daily_quote WHERE ts_code = ?`

**改为：** 1 次 `SELECT ts_code, COUNT(*), MIN, MAX FROM dwd_daily_quote WHERE ts_code IN (...) GROUP BY ts_code`，再遍历补全缺失股票。

```python
def check_data_completeness(con, ts_codes: list[str],
                             min_daily_rows: int = WARMUP_TDAYS) -> dict:
    ok = []
    missing = {}

    if not ts_codes:
        return {"ok": ok, "missing": missing}

    # One batch query instead of per-stock loop
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

**收益：** 5524 SQL → 1 SQL，省 ~30s/次。

---

### P1: 批量 `_compute_fetch_range`

**Files:**
- Modify: `backend/etl/orchestrator.py:245-297`（`_compute_fetch_range`）+ 调用处

**现状：** 每个股票 3-4 次独立 SQL（dim_stock + dim_date ROW_NUMBER + ods_daily coverage + dim_date expected）。

**改为：** 批量 dim_stock 查询 + 共享 needed_start（活跃股相同 range）+ 批量 ODS 覆盖率查询。

```python
def _compute_fetch_ranges_batch(con, ts_codes: list[str], calc_date: str,
                                 lookback_tdays: int = WARMUP_TDAYS) -> dict:
    """Batch version: compute fetch ranges for all stocks at once.

    Returns {ts_code: (needed_start, needed_end) or (None, None)}
    """
    result = {}

    # 1. Batch query dim_stock
    placeholders = ",".join(["?" for _ in ts_codes])
    stock_rows = con.execute(f"""
        SELECT ts_code, list_date, delist_date
        FROM dim_stock WHERE ts_code IN ({placeholders})
    """, ts_codes).fetchall()
    stock_info = {r[0]: (r[1], r[2]) for r in stock_rows}

    # 2. Shared needed_start for active stocks (same end_date, same lookback)
    shared_start = con.execute("""
        SELECT trade_date FROM (
            SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
            FROM dim_date WHERE is_trade_day = 1 AND trade_date <= ?
        ) WHERE rn = ?
    """, (calc_date, lookback_tdays)).fetchone()
    shared_start = shared_start[0] if shared_start else None

    for ts_code in ts_codes:
        if ts_code not in stock_info:
            result[ts_code] = (None, None)
            continue

        list_date, delist_date = stock_info[ts_code]
        end_date = calc_date
        if delist_date and delist_date < calc_date:
            end_date = delist_date

        # For stocks with non-standard end_date, compute individual needed_start
        if end_date != calc_date:
            needed = con.execute("""
                SELECT trade_date FROM (
                    SELECT trade_date, ROW_NUMBER() OVER (
                        ORDER BY trade_date DESC) AS rn
                    FROM dim_date WHERE is_trade_day = 1 AND trade_date <= ?
                ) WHERE rn = ?
            """, (end_date, lookback_tdays)).fetchone()
            needed_start = needed[0] if needed else None
        else:
            needed_start = shared_start

        if not needed_start:
            result[ts_code] = (None, None)
            continue

        if list_date and list_date > needed_start:
            needed_start = list_date

        if end_date == calc_date and needed_start == shared_start:
            # Can use batch coverage check (below)
            result[ts_code] = (needed_start, end_date)
            continue

        # Individual coverage check for non-standard ranges
        actual = con.execute("""
            SELECT COUNT(DISTINCT trade_date) FROM ods_daily
            WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ?
        """, (ts_code, needed_start, end_date)).fetchone()[0]
        expected = con.execute("""
            SELECT COUNT(*) FROM dim_date
            WHERE is_trade_day = 1 AND trade_date >= ? AND trade_date <= ?
        """, (needed_start, end_date)).fetchone()[0]
        if actual > 0 and actual >= expected:
            result[ts_code] = (None, None)
        else:
            result[ts_code] = (needed_start, end_date)

    # 3. Batch ODS coverage check for shared-range stocks
    shared_codes = [c for c, (s, e) in result.items()
                    if s is not None and e == calc_date and s == shared_start]
    if shared_codes and shared_start:
        placeholders2 = ",".join(["?" for _ in shared_codes])
        cov_rows = con.execute(f"""
            SELECT ts_code, COUNT(DISTINCT trade_date) AS n
            FROM ods_daily
            WHERE ts_code IN ({placeholders2})
            AND trade_date >= ? AND trade_date <= ?
            GROUP BY ts_code
        """, (*shared_codes, shared_start, calc_date)).fetchall()
        cov_map = {r[0]: r[1] for r in cov_rows}

        expected_shared = con.execute("""
            SELECT COUNT(*) FROM dim_date
            WHERE is_trade_day = 1 AND trade_date >= ? AND trade_date <= ?
        """, (shared_start, calc_date)).fetchone()[0]

        for ts_code in shared_codes:
            actual = cov_map.get(ts_code, 0)
            if actual >= expected_shared:
                result[ts_code] = (None, None)  # fully covered

    return result
```

**收益：** ~16500 SQL → ~400 SQL，省 ~40s/次。批量查询活跃股（95%+），少数特殊股票保持单查。

---

### P2: 多线程 calc

**Files:**
- Modify: `backend/etl/orchestrator.py` — `run_calc` 函数

**设计：** 反转循环顺序。原来"计算器 → 频率 → 股票"，改为"股票分片 → 每线程跑全计算器"。

```python
def _calc_stock_chunk(ts_codes: list[str], calc_date: str):
    """Per-thread: run ALL calculators for one chunk of stocks."""
    con = duckdb.connect(DUCKDB_PATH)
    try:
        grand_total = 0
        for CalcCls in CALCULATORS:
            for freq in ("daily", "weekly"):
                calc = CalcCls(con, freq)
                result = calc.calculate(ts_codes, calc_date)
                grand_total += result.calculated
        return grand_total
    finally:
        con.close()


def run_calc(con, ts_codes=None, auto_fetch=True, batch_size=100,
             workers=3):
    """Execute DWS computation — multi-threaded stock chunking."""
    ...
    # After DWD rebuild + completeness check
    codes_to_calc = completeness["ok"]

    # Split stocks into worker chunks
    chunk_size = max(1, len(codes_to_calc) // workers)
    chunks = [codes_to_calc[i:i + chunk_size]
              for i in range(0, len(codes_to_calc), chunk_size)]

    logger.info("calc %d stocks with %d threads (%d stocks/thread)",
                len(codes_to_calc), workers, chunk_size)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_calc_stock_chunk, chunk, calc_date)
                   for chunk in chunks]
        for f in futures:
            f.result()  # wait for all + propagate exceptions

    logger.info("calc ALL DONE — %d stocks", len(codes_to_calc))
```

**收益：** 3 线程并行，58min → ~20min。DuckDB WAL 模式天然支持并发写入。

**风险缓解：** 6 个 Calculator × 3 线程 = 18 个并发连接。DuckDB WAL 模式 ≤ 64 并发安全。已测试过 3 线程 fetch 无问题。

---

## 实施顺序

| 顺序 | 内容 | 预估时间 | 依赖 |
|:--:|------|:--:|------|
| 1 | P0: `check_data_completeness` 批量 | 30min | 无 |
| 2 | P1: `_compute_fetch_range` 批量 | 45min | 无 |
| 3 | P2: 多线程 calc | 45min | P0+P1 完成更安全 |

---

## 最终效果

| 阶段 | 耗时 | 较最初 |
|------|------|:--:|
| 最初（单线程 stock-batched） | ~5.5h（且漏拉 90%） | — |
| 当前（全部优化后） | ~58min | 5.7x |
| P0+P1 后 | ~57min | +~70s |
| P2 后 | **~20min** | **16.5x** |
