# 日志进度统一（Fetch / DWD / Calc / Export）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 ETL 主流水线（fetch → rebuild DWD → calc → export）的长时间 stderr 静默，用统一的 `StageProgress`（计数节流 + 时间心跳）在各阶段输出可 grep 的进度行。

**Architecture:** 新增 `backend/etl/progress.py` 作为唯一进度契约；fetch/DWD/batch_append/export 接入；`orchestrator` 的 calc 进度改为复用同一 helper 并删除 batch 虚假 burst；节流间隔从 20日/10股 收紧为 5日/5股，默认 30s 无输出则打 `still running` 心跳。不改 Calculator 业务逻辑、不改 DWS schema。

**Tech Stack:** Python 3.9、`logging` stdlib、`threading.Lock`、pytest `caplog`、现有 `log_etl_start/end`。

**前置审计结论（证据）：**

| 模块 | 现有节流 | 静默风险 |
|------|----------|----------|
| `ods_daily.py` parallel | `done % 20 == 0 or done == 1` | 相邻进度间隔 60–120s |
| `ods_daily.py` stock | `(i+1) % 10 == 0` | <10 股无中间进度 |
| `ods_stock_basic.py` / `ods_trade_cal.py` | 无 logger | 完全静默 |
| `build_dwd.py` | 仅 warning | rebuild 分钟级静默 |
| `calc_batch_append.py` | 无 logger | 新日 calc 最大黑洞 |
| `export_wide.py` | 无 logger | Excel 写盘静默 |
| `orchestrator.py` L1346–1347 | 虚假 burst | 进度语义失真 |

---

## File Map

| 文件 | 动作 | 职责 |
|------|------|------|
| `backend/etl/progress.py` | **Create** | `StageProgress` 统一进度 + 心跳 |
| `backend/config.py` | Modify | `LOG_PROGRESS_HEARTBEAT_SEC` 等 |
| `backend/fetch/ods_daily.py` | Modify | date/stock 进度接入 |
| `backend/fetch/ods_stock_basic.py` | Modify | 起止 INFO |
| `backend/fetch/ods_trade_cal.py` | Modify | 起止 INFO |
| `backend/etl/build_dwd.py` | Modify | 三步 DWD 起止 + 停牌填充进度 |
| `backend/etl/calc_batch_append.py` | Modify | preflight + 每指标 batch 进度 |
| `backend/etl/orchestrator.py` | Modify | calc 进度复用、删虚假 burst |
| `backend/export_wide.py` | Modify | 查询/写盘进度 |
| `backend/cli.py` | Modify | `cmd_fetch` ETL 包装 |
| `tests/test_etl/test_progress.py` | **Create** | StageProgress 单测 |
| `tests/test_fetch/test_ods_daily_progress.py` | **Create** | fetch 进度行单测 |
| `tests/test_etl/test_orchestrator.py` | Modify | calc 进度前缀断言更新 |
| `CLAUDE.md` | Modify | 日志系统章节 |
| `.env.example` | Modify | 新 env 变量 |

---

### Task 1: `StageProgress` 核心模块

**Files:**
- Create: `backend/etl/progress.py`
- Modify: `backend/config.py`
- Create: `tests/test_etl/test_progress.py`

- [ ] **Step 1: 在 `backend/config.py` 追加配置**

```python
# Progress logging — count throttle + time heartbeat (stderr anti-stall)
LOG_PROGRESS_HEARTBEAT_SEC = float(os.getenv("LOG_PROGRESS_HEARTBEAT_SEC", "30"))
LOG_PROGRESS_DAY_STEP = int(os.getenv("LOG_PROGRESS_DAY_STEP", "5"))
LOG_PROGRESS_STOCK_STEP = int(os.getenv("LOG_PROGRESS_STOCK_STEP", "5"))
```

- [ ] **Step 2: 写失败测试**

