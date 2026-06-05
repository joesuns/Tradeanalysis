# ETL 可观测性 + 增量拉取 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ETL ODS 拉取阶段输出实时进度日志 + 支持断点续传（跳过已入库日期），使 3 年全市场 ETL 在 10 分钟 timeout 约束下可多次重跑完成。

**Architecture:** 两处小改动，不改数据结构、不改 API 调用模式、不改计算逻辑。`fetch_by_date_range` 加进度日志 + 去重查询。odaily_basic 和 moneyflow 拉取独立检查各自的增量状态。

**Tech Stack:** Python 3.9, DuckDB, tushare, logging

---

## File Structure

| 文件 | 职责 |
|------|------|
| `backend/fetch/ods_daily.py` | 核心改动：`fetch_by_date_range` 增量检测 + 进度日志 |
| `backend/etl/orchestrator.py` | 可选：拆分 step 支持单独重跑 fetch-ods |

---

### Task 1: 增量检测 — 跳过已在 DB 的交易日期

**Files:**
- Modify: `backend/fetch/ods_daily.py:18-85`

- [ ] **Step 1: Write failing test**

```python
# tests/test_fetch/test_ods_daily.py (追加)

def test_fetch_skips_existing_dates():
    """fetch_by_date_range skips trade_dates already present in ods_daily."""
    import duckdb, tempfile, os
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd); os.unlink(path)
    con = duckdb.connect(path)

    # Create ods_daily with ONE existing date
    con.execute("""
        CREATE TABLE ods_daily (
            ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
            close REAL, vol REAL, amount REAL, pct_chg REAL, adj_factor REAL,
            fetched_at TEXT, PRIMARY KEY (ts_code, trade_date)
        )
    """)
    con.execute("INSERT INTO ods_daily VALUES ('000001.SZ','20260101',10,10.5,9.5,10,1000,10000,0,1,'now')")
    con.close()

    from backend.fetch.ods_daily import _get_new_trade_dates
    from unittest.mock import patch

    # With DB having 20260101, only 20260102+ should be fetched
    new_dates = _get_new_trade_dates(
        lambda: duckdb.connect(path),
        "20260101", "20260103", "ods_daily"
    )
    assert "20260101" not in new_dates, "Already-existing date should be skipped"
    assert "20260102" in new_dates
    assert "20260103" in new_dates
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_fetch/test_ods_daily.py::test_fetch_skips_existing_dates -v
```
Expected: FAIL — `_get_new_trade_dates` not defined

- [ ] **Step 3: Add `_get_new_trade_dates` helper + integrate into `fetch_by_date_range`**

In `backend/fetch/ods_daily.py`, add before `fetch_by_date_range`:

```python
def _get_new_trade_dates(con_factory, start: str, end: str, table: str) -> list:
    """Return trade_dates in [start, end] NOT already present in table."""
    import duckdb
    con = duckdb.connect(con_factory() if callable(con_factory) else con_factory)
    # DuckDB connect from path — use a fresh read-only connection
    from backend.config import DUCKDB_PATH
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    existing = set(
        row[0] for row in con.execute(
            f"SELECT DISTINCT trade_date FROM {table} "
            "WHERE trade_date >= ? AND trade_date <= ?",
            (start, end)
        ).fetchall()
    )
    con.close()

    from datetime import datetime, timedelta
    all_dates = []
    d = datetime.strptime(start, "%Y%m%d")
    end_d = datetime.strptime(end, "%Y%m%d")
    while d <= end_d:
        ds = d.strftime("%Y%m%d")
        if ds not in existing:
            all_dates.append(ds)
        d += timedelta(days=1)

    if len(existing) > 0:
        logger.info(
            "Skipping %d already-existing dates in %s (%s~%s), "
            "%d new dates to fetch",
            len(existing), table, start, end, len(all_dates)
        )
    return all_dates
```

Then modify `fetch_by_date_range` to use it. Change line 24-27:

