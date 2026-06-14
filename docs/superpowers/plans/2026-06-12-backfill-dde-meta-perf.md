# Backfill DDE Meta Performance Optimization Plan

> **⚠️ SUPERSEDED** — 按股并行 Perf v2 已废弃。请实施 **Plan C（date-batched）**：
> `docs/superpowers/plans/2026-06-13-ods-date-batched-backfill-plan-c.md`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ~~将全市场 `backfill-dde-meta` 从 ~3h 单线程降至 ~45–60min~~ → 见 Plan C（~740 API，≤20min）

**Architecture:** 保留现有「仅补 NULL 缺口、每股 merged range 一次 API」语义；三处提速：(1) 批量 SQL 预筛缺口股，消除 5207×2 次逐股 gap 查询；(2) 3 线程 stock-parallel + 进程级 per-interface 限流（复用 `fetch_by_date_range_parallel` 模式）；(3) API 返回后 `register + UPDATE FROM` 批量写 ODS，替代逐行 `execute`。CLI 新增 `--workers` 与 `--sync-dwd-batch`；ODS 完成后可选增量 DWD sync，避免「全 ODS 跑完才 sync」导致中断后 DWD 长期漂移。

**Tech Stack:** Python 3.9+, DuckDB (WAL 多写), tushare (`moneyflow_dc`, `daily_basic`), pandas, pytest, ThreadPoolExecutor

**Upstream:** 功能计划 `docs/superpowers/plans/2026-06-12-weekly-dde-trend-backfill.md`（Task 1–6 已落地）；本计划仅优化 perf + 运维可恢复性，不改动 calc/B4 算法。

**Hard-coded constants（不可改，除非 spec 更新）：**

| Constant | Value | File |
|----------|-------|------|
| `DDE_WEEKLY_TREND_HISTORY_DAYS` | 900 | `backend/etl/calc_dde.py` |
| `MONEYFLOW_DC_MIN` | 20230911 | `backend/fetch/backfill_dde_meta.py` |
| `TushareClient.RATE_LIMIT` | 480/min/interface | `backend/fetch/client.py` |
| Default parallel workers | 3 | 对齐 `fetch_by_date_range_parallel` |

**中断前实库状态（2026-06-13，进程已 kill）：**

- ODS 阶段约 495/5207 股已处理（进度日志）；ODS `net_amount_dc` 在 `[20231225, calc_date]` 窗口内仍有 ~4332 股含 NULL 行（需续跑）
- DWD sync **未执行**（原设计等全量 ODS 结束）
- 恢复安全：已补 ODS 不会丢；重跑自动 skip 无缺口股

**性能目标（全市场 5207 股，900 天窗口）：**

| 指标 | 当前 v1 | 目标 v2 |
|------|---------|---------|
| ODS 墙钟 | ~3.0h | ≤60min |
| 断点续跑 | ODS 有，DWD 无 | ODS + 分批 DWD |
| 每股 gap SQL | 2×5207 | 2 条 bulk + 仅缺口股 per-stock range |
| 写库 | ~300 UPDATE/股 | 1 bulk UPDATE/股/API |

---

## File map

| File | Responsibility |
|------|----------------|
| `backend/fetch/backfill_dde_meta.py` | **Modify** — bulk gap 预筛、parallel orchestrator、batch sync hook |
| `backend/fetch/ods_daily.py` | **Modify** — bulk UPDATE helpers for dc/circ backfill |
| `backend/cli.py` | **Modify** — `--workers`, `--sync-dwd-batch` |
| `tests/test_fetch/test_backfill_dde_meta.py` | **Modify** — bulk gap + parallel + batch write tests |
| `CLAUDE.md` | **Modify** — 优化后运维命令 + ETA 说明 |
| `docs/superpowers/plans/2026-06-12-weekly-dde-trend-backfill.md` | **Modify** — 追加「Perf v2」交叉引用 |

---

## Root cause（为何 v1 慢）