```python
# tests/test_etl/test_progress.py
import logging
import time

import pytest

from backend.etl.progress import StageProgress


def _lines(caplog, stage: str):
    prefix = f"progress {stage}:"
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def test_stage_progress_count_throttle(caplog):
    p = StageProgress("test.stage", total=100, count_step=5, heartbeat_sec=999)
    with caplog.at_level(logging.INFO, logger="backend.etl.progress"):
        p.log_start(extra="unit=items")
        for _ in range(100):
            p.tick()
        p.log_done(rows=100)

    lines = _lines(caplog, "test.stage")
    assert any("5/100" in ln for ln in lines)
    assert any("100/100 (100%)" in ln for ln in lines)
    assert lines[-2 if lines[-1].startswith("progress test.stage: done") else -1]  # has 100%


def test_stage_progress_heartbeat(caplog):
    p = StageProgress("hb.stage", total=1000, count_step=500, heartbeat_sec=0.05)
    with caplog.at_level(logging.INFO, logger="backend.etl.progress"):
        p.log_start()
        p.tick()  # done=1, below step 500, but heartbeat should fire soon
        time.sleep(0.08)
        p.tick(force_heartbeat=True)
        p.log_done()

    lines = _lines(caplog, "hb.stage")
    assert len(lines) >= 2, f"expected heartbeat lines, got {lines}"


def test_stage_progress_thread_safe(caplog):
    import threading

    p = StageProgress("thread.stage", total=200, count_step=10, heartbeat_sec=999)
    with caplog.at_level(logging.INFO, logger="backend.etl.progress"):
        p.log_start()

        def worker():
            for _ in range(50):
                p.tick()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        p.log_done()

    lines = _lines(caplog, "thread.stage")
    assert any("200/200 (100%)" in ln for ln in lines)
```

- [ ] **Step 3: 运行测试确认 FAIL**

```bash
pytest tests/test_etl/test_progress.py -v
```

Expected: FAIL — `ModuleNotFoundError: backend.etl.progress`

- [ ] **Step 4: 实现 `backend/etl/progress.py`**

```python
"""Unified stage progress logging — count throttle + time heartbeat."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from backend.config import (
    LOG_PROGRESS_DAY_STEP,
    LOG_PROGRESS_HEARTBEAT_SEC,
    LOG_PROGRESS_STOCK_STEP,
)

logger = logging.getLogger(__name__)


class StageProgress:
    """Thread-safe progress reporter for long-running ETL stages.

    Log format (stable prefix for grep):
        progress {stage}: {done}/{total} ({pct}%) | {elapsed}s | {rate} {unit}/s | ETA ~{eta}s
        progress {stage}: still running | {done}/{total} | {elapsed}s
        progress {stage}: done | {elapsed}s | {summary}
    """

    def __init__(
        self,
        stage: str,
        total: int,
        *,
        count_step: Optional[int] = None,
        heartbeat_sec: Optional[float] = None,
        unit: str = "items",
    ):
        self.stage = stage
        self.total = max(0, int(total))
        self.count_step = max(1, count_step or max(1, self.total // 20))
        self.heartbeat_sec = (
            float(LOG_PROGRESS_HEARTBEAT_SEC)
            if heartbeat_sec is None
            else float(heartbeat_sec)
        )
        self.unit = unit
        self._done = 0
        self._t0 = 0.0
        self._last_log_mono = 0.0
        self._lock = threading.Lock()

    def log_start(self, **extra: object) -> None:
        suffix = " ".join(f"{k}={v}" for k, v in extra.items())
        msg = f"progress {self.stage}: started | total={self.total} {self.unit}"
        if suffix:
            msg = f"{msg} | {suffix}"
        with self._lock:
            self._t0 = time.monotonic()
            self._last_log_mono = self._t0
        logger.info(msg)

    def tick(self, n: int = 1, *, force: bool = False, force_heartbeat: bool = False) -> None:
        with self._lock:
            self._done += n
            self._maybe_log(force=force, force_heartbeat=force_heartbeat)

    def log_done(self, **extra: object) -> None:
        elapsed = time.monotonic() - self._t0 if self._t0 else 0.0
        suffix = " ".join(f"{k}={v}" for k, v in extra.items())
        msg = f"progress {self.stage}: done | {elapsed:.0f}s"
        if suffix:
            msg = f"{msg} | {suffix}"
        logger.info(msg)

    def _maybe_log(self, *, force: bool, force_heartbeat: bool) -> None:
        now = time.monotonic()
        elapsed = now - self._t0 if self._t0 else 0.0
        done = self._done
        total = self.total
        if total <= 0:
            return

        at_step = (done % self.count_step == 0) or done == total
        heartbeat_due = (
            force_heartbeat
            or (
                self.heartbeat_sec > 0
                and (now - self._last_log_mono) >= self.heartbeat_sec
                and done > 0
                and done < total
            )
        )

        if not (force or at_step or heartbeat_due):
            return

        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (total - done) / rate if rate > 0 else 0.0
        pct = done * 100 // total

        if heartbeat_due and not at_step and not force:
            logger.info(
                "progress %s: still running | %d/%d (%d%%) | %.0fs",
                self.stage, done, total, pct, elapsed,
            )
        else:
            logger.info(
                "progress %s: %d/%d (%d%%) | %.0fs | %.1f %s/s | ETA ~%.0fs",
                self.stage, done, total, pct, elapsed, rate, self.unit, eta,
            )
        self._last_log_mono = now


def day_progress(stage: str, total_days: int) -> StageProgress:
    return StageProgress(
        stage, total_days,
        count_step=max(1, LOG_PROGRESS_DAY_STEP),
        unit="days",
    )


def stock_progress(stage: str, total_stocks: int) -> StageProgress:
    return StageProgress(
        stage, total_stocks,
        count_step=max(1, LOG_PROGRESS_STOCK_STEP),
        unit="stocks",
    )
```