```python
# Before:
def fetch_by_date_range(client, con, start: str, end: str) -> int:
    ...
    days = []
    d = datetime.strptime(start, "%Y%m%d")
    end_d = datetime.strptime(end, "%Y%m%d")
    while d <= end_d:
        days.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)

# After:
def fetch_by_date_range(client, con, start: str, end: str) -> int:
    ...
    days = _get_new_trade_dates(con, start, end, "ods_daily")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_fetch/test_ods_daily.py::test_fetch_skips_existing_dates -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/ods_daily.py tests/test_fetch/test_ods_daily.py
git commit -m "feat: ETL ODS fetch skips already-existing trade dates (incremental)"
```

---

### Task 2: 进度日志 — 每 20 个交易日输出进度百分比

**Files:**
- Modify: `backend/fetch/ods_daily.py:28-82`

- [ ] **Step 1: No new test needed** — this is an observability-only change (logging). Verified by manual inspection of log output during ETL run.

- [ ] **Step 2: Add progress logging to `fetch_by_date_range` loop**

In `fetch_by_date_range`, add import at top:

```python
import time
```

Inside the per-date loop (after `for i, trade_date in enumerate(days):`), add at the start of each iteration:

```python
    for i, trade_date in enumerate(days):
        # Progress logging every 20 days or on first/last
        if i == 0 or i == len(days) - 1 or (i + 1) % 20 == 0:
            elapsed = time.time() - t0 if 't0' in dir() else 0
            pct = (i + 1) / len(days) * 100
            rate = (i + 1) / elapsed * 60 if elapsed > 0 else 0
            eta = (len(days) - i - 1) / rate if rate > 0 else 0
            logger.info(
                "ODS fetch: %d/%d days (%d%%) | elapsed=%.0fs | "
                "rate=%.1f days/min | eta=%.0fs",
                i + 1, len(days), pct, elapsed, rate, eta
            )
```

And initialize timer before the loop:

```python
    t0 = time.time()
    total_rows = 0
    for i, trade_date in enumerate(days):
        # ... progress logging ...
```

- [ ] **Step 3: Verify — run a small fetch and confirm logs appear**

```bash
python -m backend.cli etl --step fetch-ods --start 20260601 --end 20260603 2>&1 | grep "ODS fetch"
```
Expected: output like `ODS fetch: 1/3 days (33%) | ...`

- [ ] **Step 4: Commit**

```bash
git add backend/fetch/ods_daily.py
git commit -m "feat: add progress logging to ODS fetch (every 20 trading days)"
```

---

### Task 3: 拆分 ETL step — 支持 fetch-ods 单独重跑不触发超时

**Files:**
- Modify: `backend/etl/orchestrator.py:104-113`

- [ ] **Step 1: No code change needed** — `--step fetch-ods` 已支持单独跑取数阶段。直接验证:

```bash
python -m backend.cli etl --step fetch-ods --start 20230601 --end 20260603
```

如果 ODS 已全部入库，增量检测会跳过所有日期，输出 `Skipping N already-existing dates` 并秒级完成。

- [ ] **Step 2: 文档化分步执行流程**

在输出中引导用户分步执行：

```
# Step 1: 拉取 ODS（可多次重跑，增量跳过已拉取数据）
python -m backend.cli etl --step fetch-ods --start 20230601 --end 20260603

# Step 2: 构建 DIM + DWD + DWS（纯计算，batch INSERT 优化后 5-10 分钟）
python -m backend.cli etl --step build-all --start 20230601 --end 20260603
# 注：build-all 包含 fetch-ods，但 ODS 已就绪时会秒级跳过
```

---

## 验证清单

- [ ] `test_fetch_skips_existing_dates` 通过
- [ ] 增量 ETL：两次连续运行 `fetch-ods`，第二次应跳过所有日期（0 天拉取）
- [ ] 进度日志：运行时可见 `ODS fetch: X/727 days` 格式日志
- [ ] 全量测试：`pytest tests/ -q` 无新增失败