1. **单线程** `for ts_code in codes` — 无网络/API 重叠
2. **5207×2 次 gap SELECT** — 即使无缺口也全扫
3. **逐行 UPDATE** — `_backfill_net_amount_dc_stock` / `_backfill_circ_mv_stock` 每 API 返回 ~370 行 × 1–2 SQL
4. **DWD sync 滞后** — `--sync-dwd` 仅在全 ODS 完成后一次执行

---

## Acceptance criteria

| ID | Check | Pass |
|----|-------|------|
| P1 | 全市场 ODS backfill 墙钟 | ≤3600s（3 worker，实库） |
| P2 | 断点续跑 | 中断后重跑：已完成股 API=0、秒过 |
| P3 | 分批 DWD sync | `--sync-dwd-batch 500` 每 500 股后 DWD 行数上升 |
| P4 | 语义等价 | 同 ts_code/range，v2 写后 ODS 值与 v1 oracle 逐行相等 |
| P5 | pytest | `tests/test_fetch/test_backfill_dde_meta.py` 全绿 |
| P6 | 原验收 V1–V6 | 续跑完成后仍满足 `2026-06-12-weekly-dde-trend-backfill.md` |

---

### Task 1: Bulk gap 预筛（消除 5207×2 查询）

**Files:**
- Modify: `backend/fetch/backfill_dde_meta.py`
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write the failing test**

```python
def test_list_stocks_needing_dc_backfill(db_with_schema):
    from backend.fetch.backfill_dde_meta import list_stocks_needing_dc_backfill

    con = db_with_schema
    con.execute(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) "
        "VALUES ('20240101', 0, 0), ('20240102', 1, 0), ('20240103', 1, 0)"
    )
    con.execute(
        "INSERT INTO ods_stock_basic (ts_code, list_date) VALUES "
        "('A.SZ','20200101'), ('B.SZ','20200101')"
    )
    con.execute(
        "INSERT INTO ods_moneyflow (ts_code, trade_date, net_mf_amount, net_amount_dc, fetched_at) VALUES "
        "('A.SZ','20240102',1.0,NULL,now()), "
        "('B.SZ','20240102',1.0,99.0,now())"
    )
    need = list_stocks_needing_dc_backfill(con, "20240102", "20240103")
    assert need == ["A.SZ"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py::test_list_stocks_needing_dc_backfill -v`

Expected: FAIL with `ImportError` or `cannot import name 'list_stocks_needing_dc_backfill'`

- [ ] **Step 3: Implement bulk gap listers**

Add to `backend/fetch/backfill_dde_meta.py`:

```python
def list_stocks_needing_dc_backfill(
    con, start: str, end: str, ts_codes: Optional[List[str]] = None,
) -> List[str]:
    code_filter = ""
    params: list = [start, end]
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        code_filter = f" AND ts_code IN ({ph})"
        params.extend(ts_codes)
    rows = con.execute(
        f"""
        SELECT DISTINCT ts_code
        FROM ods_moneyflow
        WHERE trade_date >= ? AND trade_date <= ?
          AND net_amount_dc IS NULL
          {code_filter}
        ORDER BY ts_code
        """,
        params,
    ).fetchall()
    return [r[0] for r in rows]


def list_stocks_needing_circ_backfill(
    con, start: str, end: str, ts_codes: Optional[List[str]] = None,
) -> List[str]:
    code_filter = ""
    params: list = [start, end, start, end]
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        code_filter = f" AND d.ts_code IN ({ph})"
        params.extend(ts_codes)
    rows = con.execute(
        f"""
        SELECT DISTINCT d.ts_code
        FROM ods_daily d
        LEFT JOIN ods_daily_basic b
          ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date
        WHERE d.trade_date >= ? AND d.trade_date <= ?
          AND b.circ_mv IS NULL
          {code_filter}
        ORDER BY d.ts_code
        """,
        params,
    ).fetchall()
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py::test_list_stocks_needing_dc_backfill -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/backfill_dde_meta.py tests/test_fetch/test_backfill_dde_meta.py
git commit -m "perf(backfill): bulk SQL to list stocks needing dc/circ gaps"
```