- [ ] **Step 5: 运行测试确认 PASS**

```bash
pytest tests/test_etl/test_progress.py -v
```

Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add backend/etl/progress.py backend/config.py tests/test_etl/test_progress.py
git commit -m "feat: add StageProgress for unified ETL progress logging"
```

---

### Task 2: Fetch — `ods_daily` 接入 `StageProgress`

**Files:**
- Modify: `backend/fetch/ods_daily.py`
- Create: `tests/test_fetch/test_ods_daily_progress.py`

- [ ] **Step 1: 写失败测试（mock 并行 fetch 进度）**

```python
# tests/test_fetch/test_ods_daily_progress.py
import logging
from unittest.mock import patch

import duckdb
import pytest

from backend.fetch.ods_daily import fetch_by_date_range_parallel


class _FakeClient:
    def call(self, api, **kwargs):
        if api == "trade_cal":
            return [{"cal_date": d} for d in ["20260101", "20260102", "20260103"]]
        return []


def test_parallel_fetch_emits_unified_progress_prefix(caplog, monkeypatch):
    """3 trading days → progress lines use progress fetch.ods: prefix."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT, "
                "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
                "vol DOUBLE, amount DOUBLE, pct_chg DOUBLE, adj_factor DOUBLE, fetched_at TIMESTAMP)")
    con.execute("CREATE TABLE ods_daily_basic (ts_code TEXT, trade_date TEXT, "
                "total_mv DOUBLE, pe_ttm DOUBLE, turnover_rate DOUBLE, volume_ratio DOUBLE, fetched_at TIMESTAMP)")
    con.execute("CREATE TABLE ods_moneyflow (ts_code TEXT, trade_date TEXT, "
                "buy_sm_vol DOUBLE, buy_sm_amount DOUBLE, sell_sm_vol DOUBLE, sell_sm_amount DOUBLE, "
                "buy_md_vol DOUBLE, buy_md_amount DOUBLE, sell_md_vol DOUBLE, sell_md_amount DOUBLE, "
                "buy_lg_vol DOUBLE, buy_lg_amount DOUBLE, sell_lg_vol DOUBLE, sell_lg_amount DOUBLE, "
                "buy_elg_vol DOUBLE, buy_elg_amount DOUBLE, sell_elg_vol DOUBLE, sell_elg_amount DOUBLE, "
                "net_mf_vol DOUBLE, net_mf_amount DOUBLE, fetched_at TIMESTAMP)")
    con.execute("CREATE TABLE dim_date (trade_date TEXT, is_trade_day INTEGER)")
    for d in ["20260101", "20260102", "20260103"]:
        con.execute("INSERT INTO dim_date VALUES (?, 1)", (d,))

    monkeypatch.setattr("backend.fetch.ods_daily.TushareClient", lambda: _FakeClient())
    monkeypatch.setattr(
        "backend.fetch.ods_daily._get_trading_days",
        lambda *a, **k: ["20260101", "20260102", "20260103"],
    )

    with caplog.at_level(logging.INFO):
        fetch_by_date_range_parallel("20260101", "20260103", workers=1, con=con)

    progress = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("progress fetch.ods:")]
    assert any("started" in m for m in progress), progress
    assert any("3/3 (100%)" in m for m in progress), progress
    con.close()
```

- [ ] **Step 2: 运行测试确认 FAIL**

```bash
pytest tests/test_fetch/test_ods_daily_progress.py -v
```

Expected: FAIL — no `progress fetch.ods:` lines

- [ ] **Step 3: 修改 `fetch_by_date_range_parallel`**

在 `backend/fetch/ods_daily.py` 顶部增加：

```python
from backend.etl.progress import day_progress, stock_progress
```

替换 `fetch_by_date_range_parallel` 内进度块（约 L532–683）：

1. `days` 确定后立刻：

```python
prog = day_progress("fetch.ods", len(days))
prog.log_start(threads=workers, range=f"{start}~{end}")
```

2. 删除 `progress_lock` / `progress_done` 及旧 `logger.info("ODS fetch: ...")` 块。

3. 在 `_fetch_chunk` 内每完成一个 `trade_date` 后：

```python
prog.tick()
```

4. 函数 return 前：

```python
prog.log_done(rows=total_rows, days=len(days))
```

保留末尾兼容行（deprecated，一轮发布后删）：

```python
logger.info("ODS fetch complete: %d rows in %.0fs (%.1f days/min)",
            total_rows, elapsed, len(days) / elapsed * 60 if elapsed > 0 else 0)
```

5. 同样改 `fetch_stocks_incremental`：

```python
prog = stock_progress("fetch.stocks", len(ts_codes))
prog.log_start(range=f"{start}~{end}")
# 循环内每处理完一只（无论 skip 或有拉取）:
prog.tick()
# 结束:
prog.log_done(rows=total)
```

6. 修 `fetch_by_date_range`（串行，测试仍用）：首条进度在 `i==0` 时也 tick，或直接用 `day_progress` 替换 `i % 20` 逻辑。

- [ ] **Step 4: 运行测试 PASS**

```bash
pytest tests/test_fetch/test_ods_daily_progress.py tests/test_fetch/test_ods_daily.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/ods_daily.py tests/test_fetch/test_ods_daily_progress.py
git commit -m "feat: unify ODS fetch progress with StageProgress (5-day/5-stock throttle)"
```

---

### Task 3: Fetch — 静态表 + CLI 包装

**Files:**
- Modify: `backend/fetch/ods_stock_basic.py`
- Modify: `backend/fetch/ods_trade_cal.py`
- Modify: `backend/cli.py`
- Modify: `tests/test_fetch/test_ods_static.py`

- [ ] **Step 1: `ods_stock_basic.py` 加日志**

```python
import json
import logging

logger = logging.getLogger(__name__)

def fetch_stock_basic(client, con) -> int:
    logger.info("progress fetch.stock_basic: started")
    records = client.call("stock_basic", exchange="", list_status="L",
        fields="ts_code,symbol,name,area,industry,exchange,list_date,delist_date")
    for r in records:
        con.execute("""INSERT OR REPLACE INTO ods_stock_basic
            (ts_code, symbol, name, area, industry, exchange, list_date, delist_date, raw_json, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,now())""",
            (r["ts_code"], r["symbol"], r["name"], r.get("area",""), r.get("industry",""),
             r["exchange"], r.get("list_date",""), r.get("delist_date",""),
             json.dumps(r, ensure_ascii=False)))
    logger.info("progress fetch.stock_basic: done | rows=%d", len(records))
    return len(records)
```

- [ ] **Step 2: `ods_trade_cal.py` 加日志**

```python
import logging

logger = logging.getLogger(__name__)

def fetch_trade_cal(client, con, start: str = "20150101", end: str = "20301231") -> int:
    logger.info("progress fetch.trade_cal: started | range=%s~%s", start, end)
    records = client.call("trade_cal", exchange="SSE", start_date=start, end_date=end)
    for r in records:
        con.execute("""INSERT OR REPLACE INTO ods_trade_cal (cal_date, is_open, pretrade_date)
            VALUES (?,?,?)""", (r["cal_date"], r["is_open"], r.get("pretrade_date","")))
    logger.info("progress fetch.trade_cal: done | rows=%d", len(records))
    return len(records)
```

- [ ] **Step 3: `cmd_fetch` 加 ETL 审计**

在 `backend/cli.py` 的 `cmd_fetch` 内，`con = get_connection()` 之后：

```python
from backend.etl.error_handler import log_etl_end, log_etl_start

lid, t0 = log_etl_start(con, "cli_fetch")
try:
    # ... existing fetch logic ...
    log_etl_end(con, lid, "cli_fetch", t0, "success", row_count=n,
                data_completeness={"mode": "stock" if args.ts_code else "date",
                                   "start": start, "end": end})
    logger.info("Fetch complete: %d ODS rows", n)
finally:
    con.close()
```

将 `print(f"Fetched {n} rows")` 改为 `logger.info`（保留 print 可选——**删除 print**，统一 logger）。

- [ ] **Step 4: 更新 `test_ods_static.py` 断言日志存在（caplog）**

- [ ] **Step 5: 运行测试**

```bash
pytest tests/test_fetch/test_ods_static.py tests/test_cli.py -v -k fetch
```

- [ ] **Step 6: Commit**

```bash
git add backend/fetch/ods_stock_basic.py backend/fetch/ods_trade_cal.py backend/cli.py tests/test_fetch/test_ods_static.py
git commit -m "feat: add fetch progress for stock_basic/trade_cal and cli_fetch ETL audit"
```

---

### Task 4: DWD 重建进度

**Files:**
- Modify: `backend/etl/build_dwd.py`
- Create: `tests/test_etl/test_build_dwd_progress.py`

- [ ] **Step 1: 写失败测试（内存 DuckDB smoke）**

```python
# tests/test_etl/test_build_dwd_progress.py
import logging

import duckdb
import pytest

from backend.etl.build_dwd import rebuild_all_dwd


def test_rebuild_all_dwd_logs_substeps(caplog):
    con = duckdb.connect(":memory:")
    # minimal schema for daily path only — use fixtures if project has db_with_schema
    con.execute("""CREATE TABLE ods_daily (
        ts_code TEXT, trade_date TEXT, open DOUBLE, high DOUBLE, low DOUBLE,
        close DOUBLE, vol DOUBLE, amount DOUBLE, pct_chg DOUBLE, adj_factor DOUBLE)""")
    con.execute("""CREATE TABLE ods_daily_basic (
        ts_code TEXT, trade_date TEXT, total_mv DOUBLE, pe_ttm DOUBLE,
        turnover_rate DOUBLE, volume_ratio DOUBLE)""")
    con.execute("""CREATE TABLE dwd_daily_quote (
        ts_code TEXT, trade_date TEXT, open_qfq DOUBLE, high_qfq DOUBLE, low_qfq DOUBLE,
        close_qfq DOUBLE, vol DOUBLE, amount DOUBLE, pct_chg DOUBLE, total_mv DOUBLE,
        pe_ttm DOUBLE, turnover_rate DOUBLE, volume_ratio DOUBLE, is_suspended INTEGER)""")
    con.execute("""CREATE TABLE dwd_weekly_quote (
        ts_code TEXT, trade_date TEXT, open_qfq DOUBLE, high_qfq DOUBLE, low_qfq DOUBLE,
        close_qfq DOUBLE, vol DOUBLE, amount DOUBLE, pct_chg DOUBLE, total_mv DOUBLE,
        pe_ttm DOUBLE, turnover_rate DOUBLE, volume_ratio DOUBLE, active_days INTEGER)""")
    con.execute("""CREATE TABLE dwd_daily_moneyflow (
        ts_code TEXT, trade_date TEXT, net_mf_vol DOUBLE, net_mf_amount DOUBLE,
        buy_lg_vol DOUBLE, sell_lg_vol DOUBLE, buy_elg_vol DOUBLE, sell_elg_vol DOUBLE,
        total_vol DOUBLE)""")
    con.execute("""CREATE TABLE ods_moneyflow (
        ts_code TEXT, trade_date TEXT, net_mf_vol DOUBLE, net_mf_amount DOUBLE,
        buy_sm_vol DOUBLE, buy_md_vol DOUBLE, buy_lg_vol DOUBLE, buy_elg_vol DOUBLE,
        sell_sm_vol DOUBLE, sell_md_vol DOUBLE, sell_lg_vol DOUBLE, sell_elg_vol DOUBLE,
        buy_sm_amount DOUBLE, sell_sm_amount DOUBLE, buy_md_amount DOUBLE, sell_md_amount DOUBLE,
        buy_lg_amount DOUBLE, sell_lg_amount DOUBLE, buy_elg_amount DOUBLE, sell_elg_amount DOUBLE)""")
    con.execute("""CREATE TABLE dim_date (trade_date TEXT, is_trade_day INTEGER)""")
    con.execute("""CREATE TABLE dim_stock (ts_code TEXT)""")
    con.execute("INSERT INTO ods_daily VALUES ('000001.SZ','20260101',1,1,1,1,1,1,1,1)")
    con.execute("INSERT INTO dim_stock VALUES ('000001.SZ')")
    con.execute("INSERT INTO dim_date VALUES ('20260101', 1)")

    with caplog.at_level(logging.INFO, logger="backend.etl.build_dwd"):
        rebuild_all_dwd(con, ["000001.SZ"])

    msgs = [r.getMessage() for r in caplog.records]
    assert any("progress dwd.daily_quote:" in m for m in msgs)
    assert any("progress dwd.weekly_quote:" in m for m in msgs)
    assert any("progress dwd.moneyflow:" in m for m in msgs)
    con.close()
```

若 schema 过复杂，改用项目已有 `db_with_schema` fixture 并只断言 3 条 `progress dwd.*: started`。

- [ ] **Step 2: 修改 `rebuild_all_dwd`**

```python
import logging
import time

logger = logging.getLogger(__name__)

def rebuild_all_dwd(con, ts_codes=None) -> dict:
    logger.info("progress dwd.rebuild: started | stocks=%s",
                len(ts_codes) if ts_codes else "all")
    t0 = time.monotonic()
    result = {
        "daily_quote": build_dwd_daily_quote(con, ts_codes),
        "weekly_quote": build_dwd_weekly_quote(con, ts_codes),
        "moneyflow": build_dwd_daily_moneyflow(con, ts_codes),
    }
    logger.info("progress dwd.rebuild: done | %.0fs | %s",
                time.monotonic() - t0, result)
    return result
```

每个 `build_dwd_*` 入口/出口：

```python
logger.info("progress dwd.daily_quote: started | stocks=%s", ...)
# ... existing SQL ...
logger.info("progress dwd.daily_quote: done | rows=%d", n)
```

停牌填充 loop（`build_dwd_daily_quote` L116–143）：每处理 50 只 gap 股打一行：

```python
from backend.etl.progress import StageProgress
# gap_stocks 确定后:
if gap_stocks:
    fill_list = [c for c in codes_to_fill if c in gap_stocks]
    sp = StageProgress("dwd.suspension_fill", len(fill_list), count_step=50, unit="stocks")
    sp.log_start()
    for i, ts_code in enumerate(codes_to_fill):
        if ts_code not in gap_stocks:
            continue
        # ... existing LATERAL INSERT ...
        sp.tick()
    sp.log_done()
```

- [ ] **Step 3: pytest PASS**

```bash
pytest tests/test_etl/test_build_dwd_progress.py -v
```

- [ ] **Step 4: Commit**

```bash
git add backend/etl/build_dwd.py tests/test_etl/test_build_dwd_progress.py
git commit -m "feat: add DWD rebuild step progress logging"
```

---

### Task 5: `calc_batch_append` 进度 + 删除虚假 burst

**Files:**
- Modify: `backend/etl/calc_batch_append.py`
- Modify: `backend/etl/orchestrator.py`
- Modify: `tests/test_etl/test_batch_append_calc.py`（若存在）

- [ ] **Step 1: `run_batch_append_phase` 加进度**

在 `backend/etl/calc_batch_append.py` 顶部：

```python
import logging
import time

logger = logging.getLogger(__name__)
```

`run_batch_append_phase` 内：

```python
logger.info("progress calc.batch_append: started | stocks=%d", len(codes))
t0 = time.monotonic()

# preflight 循环（for ts_code in codes）外包:
from backend.etl.progress import stock_progress
preflight = stock_progress("calc.batch_preflight", len(codes))
preflight.log_start()
for ts_code in codes:
    # ... existing preflight body ...
    preflight.tick()
preflight.log_done()

# 每个 (indicator_name, freq) batch append 后:
logger.info(
    "progress calc.batch_append: %s %s | append=%d calculated=%d",
    indicator_name, freq, len(append_codes), result.calculated,
)

# return 前:
logger.info("progress calc.batch_append: done | %.0fs | chunk=%d full=%d",
            time.monotonic() - t0, len(chunk_codes), len(codes) - len(chunk_codes))
```

- [ ] **Step 2: 删除 `orchestrator.py` L1346–1347 虚假 burst**

删除：

```python
for _ in range(n_batch_only):
    _report_calc_progress()
```

改为在 `run_batch_append_phase` 内对「完全 batch 处理掉」的每股调用 orchestrator 提供的回调，或在 batch 结束后由 orchestrator 统一：

```python
# orchestrator run_calc 内，batch_ctx 返回后:
if batch_ctx and n_batch_only:
    logger.info(
        "batch_append: %d stocks fully handled (APPEND/SKIP), %d need chunk",
        n_batch_only, len(chunk_codes),
    )
# 不再 fake tick；calc progress 仅从 _calc_stock_chunk 真实每股上报
```

若需 batch 阶段占用总进度条：在 `run_batch_append_phase` 返回 `batch_handled_count`，`_init_calc_progress` 保持不变，batch 内 `preflight.tick()` 已给用户可见进度，不必伪造 calc progress 计数。

- [ ] **Step 3: 将 `_report_calc_progress` 改为复用 `StageProgress`**

在 `orchestrator.py`：

```python
from backend.etl.progress import StageProgress

_calc_progress: Optional[StageProgress] = None

def _init_calc_progress(total: int) -> None:
    global _calc_progress
    _calc_progress = StageProgress("calc.stocks", total, unit="stocks")

def _report_calc_progress() -> None:
    if _calc_progress is not None:
        _calc_progress.tick()
```

`_init_calc_progress` 后立刻 `_calc_progress.log_start(threads=workers)`（在 `run_calc` 拿到 workers 后）。

`calc ALL DONE` 前 `_calc_progress.log_done(rows=grand_total)`。

更新 `tests/test_etl/test_orchestrator.py` 中 `_count_progress_lines`：

```python
def _count_progress_lines(caplog):
    return [r for r in caplog.records
            if r.getMessage().startswith("progress calc.stocks:")]
```

- [ ] **Step 4: 运行相关测试**

```bash
pytest tests/test_etl/test_orchestrator.py tests/test_etl/test_batch_append_calc.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_append.py backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "feat: batch_append progress logging and remove fake calc progress burst"
```

---

### Task 6: Export 进度

**Files:**
- Modify: `backend/export_wide.py`

- [ ] **Step 1: 在 `export_wide_to_excel` 加日志**

文件顶部：

```python
import logging
import time

logger = logging.getLogger(__name__)
```

函数内：

```python
logger.info("progress export: started | date=%s", trade_date)
t0 = time.monotonic()

# daily 查询后:
logger.info("progress export: daily query done | rows=%d | %.0fs",
            len(daily), time.monotonic() - t0)

# weekly merge 后（若有 weekly 分支）:
logger.info("progress export: weekly merge done | %.0fs", time.monotonic() - t0)

# wb.save 前:
logger.info("progress export: writing xlsx | path=%s", output_path)
# save 后:
logger.info("progress export: done | rows=%d | %.0fs", len(df), time.monotonic() - t0)
```

- [ ] **Step 2: 手动验证**

```bash
python -m backend.cli export --date 20260602 2>&1 | grep "progress export:"
```

Expected: 4–5 行 `progress export:`

- [ ] **Step 3: Commit**

```bash
git add backend/export_wide.py
git commit -m "feat: add export step progress logging"
```

---

### Task 7: `run_calc` auto-fetch 桶级进度

**Files:**
- Modify: `backend/etl/orchestrator.py`

- [ ] **Step 1: auto-fetch 桶循环加子进度**

在 `run_calc` 的 `for (seg_start, seg_end), bucket_codes in range_buckets.items():` 内，fetch 前：

```python
logger.info(
    "progress calc.auto_fetch: bucket %d/%d | %s~%s | stocks=%d",
    bucket_idx, len(range_buckets), seg_start, seg_end, len(bucket_codes),
)
```

桶结束后：

```python
logger.info("progress calc.auto_fetch: bucket done | rows=%d", rows)
```

- [ ] **Step 2: Commit**

```bash
git add backend/etl/orchestrator.py
git commit -m "feat: auto-fetch bucket progress in run_calc"
```

---

### Task 8: 文档与环境变量

**Files:**
- Modify: `CLAUDE.md`
- Modify: `.env.example`

- [ ] **Step 1: 更新 `CLAUDE.md` 日志系统章节**

追加：

```markdown
- **统一进度前缀：** `progress {stage}:` — fetch.ods / fetch.stocks / dwd.* / calc.batch_append / calc.stocks / export
- **节流：** 默认每 5 交易日 / 5 股票一条；`LOG_PROGRESS_HEARTBEAT_SEC=30` 无输出则打 `still running`
- **环境变量：** `LOG_PROGRESS_HEARTBEAT_SEC` / `LOG_PROGRESS_DAY_STEP` / `LOG_PROGRESS_STOCK_STEP`
```

删除或更新旧描述中「ODS fetch 每 20 日」「calc progress:」前缀说明。

- [ ] **Step 2: `.env.example` 追加**

```
LOG_PROGRESS_HEARTBEAT_SEC=30
LOG_PROGRESS_DAY_STEP=5
LOG_PROGRESS_STOCK_STEP=5
```

- [ ] **Step 3: 全量测试**

```bash
pytest tests/ -v
```

Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md .env.example
git commit -m "docs: document unified progress logging and env vars"
```

---

## Self-Review（计划自检）

| 需求 | 对应 Task |
|------|-----------|
| fetch 实时进度 | Task 2, 3 |
| DWD 静默 | Task 4 |
| calc batch_append 静默 | Task 5 |
| 删除虚假 burst | Task 5 |
| export 静默 | Task 6 |
| 时间心跳兜底 | Task 1（全阶段复用） |
| 各阶段统一格式 | Task 1 `progress {stage}:` |
| CLI fetch 包装 | Task 3 |
| auto-fetch 桶进度 | Task 7 |
| 文档 | Task 8 |

**Placeholder 扫描：** 无 TBD/TODO/「类似 Task N」省略。

**类型一致性：** `StageProgress.tick(force_heartbeat=)` 与测试一致；`day_progress`/`stock_progress` 工厂函数在 Task 2 使用。

---

## 验收标准（手动）

```bash
# 1. fetch 应持续有 progress 行（每 ≤30s）
python -m backend.cli fetch --start 20260101 --end 20260131 2>&1 | grep "progress fetch"

# 2. run 三步均有 progress
python -m backend.cli run --date 20260602 --skip-export 2>&1 | grep "progress "

# 3. 不应再出现旧前缀作为唯一进度（兼容 complete 行可保留一轮）
python -m backend.cli fetch --start 20260101 --end 20260110 2>&1 | grep -c "progress fetch.ods:"
# 期望 >= 3
```

---

Plan complete and saved to `docs/superpowers/plans/2026-06-08-logging-progress-unification.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每个 Task 派独立 subagent，Task 间你做 review，迭代快

**2. Inline Execution** — 本会话用 executing-plans 按 Task 批量执行，检查点处暂停给你看

你选哪种？
