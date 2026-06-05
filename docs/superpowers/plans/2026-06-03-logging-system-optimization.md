# Logging System Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all logs persistent (file rotation), traceable (module/line/stacktrace), and complete (no silent failures in API/DIM/DWD/DWS layers), while adding ETL step duration tracking.

**Architecture:** Extract logging config from CLI into a shared `backend/log_config.py` module. Use `RotatingFileHandler` for persistent logs + stderr for dev. Add FastAPI middleware for request/response logging. Wrap orchestrator DIM/DWD/DWS in try/except with `log_etl("failed")`. Fix `started_at`/`finished_at` to capture real duration.

**Tech Stack:** Python `logging` stdlib (`RotatingFileHandler`), FastAPI middleware, existing DuckDB `ods_etl_log` table.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `backend/log_config.py` | **Create** | Shared `setup_logging()` — handlers, format, level. Called by CLI + scripts. |
| `backend/cli.py:17-20` | Modify | Replace inline `basicConfig` with `setup_logging()` |
| `backend/api/app.py` | Modify | Add logging middleware for request/response/error/latency |
| `backend/etl/error_handler.py` | Modify | Fix `started_at`/`finished_at` timing, add `log_etl_start()` / `log_etl_end()`, use `logger.exception()` |
| `backend/etl/orchestrator.py` | Modify | Wrap DIM/DWD/DWS in try/except, call `check_data_completeness()`, use new start/end API |
| `fetch_stocks.py` | Modify | Call `setup_logging()`, replace `print()` with `logger.info()` |
| `backend/db/schema.py:103-112` | Modify | Add indexes on `ods_etl_log(step_name, started_at)` |
| `tests/test_log_config.py` | **Create** | Test log file creation, format, rotation |

---

### Task 1: Create shared logging configuration module