---

### Task 2: Bulk ODS 写（register + UPDATE FROM）

**Files:**
- Modify: `backend/fetch/ods_daily.py`
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write the failing test**

```python
def test_backfill_net_amount_dc_stock_bulk_update(db_with_schema):
    import pandas as pd
    from backend.fetch.ods_daily import _apply_net_amount_dc_patch

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code, trade_date, net_mf_amount, net_amount_dc, fetched_at) VALUES "
        "('X.SZ','20240102',1.0,NULL,now()), "
        "('X.SZ','20240103',2.0,NULL,now())"
    )
    patch = pd.DataFrame([
        {"ts_code": "X.SZ", "trade_date": "20240102", "net_amount_dc": 10.0},
        {"ts_code": "X.SZ", "trade_date": "20240103", "net_amount_dc": 20.0},
    ])
    n = _apply_net_amount_dc_patch(con, patch)
    assert n == 2
    rows = con.execute(
        "SELECT trade_date, net_amount_dc FROM ods_moneyflow WHERE ts_code='X.SZ' ORDER BY 1"
    ).fetchall()
    assert rows == [("20240102", 10.0), ("20240103", 20.0)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py::test_backfill_net_amount_dc_stock_bulk_update -v`

Expected: FAIL — `_apply_net_amount_dc_patch` not defined

- [ ] **Step 3: Implement bulk patch helpers and wire into stock backfill**

Add to `backend/fetch/ods_daily.py` (after `_insert_ods_moneyflow`):

```python
def _apply_net_amount_dc_patch(con, df, register_name: str = "_dc_patch") -> int:
    """Bulk UPDATE ods_moneyflow.net_amount_dc where currently NULL."""
    if df is None or df.empty:
        return 0
    con.register(register_name, df)
    before = con.execute(
        f"""
        SELECT COUNT(*)
        FROM ods_moneyflow o
        JOIN {register_name} p
          ON o.ts_code = p.ts_code AND o.trade_date = p.trade_date
        WHERE o.net_amount_dc IS NULL AND p.net_amount_dc IS NOT NULL
        """
    ).fetchone()[0]
    con.execute(
        f"""
        UPDATE ods_moneyflow AS o
        SET net_amount_dc = p.net_amount_dc, fetched_at = now()
        FROM {register_name} AS p
        WHERE o.ts_code = p.ts_code
          AND o.trade_date = p.trade_date
          AND o.net_amount_dc IS NULL
          AND p.net_amount_dc IS NOT NULL
        """
    )
    con.unregister(register_name)
    return int(before)


def _apply_circ_mv_patch(con, df, register_name: str = "_circ_patch") -> int:
    """Bulk upsert circ_mv into ods_daily_basic (NULL-only update + missing INSERT)."""
    if df is None or df.empty:
        return 0
    import pandas as pd
    con.register(register_name, df)
    # INSERT missing rows
    con.execute(
        f"""
        INSERT INTO ods_daily_basic (ts_code, trade_date, circ_mv, fetched_at)
        SELECT p.ts_code, p.trade_date, p.circ_mv, now()
        FROM {register_name} p
        LEFT JOIN ods_daily_basic b
          ON b.ts_code = p.ts_code AND b.trade_date = p.trade_date
        WHERE b.ts_code IS NULL AND p.circ_mv IS NOT NULL
        """
    )
    con.execute(
        f"""
        UPDATE ods_daily_basic AS b
        SET circ_mv = p.circ_mv, fetched_at = now()
        FROM {register_name} AS p
        WHERE b.ts_code = p.ts_code
          AND b.trade_date = p.trade_date
          AND b.circ_mv IS NULL
          AND p.circ_mv IS NOT NULL
        """
    )
    n = con.execute(
        f"""
        SELECT COUNT(*)
        FROM {register_name} p
        WHERE p.circ_mv IS NOT NULL
        """
    ).fetchone()[0]
    con.unregister(register_name)
    return int(n)
```

Refactor `_backfill_net_amount_dc_stock` body to build DataFrame then call `_apply_net_amount_dc_patch`. Same for `_backfill_circ_mv_stock` → `_apply_circ_mv_patch`. **Keep function signatures unchanged** for backward compat.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py -v`

Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/ods_daily.py tests/test_fetch/test_backfill_dde_meta.py
git commit -m "perf(backfill): bulk register+UPDATE for dc/circ ODS patches"
```

---

### Task 3: 3 线程 parallel stock backfill

**Files:**
- Modify: `backend/fetch/backfill_dde_meta.py`
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write the failing test**

```python
def test_backfill_dde_meta_ods_parallel_two_stocks(db_with_schema, monkeypatch):
    from backend.fetch.backfill_dde_meta import backfill_dde_meta_ods_parallel

    con = db_with_schema
    con.execute(
        "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) "
        "VALUES ('20240101', 0, 0), ('20240102', 1, 0)"
    )
    for code in ("P1.SZ", "P2.SZ"):
        con.execute(
            f"INSERT INTO ods_stock_basic (ts_code, list_date) VALUES ('{code}','20200101')"
        )
        con.execute(
            f"INSERT INTO ods_daily VALUES "
            f"('{code}','20240102',10,12,9,11,100,1000,0,1.0,now())"
        )
        con.execute(
            f"INSERT INTO ods_moneyflow "
            f"(ts_code,trade_date,net_mf_amount,net_amount_dc,fetched_at) "
            f"VALUES ('{code}','20240102',1.0,NULL,now())"
        )

    calls = []

    class FakeClient:
        def call(self, api, **kwargs):
            calls.append((api, kwargs.get("ts_code")))
            return [{"trade_date": "20240102", "net_amount": 42.0}]

    stats = backfill_dde_meta_ods_parallel(
        con, FakeClient(), None, "20240102", "20240102",
        workers=2, dry_run=False,
    )
    assert stats["dc_rows_updated"] == 2
    assert set(c[1] for c in calls if c[0] == "moneyflow_dc") == {"P1.SZ", "P2.SZ"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py::test_backfill_dde_meta_ods_parallel_two_stocks -v`

Expected: FAIL — `backfill_dde_meta_ods_parallel` not defined

- [ ] **Step 3: Implement parallel orchestrator**

Add `backfill_dde_meta_ods_parallel` to `backfill_dde_meta.py`:

```python
def _backfill_one_stock(
    ts_code: str,
    start: str,
    end: str,
    all_days: list[str],
    dry_run: bool,
) -> dict:
    """Worker: own DuckDB connection + TushareClient (matches fetch parallel pattern)."""
    from backend.config import DUCKDB_PATH
    from backend.fetch.client import TushareClient
    import duckdb

    local_stats = {
        "dc_api_calls": 0, "circ_api_calls": 0,
        "dc_rows_updated": 0, "circ_rows_updated": 0,
        "dc_null_days": 0, "circ_null_days": 0,
    }
    thread_con = duckdb.connect(DUCKDB_PATH)
    try:
        dc_ranges = _get_net_amount_dc_null_ranges(thread_con, ts_code, all_days)
        circ_ranges = _get_circ_mv_null_ranges(thread_con, ts_code, all_days)
        local_stats["dc_null_days"] += _count_days_in_ranges(dc_ranges, all_days)
        local_stats["circ_null_days"] += _count_days_in_ranges(circ_ranges, all_days)
        if dry_run:
            if dc_ranges:
                local_stats["dc_api_calls"] += 1
            if circ_ranges:
                local_stats["circ_api_calls"] += 1
            return local_stats
        client = TushareClient()
        for seg_start, seg_end in dc_ranges:
            local_stats["dc_api_calls"] += 1
            local_stats["dc_rows_updated"] += _backfill_net_amount_dc_stock(
                thread_con, client, ts_code, seg_start, seg_end,
            )
        for seg_start, seg_end in circ_ranges:
            local_stats["circ_api_calls"] += 1
            local_stats["circ_rows_updated"] += _backfill_circ_mv_stock(
                thread_con, client, ts_code, seg_start, seg_end,
            )
        return local_stats
    finally:
        thread_con.close()


def backfill_dde_meta_ods_parallel(
    con,
    client,  # unused in workers; kept for CLI signature compat
    ts_codes: Optional[List[str]],
    start: str,
    end: str,
    dry_run: bool = False,
    workers: int = 3,
    sync_dwd_batch: int = 0,
    on_batch_sync=None,
) -> dict:
    """Parallel ODS backfill; optional DWD sync every N completed stocks."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    codes = _resolve_universe(con, ts_codes)
    all_days = _local_trading_days(con, start, end)
    if all_days is None:
        raise RuntimeError(
            f"dim_date does not cover [{start}, {end}]; run fetch/build_dim first"
        )

    # Bulk pre-filter: only schedule stocks with any gap (fast skip for done stocks)
    need_dc = set(list_stocks_needing_dc_backfill(con, start, end, ts_codes=codes))
    need_circ = set(list_stocks_needing_circ_backfill(con, start, end, ts_codes=codes))
    work_codes = sorted(need_dc | need_circ)
    skip_count = len(codes) - len(work_codes)

    stats = {
        "stocks": len(codes),
        "stocks_skipped": skip_count,
        "stocks_work": len(work_codes),
        "dc_api_calls": 0,
        "circ_api_calls": 0,
        "dc_rows_updated": 0,
        "circ_rows_updated": 0,
        "dc_null_days": 0,
        "circ_null_days": 0,
        "dwd_sync_batches": 0,
    }

    from backend.etl.progress import stock_progress
    prog = stock_progress("fetch.dde_meta", len(work_codes), detail="DDE元数据补洞")
    prog.log_start(range=f"{start}~{end}", workers=workers, skipped=skip_count)

    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_backfill_one_stock, c, start, end, all_days, dry_run): c
            for c in work_codes
        }
        for fut in as_completed(futures):
            part = fut.result()
            for k in ("dc_api_calls", "circ_api_calls", "dc_rows_updated",
                      "circ_rows_updated", "dc_null_days", "circ_null_days"):
                stats[k] += part[k]
            completed += 1
            prog.tick()
            if (
                sync_dwd_batch > 0
                and on_batch_sync is not None
                and not dry_run
                and completed % sync_dwd_batch == 0
            ):
                on_batch_sync(con)
                stats["dwd_sync_batches"] += 1

    if sync_dwd_batch > 0 and on_batch_sync is not None and not dry_run and completed % sync_dwd_batch != 0:
        on_batch_sync(con)
        stats["dwd_sync_batches"] += 1

    prog.log_done(stocks=len(work_codes), skipped=skip_count)
    logger.info("backfill_dde_meta_ods_parallel complete: %s", stats)
    return stats
```

Keep `backfill_dde_meta_ods` as thin wrapper calling `backfill_dde_meta_ods_parallel(..., workers=1)` for tests that use FakeClient injected into single-thread path — **or** update tests to patch `_backfill_one_stock`. Prefer wrapper:

```python
def backfill_dde_meta_ods(con, client, ts_codes, start, end, dry_run=False, **kwargs):
    return backfill_dde_meta_ods_parallel(
        con, client, ts_codes, start, end,
        dry_run=dry_run, workers=kwargs.get("workers", 1), **kwargs,
    )
```