**Files:**
- Create: `backend/log_config.py`
- Modify: `backend/config.py:11` (add `LOG_FILE`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`)

- [ ] **Step 1: Add new config vars to `backend/config.py`**

```python
# backend/config.py — append after line 12
LOG_FILE = os.getenv("LOG_FILE", "./data/tradeanalysis.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
```

- [ ] **Step 2: Write `backend/log_config.py`**

```python
"""Shared logging configuration — file rotation + stderr, structured format."""

import logging
from logging.handlers import RotatingFileHandler
import os
from backend.config import LOG_FILE, LOG_LEVEL, LOG_MAX_BYTES, LOG_BACKUP_COUNT

_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def setup_logging(name: str = None) -> logging.Logger:
    """Configure root logger with file rotation + stderr.

    Idempotent — only adds handlers once. Call at entry points (CLI, scripts).
    Returns a logger for the given name (or root if name is None).
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Prevent duplicate handlers on repeated calls
    if root.handlers:
        return logging.getLogger(name) if name else root

    fmt = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    # File handler with rotation
    os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Stderr handler (for dev visibility)
    sh = logging.StreamHandler()
    sh.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    sh.setFormatter(fmt)
    root.addHandler(sh)

    return logging.getLogger(name) if name else root
```

- [ ] **Step 3: Verify module imports cleanly**

Run: `python3 -c "from backend.log_config import setup_logging; print('OK')"`
Expected: `OK` (no errors)

- [ ] **Step 4: Commit**

```bash
git add backend/log_config.py backend/config.py
git commit -m "feat: add shared logging config with RotatingFileHandler + stderr

- New backend/log_config.py: setup_logging() with dual handlers
- RotatingFileHandler writes to ./data/tradeanalysis.log (10MB x 5)
- Stderr handler for dev visibility
- New config vars: LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT
- Format includes module name and ISO8601 timestamp

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Wire CLI to use `setup_logging()`

**Files:**
- Modify: `backend/cli.py:17-20`

- [ ] **Step 1: Replace inline `basicConfig` with `setup_logging()`**

```python
# backend/cli.py — replace lines 17-19 with:
from backend.log_config import setup_logging

setup_logging()
```

Remove the old block:
```python
# DELETE these lines:
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
```

Also remove the now-unused import:
```python
# Remove: import logging (if only used for basicConfig)
# Remove: from backend.config import LOG_LEVEL (if only used for basicConfig)
```

Check if `logging` and `LOG_LEVEL` are still used elsewhere in cli.py. They aren't — remove both.

- [ ] **Step 2: Verify CLI still works**

Run: `python3 -m backend.cli check 2>&1`
Expected: DuckDB + tushare connectivity info with new format: `2026-06-03T... INFO     [backend.db.connection] ...`

- [ ] **Step 3: Verify log file is created**

Run: `ls -lh data/tradeanalysis.log`
Expected: file exists, non-empty

- [ ] **Step 4: Commit**

```bash
git add backend/cli.py
git commit -m "refactor: CLI uses shared setup_logging() instead of inline basicConfig

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Fix `ods_etl_log` duration tracking and add `logger.exception()`

**Files:**
- Modify: `backend/etl/error_handler.py` (full rewrite)

- [ ] **Step 1: Rewrite `error_handler.py` with start/end API**

```python
"""ETL error grading and audit logging.

Provides:
    log_etl_start()  — INSERT a "running" row, return (log_id, start_time)
    log_etl_end()    — UPDATE the row with duration, status, row_count, error_msg
    check_data_completeness() — compare ODS table freshness
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def log_etl_start(con, step_name: str) -> tuple[str, float]:
    """Insert a 'running' row into ods_etl_log. Returns (log_id, start_time_monotonic).

    Use with log_etl_end() to capture real wall-clock duration.
    """
    log_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg, data_completeness)
           VALUES (?, ?, ?, '', 'running', 0, '', '')""",
        (log_id, step_name, now_iso),
    )
    logger.info(f"ETL {step_name} — started")
    return log_id, time.monotonic()


def log_etl_end(con, log_id: str, step_name: str, start_time: float,
                status: str, row_count: int = 0, error_msg: str = "",
                data_completeness: Optional[dict] = None):
    """Finalize an ETL step with duration, status, and optional error/audit data.

    If status is 'failed' or 'degraded', also emits a warning log.
    """
    duration_ms = round((time.monotonic() - start_time) * 1000)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    comp = json.dumps(data_completeness) if data_completeness else ""

    con.execute(
        """UPDATE ods_etl_log
           SET finished_at = ?, status = ?, row_count = ?, error_msg = ?,
               data_completeness = ?
           WHERE id = ?""",
        (now_iso, status, row_count, error_msg or "", comp, log_id),
    )

    if status in ("failed", "degraded"):
        logger.warning(f"ETL {step_name}: {status} ({duration_ms}ms) — {error_msg}")
    else:
        logger.info(f"ETL {step_name}: {status} ({duration_ms}ms, {row_count} rows)")


def log_etl_error(con, log_id: str, step_name: str, start_time: float,
                  row_count: int, exception: Exception):
    """Convenience: log a step as 'failed' with full traceback."""
    import traceback
    tb = traceback.format_exc()
    logger.exception(f"ETL {step_name} — FAILED")
    log_etl_end(
        con, log_id, step_name, start_time, "failed",
        row_count=row_count, error_msg=f"{type(exception).__name__}: {exception}\n{tb[-500:]}",
    )


def check_data_completeness(con) -> dict:
    """Check that ODS tables are all at the same latest trade_date.

    Returns a dict mapping table name -> max trade_date (or None if empty).
    """
    tables = ["ods_daily", "ods_daily_basic", "ods_moneyflow"]
    result = {}
    for t in tables:
        row = con.execute(f"SELECT MAX(trade_date) FROM {t}").fetchone()
        result[t] = row[0] if row and row[0] else None
    return result
```

- [ ] **Step 2: Verify imports**

Run: `python3 -c "from backend.etl.error_handler import log_etl_start, log_etl_end, log_etl_error; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backend/etl/error_handler.py
git commit -m "refactor: log_etl with start/end API for real duration tracking

- log_etl_start() inserts 'running' row, returns (log_id, monotonic)
- log_etl_end() updates row with finished_at, duration_ms in log message
- log_etl_error() captures full traceback via logger.exception()
- All timestamps now in UTC ISO8601

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Add try/except and duration tracking to orchestrator

**Files:**
- Modify: `backend/etl/orchestrator.py:32-118` (rewrite `run_etl` body)
- Modify: `backend/etl/orchestrator.py:7` (remove dead `logger`)

- [ ] **Step 1: Rewrite `run_etl` to use new `log_etl_start/end` and wrap all steps**

The full function body replaces lines 50-118. Key changes:
- Remove the dead `logger` at line 26 (never used)
- Wrap DIM/DWD/DWS in try/except with `log_etl_error()`
- Call `check_data_completeness()` after fetch step
- Use `log_etl_start()` / `log_etl_end()` for every step

```python
# backend/etl/orchestrator.py — replace import at line 7 and body from line 48

from backend.etl.error_handler import (
    log_etl_start, log_etl_end, log_etl_error, check_data_completeness,
)


def run_etl(step: str = "build-all", ts_codes: Optional[list[str]] = None,
            start: Optional[str] = None, end: Optional[str] = None,
            batch_size: int = 100, force_full: bool = False):
    """Run the ETL pipeline."""
    con = get_connection()
    try:
        # 0. Self-check
        health = check_connectivity()
        lid, t0 = log_etl_start(con, "health_check")
        if "fatal" in health.get("duckdb", ""):
            log_etl_end(con, lid, "health_check", t0, "failed",
                        error_msg=health["duckdb"])
            raise RuntimeError(health["duckdb"])
        log_etl_end(con, lid, "health_check", t0, "success",
                    error_msg=f"DuckDB v{health['version']}, "
                              f"{health['disk_free_mb']}MB free")

        # 1. Fetch ODS
        if step in ("fetch-ods", "build-all"):
            client = TushareClient()

            from backend.fetch.ods_stock_basic import fetch_stock_basic
            from backend.fetch.ods_trade_cal import fetch_trade_cal
            from backend.fetch.ods_concept import fetch_concept_detail

            lid, t0 = log_etl_start(con, "fetch_stock_basic")
            n = fetch_stock_basic(client, con)
            log_etl_end(con, lid, "fetch_stock_basic", t0, "success", row_count=n)

            lid, t0 = log_etl_start(con, "fetch_trade_cal")
            n = fetch_trade_cal(client, con)
            log_etl_end(con, lid, "fetch_trade_cal", t0, "success", row_count=n)

            codes = ts_codes or get_all_active_codes(con)

            lid, t0 = log_etl_start(con, "fetch_market_data")
            rows = fetch_by_date_range_parallel(
                start or "20150101", end or "20991231", workers=3,
                ts_codes=ts_codes)
            log_etl_end(con, lid, "fetch_market_data", t0, "success", row_count=rows)

            lid, t0 = log_etl_start(con, "fetch_concept_detail")
            try:
                n = fetch_concept_detail(client, con, ts_codes=codes)
                log_etl_end(con, lid, "fetch_concept_detail", t0, "success", row_count=n)
            except Exception as e:
                log_etl_end(con, lid, "fetch_concept_detail", t0, "degraded",
                            error_msg=f"skipped (rate limited): {e}")

            # Run data completeness check after fetch
            comp = check_data_completeness(con)
            lid, t0 = log_etl_start(con, "data_completeness_check")
            log_etl_end(con, lid, "data_completeness_check", t0, "success",
                        data_completeness=comp)

        # 2. Build DIM — wrapped in try/except
        if step in ("build-dim", "build-all"):
            for dim_step, fn in [
                ("build_dim_stock", build_dim_stock),
                ("build_dim_date", build_dim_date),
                ("build_dim_concept", build_dim_concept),
            ]:
                lid, t0 = log_etl_start(con, dim_step)
                try:
                    if dim_step == "build_dim_concept":
                        nc, nm = fn(con)
                        n = nc + nm
                    else:
                        n = fn(con)
                    log_etl_end(con, lid, dim_step, t0, "success", row_count=n)
                except Exception as e:
                    log_etl_error(con, lid, dim_step, t0, 0, e)
                    raise

        # 3. Build DWD — wrapped in try/except
        if step in ("build-dwd", "build-all"):
            codes = ts_codes or get_all_active_codes(con)
            for dwd_step, fn in [
                ("build_dwd_daily_quote", build_dwd_daily_quote),
                ("build_dwd_weekly_quote", build_dwd_weekly_quote),
                ("build_dwd_daily_moneyflow", build_dwd_daily_moneyflow),
            ]:
                lid, t0 = log_etl_start(con, dwd_step)
                try:
                    n = fn(con, codes)
                    log_etl_end(con, lid, dwd_step, t0, "success", row_count=n)
                except Exception as e:
                    log_etl_error(con, lid, dwd_step, t0, 0, e)
                    raise

        # 4. Calc DWS — wrapped in try/except
        if step in ("calc-dws", "build-all"):
            codes = ts_codes or get_all_active_codes(con)
            lid, t0 = log_etl_start(con, "calc_dws")
            try:
                calc_date = datetime.now().strftime("%Y%m%d")
                total = 0
                for i in range(0, len(codes), batch_size):
                    batch = codes[i:i + batch_size]
                    for CalcCls in CALCULATORS:
                        for freq in ("daily", "weekly"):
                            calc = CalcCls(con, freq)
                            calc.calculate(batch, calc_date)
                    total += len(batch)
                log_etl_end(con, lid, "calc_dws", t0, "success", row_count=total)
            except Exception as e:
                log_etl_error(con, lid, "calc_dws", t0, 0, e)
                raise

        # Final checkpoint
        run_checkpoint(con)
    finally:
        con.close()
```

Also remove the dead `logger` at line 26:
```python
# Delete this line (line 26 of orchestrator.py):
logger = logging.getLogger(__name__)
```

And remove the old `from backend.etl.error_handler import log_etl, check_data_completeness` import (line 15) since we replaced it.

- [ ] **Step 2: Verify orchestrator imports**

Run: `python3 -c "from backend.etl.orchestrator import run_etl; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Quick smoke test**

Run: `python3 -c "
from backend.etl.orchestrator import run_etl
run_etl(step='build-dim', start='20250601')
print('DIM completed without error')
"`
Expected: DIM completes, no `log_etl` attribute errors

- [ ] **Step 4: Commit**

```bash
git add backend/etl/orchestrator.py
git commit -m "refactor: orchestrator with try/except, duration tracking, completeness check

- All DIM/DWD/DWS steps now wrapped in try/except with log_etl_error()
- check_data_completeness() called after fetch step — writes JSON to ods_etl_log
- log_etl_start/end API tracks real wall-clock duration per step
- Removed dead logger.getLogger that was never used

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Add FastAPI logging middleware

**Files:**
- Modify: `backend/api/app.py`

- [ ] **Step 1: Add request/response logging middleware to `backend/api/app.py`**

```python
"""FastAPI application with request logging middleware."""

import logging
import time
from fastapi import FastAPI, Request
from backend.api.router import router

logger = logging.getLogger(__name__)

app = FastAPI(title="TradeAnalysis API", version="1.0")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with method, path, status, and latency."""
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start) * 1000)
    logger.info(
        f"{request.method} {request.url.path} -> {response.status_code} "
        f"({duration_ms}ms)"
    )
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Log unhandled exceptions with full traceback."""
    logger.exception(
        f"Unhandled error: {request.method} {request.url.path} — {exc}"
    )
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


app.include_router(router)
```

- [ ] **Step 2: Start API and test log output**

Run: `uvicorn backend.api.app:app --port 8000 &`
Then: `curl http://localhost:8000/api/v1/health`
Check: `tail -5 data/tradeanalysis.log`

Expected lines like:
```
2026-06-03T... INFO     [backend.api.app] GET /api/v1/health -> 200 (12ms)
```

Kill uvicorn after: `kill %1`

- [ ] **Step 3: Commit**

```bash
git add backend/api/app.py
git commit -m "feat: add request logging middleware and global exception handler to API

- HTTP middleware logs method, path, status_code, latency_ms for every request
- Global exception_handler logs full traceback via logger.exception()
- All API activity now captured in tradeanalysis.log

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Add indexes on `ods_etl_log`

**Files:**
- Modify: `backend/db/schema.py` (add to `_INDEX_DDL` list near end of file)

- [ ] **Step 1: Add indexes to schema**

First find the `_INDEX_DDL` list in schema.py:

Run: `grep -n "_INDEX_DDL" backend/db/schema.py`

Then add these indexes:

```python
# Inside the _INDEX_DDL list in schema.py, add:
"""CREATE INDEX IF NOT EXISTS idx_etl_log_step ON ods_etl_log(step_name)""",
"""CREATE INDEX IF NOT EXISTS idx_etl_log_started ON ods_etl_log(started_at)""",
"""CREATE INDEX IF NOT EXISTS idx_etl_log_status ON ods_etl_log(status)""",
```

- [ ] **Step 2: Apply indexes**

Run: `python3 -c "
from backend.db.schema import create_all_tables
from backend.db.connection import get_connection
con = get_connection()
create_all_tables(con)
# Verify
idxs = con.execute(\"SELECT index_name FROM duckdb_indexes() WHERE table_name='ods_etl_log'\").fetchall()
for i in idxs:
    print(i[0])
con.close()
"`

Expected: three new index names printed

- [ ] **Step 3: Commit**

```bash
git add backend/db/schema.py
git commit -m "feat: add indexes on ods_etl_log(step_name, started_at, status)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Wire fetch_stocks.py to use `setup_logging()`

**Files:**
- Modify: `fetch_stocks.py`

- [ ] **Step 1: Replace `print()` calls with logger, add `setup_logging()`**

Replace the import section and add logger initialization:

```python
# fetch_stocks.py — add after existing imports:
from backend.log_config import setup_logging

logger = setup_logging("fetch_stocks")
```

Replace all `print()` calls:
- `print(f"⚠️  无法识别代码格式，跳过: {code}")` → `logger.warning(f"无法识别代码格式，跳过: {code}")`
- `print(f"📊 目标标的 ...")` → `logger.info(f"目标标的 ({len(ts_codes)} 只): {', '.join(ts_codes)}")`
- `print(f"📅 日期范围: ...")` → `logger.info(f"日期范围: {args.start} ~ {args.end or '今天'}")`
- `print(f"⚙️  ETL 步骤: {args.step}")` → `logger.info(f"ETL 步骤: {args.step}")`
- `print(f"\n✅ 完成！...")` → `logger.info(f"完成！{len(ts_codes)} 只标的数据已入库")`
- `print(f"\n📄 自动导出日期: {export_date}（日线+周线）")` → `logger.info(f"自动导出日期: {export_date}（日线+周线）")`
- `print(f"✅ 已导出 {n} 行")` → `logger.info(f"已导出 {n} 行")`
- `print(f"⚠️  日期 {trade_date} 没有这些股票的数据")` → `logger.warning(f"日期 {trade_date} 没有这些股票的数据")`
- `print("❌ 没有有效的股票代码")` → `logger.error("没有有效的股票代码")`
- `print("❌ 数据库中没有这些股票的数据，请先运行 ETL")` → `logger.error("数据库中没有这些股票的数据，请先运行 ETL")`
- `print(f"\n⚠️  未找到导出数据，请检查 ETL 是否成功")` → `logger.warning("未找到导出数据，请检查 ETL 是否成功")`

Remove the `print()` calls in the `fix_ts_code` function (lines 78-79, 93):
```python
# Change from:
print(f"⚠️  无法识别代码格式，跳过: {code}")
# To:
logger.warning(f"无法识别代码格式，跳过: {code}")
```

- [ ] **Step 2: Verify the script still runs**

Run: `python3 fetch_stocks.py --codes 000543.SZ,600580.SH --export-only 2>&1`
Expected: Timestamp-prefixed log lines (via stderr handler), no raw print output, export succeeds.

Also verify log file: `tail -5 data/tradeanalysis.log | grep fetch_stocks`
Expected: `fetch_stocks` module name in log lines

- [ ] **Step 3: Commit**

```bash
git add fetch_stocks.py
git commit -m "refactor: fetch_stocks.py uses setup_logging() instead of print()

- All output now timestamped, leveled (INFO/WARNING/ERROR), and written to log file
- Emoji prefixes removed (stdlib logging doesn't need them)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Integration test — full ETL pipeline with logging

**Files:**
- Create: `tests/test_log_config.py`

- [ ] **Step 1: Write the test**

```python
"""Smoke tests for the logging system — format, file output, rotation."""

import logging
import os
import tempfile
from unittest.mock import patch


def test_log_file_created():
    """setup_logging creates file handler writing to LOG_FILE."""
    from backend.log_config import setup_logging

    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "test.log")
        with patch("backend.log_config.LOG_FILE", log_path):
            # Clean: remove handlers added by prior tests
            root = logging.getLogger()
            root.handlers.clear()

            logger = setup_logging("test_module")
            logger.info("hello world")

            # Force flush by closing handlers
            for h in root.handlers:
                h.flush()
                h.close()
            root.handlers.clear()

            assert os.path.exists(log_path), f"Log file not created: {log_path}"
            content = open(log_path).read()
            assert "hello world" in content
            assert "[test_module]" in content


def test_etl_log_duration_tracking():
    """log_etl_start/end captures real wall-clock time."""
    from backend.db.connection import get_connection
    from backend.etl.error_handler import log_etl_start, log_etl_end
    import time

    con = get_connection()
    try:
        lid, t0 = log_etl_start(con, "test_step")
        time.sleep(0.1)
        log_etl_end(con, lid, "test_step", t0, "success", row_count=42)

        row = con.execute(
            "SELECT status, row_count, started_at, finished_at "
            "FROM ods_etl_log WHERE id = ?", (lid,)
        ).fetchone()

        assert row is not None, "Log row not found"
        assert row[0] == "success"
        assert row[1] == 42
        assert row[2] != "", "started_at should not be empty"
        assert row[3] != "", "finished_at should not be empty"
        assert row[2] != row[3], (
            f"started_at ({row[2]}) should differ from finished_at ({row[3]})"
        )

        # Clean up test row
        con.execute("DELETE FROM ods_etl_log WHERE id = ?", (lid,))
    finally:
        con.close()


def test_log_format_includes_module():
    """Log messages include [module_name] in the output."""
    import io
    import logging

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    stream = io.StringIO()
    from backend.log_config import _FORMAT
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.setLevel(logging.DEBUG)
    root.addHandler(handler)

    logger = logging.getLogger("my_custom_module")
    logger.info("test message")

    root.handlers.clear()

    output = stream.getvalue()
    assert "[my_custom_module]" in output, f"Module name missing from: {output}"
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_log_config.py -v`
Expected: 3 tests PASS

- [ ] **Step 3: Run existing tests to ensure no regression**

Run: `pytest tests/ -v`
Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_log_config.py
git commit -m "test: add logging system integration tests

- Log file creation and content verification
- ETL log start/end duration tracking
- Log format module name inclusion

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: End-to-end verification

- [ ] **Step 1: Run full ETL and inspect logs**

```bash
python3 -m backend.cli etl --step build-all --start 20250601 2>&1 | head -30
```

Expected: Timestamped, leveled output with module names. No raw `print()` output from backend modules.

- [ ] **Step 2: Inspect `ods_etl_log` for duration data**

```bash
python3 -c "
from backend.db.connection import get_connection
con = get_connection(read_only=True)
rows = con.execute('SELECT step_name, status, started_at, finished_at FROM ods_etl_log ORDER BY started_at DESC LIMIT 15').fetchall()
for r in rows:
    print(f'{r[0]:30s} {r[1]:10s}  {r[2][:19]} -> {r[3][:19]}')
con.close()
"
```

Expected: Different `started_at` and `finished_at` values, all statuses non-empty, `calc_dws` present.

- [ ] **Step 3: Inspect log file**

```bash
wc -l data/tradeanalysis.log && tail -10 data/tradeanalysis.log
```

Expected: Multiple lines, latest lines show the recent ETL run.

- [ ] **Step 4: Verify rotation works (optional)**

```bash
python3 -c "
from backend.log_config import setup_logging
import logging
logger = setup_logging('rotation_test')
for i in range(100000):
    logger.info(f'Fill line {i} to test rotation')
"
ls -lh data/tradeanalysis.log*
```

Expected: `tradeanalysis.log` exists, possibly `tradeanalysis.log.1` if >10MB.

- [ ] **Step 5: Final commit (if any fixes from verification)**

```bash
git add -A
git commit -m "chore: final verification of logging system optimization

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