For unit tests with FakeClient: when `workers=1`, use inline loop with passed `client` instead of spawning thread (avoid FakeClient in thread). Implement `_backfill_one_stock(..., client=None)` — if client provided, don't create TushareClient.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py -v`

Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/backfill_dde_meta.py tests/test_fetch/test_backfill_dde_meta.py
git commit -m "perf(backfill): 3-worker parallel stock backfill with bulk gap prefilter"
```

---

### Task 4: CLI `--workers` + `--sync-dwd-batch`

**Files:**
- Modify: `backend/cli.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add CLI flags**

In `cmd_backfill_dde_meta`:

```python
workers = args.workers  # new arg, default 3
sync_batch = args.sync_dwd_batch or 0

def _sync_hook(con):
    from backend.etl.sync_dwd_dde_meta import sync_dwd_dde_meta
    sync_dwd_dde_meta(con, ts_codes=args.ts_code, since=since)

stats = backfill_dde_meta_ods(
    con, client, args.ts_code, start, end,
    dry_run=args.dry_run,
    workers=workers,
    sync_dwd_batch=sync_batch if args.sync_dwd else 0,
    on_batch_sync=_sync_hook if sync_batch and args.sync_dwd else None,
)
# Final sync if --sync-dwd and sync_batch==0 (existing behavior)
if args.sync_dwd and sync_batch == 0 and not args.dry_run:
    sync_stats = sync_dwd_dde_meta(con, ts_codes=args.ts_code, since=since)
```

Parser additions:

```python
bdm.add_argument("--workers", type=int, default=3,
                 help="Parallel stock workers (default 3; use 1 for debug)")
bdm.add_argument("--sync-dwd-batch", type=int, default=500,
                 help="DWD sync every N completed gap-stocks (0=end only; requires --sync-dwd)")
```

- [ ] **Step 2: Update CLAUDE.md 运维命令**

```bash
# 全市场补洞（推荐 v2：3 线程 + 每 500 股 sync DWD）
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --sync-dwd --workers 3 --sync-dwd-batch 500

# 断点续跑（已补 ODS 自动 skip）+ 仅 DWD 对齐
python -m backend.cli backfill-dde-meta --sync-dwd-only

# dry-run 观测 API 调用量
python -m backend.cli backfill-dde-meta --days 900 --dry-run --workers 3
```

- [ ] **Step 3: Manual smoke**

Run: `python -m backend.cli backfill-dde-meta --ts-code 000011.SZ 000967.SZ --days 900 --sync-dwd --workers 2`

Expected: 秒级完成（pilot 已补），DWD sync 行数 > 0 或 drift=0

- [ ] **Step 4: Commit**

```bash
git add backend/cli.py CLAUDE.md
git commit -m "feat(cli): backfill-dde-meta --workers and --sync-dwd-batch"
```

---

### Task 5: 等价性 oracle 测试（v1 vs v2 写结果一致）

**Files:**
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write equivalence test**

```python
def test_bulk_dc_patch_matches_row_by_row_oracle(db_with_schema):
    """Bulk path must match legacy row-by-row semantics on same API payload."""
    import pandas as pd
    from backend.fetch.ods_daily import _apply_net_amount_dc_patch

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code, trade_date, net_mf_amount, net_amount_dc, fetched_at) VALUES "
        "('Y.SZ','20240102',1.0,NULL,now()), "
        "('Y.SZ','20240103',2.0,5.0,now())"  # already set — must not overwrite
    )
    patch = pd.DataFrame([
        {"ts_code": "Y.SZ", "trade_date": "20240102", "net_amount_dc": 10.0},
        {"ts_code": "Y.SZ", "trade_date": "20240103", "net_amount_dc": 99.0},
    ])
    n = _apply_net_amount_dc_patch(con, patch)
    assert n == 1  # only NULL row updated
    assert con.execute(
        "SELECT net_amount_dc FROM ods_moneyflow WHERE ts_code='Y.SZ' AND trade_date='20240103'"
    ).fetchone()[0] == 5.0
```

- [ ] **Step 2: Run full test suite for module**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py tests/test_etl/test_sync_dwd_dde_meta.py -v`

Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_fetch/test_backfill_dde_meta.py
git commit -m "test(backfill): lock bulk patch NULL-only semantics"
```

---

### Task 6: 实库续跑 + 验收

**Files:** (ops only, no code)

- [ ] **Step 1: 先 sync 已有 ODS → DWD（中断恢复）**

Run:

```bash
python -m backend.cli backfill-dde-meta --sync-dwd-only
```

Expected stdout 含 `DWD sync:` 且 `dc_updated` / `circ_updated` > 0（或 drift 已为 0 则 =0）

- [ ] **Step 2: 全市场 parallel 续跑**

Run:

```bash
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --sync-dwd --workers 3 --sync-dwd-batch 500
```

Expected:
- 开头 `skipped=N`（已完成股）
- 墙钟 ≤60min
- 无 ERROR Traceback；限流仅 INFO `Rate limit [...] sleeping`

- [ ] **Step 3: 验收 SQL**

```sql
-- V1 dc coverage
SELECT
  COUNT(*) AS n,
  SUM(CASE WHEN d.net_amount_dc IS NOT NULL THEN 1 ELSE 0 END) AS has_dc
FROM dwd_daily_moneyflow d
JOIN dwd_daily_quote q USING (ts_code, trade_date)
WHERE d.trade_date >= '20230911' AND q.is_suspended = 0;

-- V2 weekly trend non-null
SELECT COUNT(*), SUM(CASE WHEN trend IS NULL THEN 1 ELSE 0 END)
FROM v_dws_dde_weekly_latest
WHERE trade_date = (SELECT MAX(trade_date) FROM dim_date WHERE is_week_end=1 AND trade_date <= '20260612');
```

Pass: dc coverage ≥85%; trend_null rate ≤20%

- [ ] **Step 4: calc + export（原 plan Phase 3）**

```bash
python -m backend.cli calc --date 20260612 --force
python -m backend.cli refresh-state --date 20260612
python -m backend.cli export --date 20260612
```

- [ ] **Step 5: Commit plan cross-ref**

Update `docs/superpowers/plans/2026-06-12-weekly-dde-trend-backfill.md` header with:

```markdown
**Perf follow-up:** `docs/superpowers/plans/2026-06-12-backfill-dde-meta-perf.md`
```

---

## Self-Review

**1. Spec coverage**

| Requirement | Task |
|-------------|------|
| 不断点丢 ODS | 已有 + Task 6 Step 1 |
| 加速 API 阶段 | Task 2–3 |
| DWD 不再等全量 | Task 3–4 `--sync-dwd-batch` |
| B4 语义不变 | Task 2 NULL-only bulk UPDATE; Task 5 oracle |
| 可 dry-run | 保留 + bulk prefilter 兼容 |

**2. Placeholder scan:** 无 TBD/TODO/similar-to

**3. Type consistency:** `backfill_dde_meta_ods` wrapper 保留原签名；`on_batch_sync(con)` 与 `sync_dwd_dde_meta(con, ...)` 一致

---

## 运维决策树（实施后）

```
backfill-dde-meta 该怎么跑？
├─ 首次 / 续跑全市场 ODS+DWD
│   └─ --sync-dwd --workers 3 --sync-dwd-batch 500
├─ 中断后只想对齐 DWD
│   └─ --sync-dwd-only
├─ 只看还要多少 API
│   └─ --dry-run --workers 3
└─ 调试单股
    └─ --ts-code X --workers 1
```

**禁止：** 为提速改 weekly trend 回退 net_mf；为提速 `rebuild_all_dwd` 全库。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-12-backfill-dde-meta-perf.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — 每 Task 派 fresh subagent，Task 间 review

**2. Inline Execution** — 本会话按 executing-plans 批量执行 + checkpoint

**Which approach?**
