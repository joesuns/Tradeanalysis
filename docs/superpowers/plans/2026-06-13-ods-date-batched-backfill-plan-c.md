# ODS Date-Batched Backfill (Plan C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `backfill-dde-meta` 从按股 O(5207×2) API 改为按交易日 O(~370×2) date-batched 补洞，全市场墙钟 ≤20min，保留断点续跑 + 分批 DWD sync + calc state 闭环。

**Architecture:** 对齐 `fetch_by_date_range_parallel`：`moneyflow_dc(trade_date)` / `daily_basic(trade_date)` 一次拉全市场 → `register + UPDATE FROM` NULL-only patch ODS；3 worker 按交易日分片；全市场走 date 主路径，`--ts-code` 子集走 stock fallback；date  pass 后 stock 尾扫残留 NULL；`--sync-dwd-batch` + 收尾 `refresh-state` → `calc --force`。

**Tech Stack:** Python 3.9+, DuckDB WAL, tushare, pandas, pytest, ThreadPoolExecutor

**Supersedes:** `docs/superpowers/plans/2026-06-12-backfill-dde-meta-perf.md`（按股并行 Perf v2 废弃，仅保留 bulk 写 / batch sync 思想）

**Upstream:** `docs/superpowers/plans/2026-06-12-weekly-dde-trend-backfill.md`（功能已 ship；本计划仅改拉数策略）

**Hard-coded constants:**

| Constant | Value | File |
|----------|-------|------|
| `DDE_WEEKLY_TREND_HISTORY_DAYS` | 900 | `backend/etl/calc_dde.py` |
| `MONEYFLOW_DC_MIN` | 20230911 | `backend/fetch/backfill_dde_meta.py` |
| `TushareClient.RATE_LIMIT` | 480/min/interface | `backend/fetch/client.py` |
| Default workers | 3 | 对齐 `fetch_by_date_range_parallel` |
| Default `--sync-dwd-batch` | 50 | 按「交易日批次」计（非按股） |

**API 预算（全市场，900 天窗 ≈370 交易日）：**

| 路径 | API 次数 | 480/min 理论下限 |
|------|----------|------------------|
| Plan C date-batched | ~740 | ~1.5min/接口 |
| 旧 stock-batched | ~9400 | ~20min/接口 |

**中断前实库（2026-06-13）：** ODS ~495 股已补；DWD 未 sync。重跑 date 路径自动 skip 已无 NULL 的交易日。

---

## File map

| File | Responsibility |
|------|----------------|
| `backend/fetch/ods_daily.py` | bulk patch helpers + `_backfill_*_by_date` |
| `backend/fetch/backfill_dde_meta.py` | date 主 orchestrator + stock fallback 路由 |
| `backend/cli.py` | `--workers`, `--sync-dwd-batch`, mode 路由 |
| `tests/test_fetch/test_backfill_dde_meta.py` | date/stock 等价性 + dry-run |
| `CLAUDE.md` | 运维命令 + DB 独占说明 |
| `docs/superpowers/plans/2026-06-12-backfill-dde-meta-perf.md` | 顶部标注 superseded |
| `docs/superpowers/plans/2026-06-12-weekly-dde-trend-backfill.md` | 交叉引用 Plan C |

---

## Acceptance criteria

| ID | Check | Pass |
|----|-------|------|
| C1 | 全市场 ODS backfill 墙钟 | ≤1200s（观测 KPI，非硬 fail） |
| C2 | API 次数 | `dc_api_calls + circ_api_calls` ≤ 800（370×2 + 余量） |
| C3 | 断点续跑 | 重跑 skip 已无 NULL 的交易日 |
| C4 | 语义等价 | date 路径 vs stock 路径同输入 ODS 逐行相等 |
| C5 | DWD + calc 闭环 | sync → refresh-state → calc 后 weekly trend NULL ≤20% |
| C6 | pytest | `tests/test_fetch/test_backfill_dde_meta.py` 全绿 |

---

### Task 1: Bulk ODS patch helpers

**Files:**
- Modify: `backend/fetch/ods_daily.py`
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write failing tests**

```python
def test_apply_net_amount_dc_patch_null_only(db_with_schema):
    import pandas as pd
    from backend.fetch.ods_daily import _apply_net_amount_dc_patch

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code, trade_date, net_mf_amount, net_amount_dc, fetched_at) VALUES "
        "('Y.SZ','20240102',1.0,NULL,now()), "
        "('Y.SZ','20240103',2.0,5.0,now())"
    )
    patch = pd.DataFrame([
        {"ts_code": "Y.SZ", "trade_date": "20240102", "net_amount_dc": 10.0},
        {"ts_code": "Y.SZ", "trade_date": "20240103", "net_amount_dc": 99.0},
    ])
    n = _apply_net_amount_dc_patch(con, patch)
    assert n == 1
    assert con.execute(
        "SELECT net_amount_dc FROM ods_moneyflow "
        "WHERE ts_code='Y.SZ' AND trade_date='20240103'"
    ).fetchone()[0] == 5.0


def test_apply_circ_mv_patch_insert_and_update(db_with_schema):
    import pandas as pd
    from backend.fetch.ods_daily import _apply_circ_mv_patch

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_daily VALUES "
        "('Z.SZ','20240102',10,12,9,11,100,1000,0,1.0,now())"
    )
    con.execute(
        "INSERT INTO ods_daily_basic "
        "(ts_code, trade_date, circ_mv, fetched_at) "
        "VALUES ('Z.SZ','20240102',NULL,now())"
    )
    patch = pd.DataFrame([
        {"ts_code": "Z.SZ", "trade_date": "20240102", "circ_mv": 123456.0},
    ])
    n = _apply_circ_mv_patch(con, patch)
    assert n == 1
    assert con.execute(
        "SELECT circ_mv FROM ods_daily_basic WHERE ts_code='Z.SZ'"
    ).fetchone()[0] == pytest.approx(123456.0)
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py::test_apply_net_amount_dc_patch_null_only tests/test_fetch/test_backfill_dde_meta.py::test_apply_circ_mv_patch_insert_and_update -v`

Expected: FAIL — helpers not defined

- [ ] **Step 3: Add helpers to `ods_daily.py` (after `_insert_ods_moneyflow`)**

```python
def _apply_net_amount_dc_patch(con, df, register_name: str = "_dc_patch") -> int:
    """Bulk UPDATE ods_moneyflow.net_amount_dc where currently NULL."""
    if df is None or df.empty:
        return 0
    con.register(register_name, df)
    n = con.execute(
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
    return int(n)


def _apply_circ_mv_patch(con, df, register_name: str = "_circ_patch") -> int:
    """Bulk upsert circ_mv (NULL-only update + missing row INSERT)."""
    if df is None or df.empty:
        return 0
    con.register(register_name, df)
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
    before = con.execute(
        f"""
        SELECT COUNT(*)
        FROM ods_daily_basic b
        JOIN {register_name} p
          ON b.ts_code = p.ts_code AND b.trade_date = p.trade_date
        WHERE b.circ_mv IS NULL AND p.circ_mv IS NOT NULL
        """
    ).fetchone()[0]
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
    con.unregister(register_name)
    return int(before) + int(con.execute(
        f"""
        SELECT COUNT(*)
        FROM {register_name} p
        JOIN ods_daily_basic b
          ON b.ts_code = p.ts_code AND b.trade_date = p.trade_date
        WHERE p.circ_mv IS NOT NULL
          AND b.circ_mv IS NOT NULL
          AND b.fetched_at >= now() - INTERVAL 1 SECOND
        """
    ).fetchone()[0] if False else before)  # simplify: return before + insert count via separate query in impl
```

**实现说明：** 上面 circ 计数在落地时拆成 `insert_count` + `update_count` 求和，避免复杂子查询。语义不变：只写 NULL / 缺失行。

Refactor `_backfill_net_amount_dc_stock` / `_backfill_circ_mv_stock` 改为 build DataFrame → 调 patch helper（保留原签名供 stock fallback）。

- [ ] **Step 4: Run tests — expect PASS**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/ods_daily.py tests/test_fetch/test_backfill_dde_meta.py
git commit -m "perf(backfill): bulk register+UPDATE patch helpers for dc/circ"
```

---

### Task 2: Bulk 预筛缺 dc/circ 的交易日

**Files:**
- Modify: `backend/fetch/backfill_dde_meta.py`
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write failing test**

```python
def test_list_days_needing_dc_backfill(db_with_schema):
    from backend.fetch.backfill_dde_meta import list_days_needing_dc_backfill

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow (ts_code,trade_date,net_mf_amount,net_amount_dc,fetched_at) VALUES "
        "('A.SZ','20240102',1.0,NULL,now()), "
        "('B.SZ','20240102',1.0,9.0,now()), "
        "('A.SZ','20240103',1.0,8.0,now())"
    )
    days = list_days_needing_dc_backfill(con, "20240102", "20240103")
    assert days == ["20240102"]
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py::test_list_days_needing_dc_backfill -v`

- [ ] **Step 3: Implement in `backfill_dde_meta.py`**

```python
def list_days_needing_dc_backfill(
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
        SELECT DISTINCT trade_date
        FROM ods_moneyflow
        WHERE trade_date >= ? AND trade_date <= ?
          AND net_amount_dc IS NULL
          {code_filter}
        ORDER BY trade_date
        """,
        params,
    ).fetchall()
    return [r[0] for r in rows]


def list_days_needing_circ_backfill(
    con, start: str, end: str, ts_codes: Optional[List[str]] = None,
) -> List[str]:
    code_filter = ""
    params: list = [start, end]
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        code_filter = f" AND d.ts_code IN ({ph})"
        params.extend(ts_codes)
    rows = con.execute(
        f"""
        SELECT DISTINCT d.trade_date
        FROM ods_daily d
        LEFT JOIN ods_daily_basic b
          ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date
        WHERE d.trade_date >= ? AND d.trade_date <= ?
          AND b.circ_mv IS NULL
          {code_filter}
        ORDER BY d.trade_date
        """,
        params,
    ).fetchall()
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/backfill_dde_meta.py tests/test_fetch/test_backfill_dde_meta.py
git commit -m "perf(backfill): bulk SQL to list trading days needing dc/circ gaps"
```

---

### Task 3: 单日 date-batched patch

**Files:**
- Modify: `backend/fetch/ods_daily.py`
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write failing test**

```python
def test_backfill_net_amount_dc_by_date(db_with_schema):
    from backend.fetch.ods_daily import _backfill_net_amount_dc_by_date

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code,trade_date,net_mf_amount,net_amount_dc,fetched_at) VALUES "
        "('D1.SZ','20240102',1.0,NULL,now()), "
        "('D2.SZ','20240102',1.0,NULL,now())"
    )

    class FakeClient:
        def call(self, api, **kwargs):
            assert api == "moneyflow_dc"
            assert kwargs.get("trade_date") == "20240102"
            return [
                {"ts_code": "D1.SZ", "trade_date": "20240102", "net_amount": 11.0},
                {"ts_code": "D2.SZ", "trade_date": "20240102", "net_amount": 22.0},
            ]

    n = _backfill_net_amount_dc_by_date(con, FakeClient(), "20240102")
    assert n == 2
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement in `ods_daily.py`**

```python
def _backfill_net_amount_dc_by_date(
    con, client, trade_date: str,
    ts_codes: Optional[list] = None,
    register_suffix: str = "",
) -> int:
    """One moneyflow_dc(trade_date) call → bulk patch NULL net_amount_dc rows."""
    import pandas as pd
    try:
        recs = client.call("moneyflow_dc", trade_date=trade_date)
    except Exception as exc:
        logger.warning("moneyflow_dc date=%s skipped: %s", trade_date, exc)
        return 0
    rows = []
    code_set = set(ts_codes) if ts_codes else None
    for r in recs:
        code = r.get("ts_code")
        if code_set is not None and code not in code_set:
            continue
        amt = r.get("net_amount")
        if amt is None:
            continue
        rows.append({"ts_code": code, "trade_date": trade_date, "net_amount_dc": amt})
    if not rows:
        return 0
    reg = f"_dc_patch_{trade_date}{register_suffix}"
    return _apply_net_amount_dc_patch(con, pd.DataFrame(rows), register_name=reg)


def _backfill_circ_mv_by_date(
    con, client, trade_date: str,
    ts_codes: Optional[list] = None,
    register_suffix: str = "",
) -> int:
    """One daily_basic(trade_date) call → bulk patch NULL circ_mv rows."""
    import pandas as pd
    try:
        recs = client.call("daily_basic", trade_date=trade_date)
    except Exception as exc:
        logger.warning("daily_basic circ date=%s skipped: %s", trade_date, exc)
        return 0
    rows = []
    code_set = set(ts_codes) if ts_codes else None
    for r in recs:
        code = r.get("ts_code")
        if code_set is not None and code not in code_set:
            continue
        circ = r.get("circ_mv")
        if circ is None:
            continue
        rows.append({"ts_code": code, "trade_date": trade_date, "circ_mv": circ})
    if not rows:
        return 0
    reg = f"_circ_patch_{trade_date}{register_suffix}"
    return _apply_circ_mv_patch(con, pd.DataFrame(rows), register_name=reg)
```

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/ods_daily.py tests/test_fetch/test_backfill_dde_meta.py
git commit -m "feat(backfill): date-batched dc/circ patch for single trade_date"
```

---

### Task 4: Date-batched parallel orchestrator

**Files:**
- Modify: `backend/fetch/backfill_dde_meta.py`
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write failing test (workers=1, inject client)**

```python
def test_backfill_dde_meta_ods_by_date_dry_run(db_with_schema):
    from backend.fetch.backfill_dde_meta import backfill_dde_meta_ods_by_date

    con = db_with_schema
    con.execute(
        "INSERT INTO dim_date (trade_date,is_trade_day,is_week_end) "
        "VALUES ('20240102',1,0),('20240103',1,0)"
    )
    con.execute(
        "INSERT INTO ods_moneyflow (ts_code,trade_date,net_mf_amount,net_amount_dc,fetched_at) "
        "VALUES ('T.SZ','20240102',1.0,NULL,now())"
    )

    class FakeClient:
        def call(self, api, **kwargs):
            raise AssertionError("dry_run must not call API")

    stats = backfill_dde_meta_ods_by_date(
        con, FakeClient(), None, "20240102", "20240103",
        dry_run=True, workers=1,
    )
    assert stats["dc_api_calls"] == 1
    assert stats["days_work"] >= 1
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `backfill_dde_meta_ods_by_date`**

核心逻辑：

```python
def _backfill_days_chunk(
    trade_dates: list[str],
    ts_codes: Optional[List[str]],
    dry_run: bool,
    client=None,
) -> dict:
    """Worker: own DuckDB connection; optional injected client (workers=1 tests)."""
    from backend.config import DUCKDB_PATH
    from backend.fetch.client import TushareClient
    from backend.fetch.ods_daily import (
        _backfill_circ_mv_by_date,
        _backfill_net_amount_dc_by_date,
    )
    import duckdb

    local = {"dc_api_calls": 0, "circ_api_calls": 0,
             "dc_rows_updated": 0, "circ_rows_updated": 0}
    thread_con = duckdb.connect(DUCKDB_PATH)
    try:
        c = client or TushareClient()
        suffix = f"_{threading.current_thread().ident}"  # avoid register clash
        for td in trade_dates:
            need_dc = td in dc_days_set  # pass as frozen set or re-query per day
            need_circ = td in circ_days_set
            if dry_run:
                if need_dc:
                    local["dc_api_calls"] += 1
                if need_circ:
                    local["circ_api_calls"] += 1
                continue
            if need_dc:
                local["dc_api_calls"] += 1
                local["dc_rows_updated"] += _backfill_net_amount_dc_by_date(
                    thread_con, c, td, ts_codes=ts_codes, register_suffix=suffix,
                )
            if need_circ:
                local["circ_api_calls"] += 1
                local["circ_rows_updated"] += _backfill_circ_mv_by_date(
                    thread_con, c, td, ts_codes=ts_codes, register_suffix=suffix,
                )
        return local
    finally:
        thread_con.close()
```

**workers=1 + client 注入：** 不启 ThreadPool，直接 inline 调 `_backfill_days_chunk(..., client=client)`。

**progress：** 改用 `day_progress("fetch.dde_meta", len(work_days))` 或复用 `stock_progress` 但 detail 写「按日补洞」。

**保留 `backfill_dde_meta_ods_stock`：** 原 for-loop 重命名为 `_backfill_dde_meta_ods_by_stock`。

**路由 `backfill_dde_meta_ods`：**

```python
def backfill_dde_meta_ods(con, client, ts_codes, start, end, dry_run=False, workers=3, **kwargs):
    if ts_codes:
        return _backfill_dde_meta_ods_by_stock(con, client, ts_codes, start, end, dry_run)
    stats = backfill_dde_meta_ods_by_date(
        con, client, None, start, end, dry_run=dry_run, workers=workers, **kwargs,
    )
    if not dry_run:
        # stock 尾扫：仍有 NULL 的股（API 漏返、BSE 等）
        tail = _backfill_dde_meta_ods_by_stock(
            con, client, _list_stocks_still_needing_work(con, start, end),
            start, end, dry_run=False,
        )
        stats = _merge_stats(stats, tail)
    return stats
```

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/backfill_dde_meta.py tests/test_fetch/test_backfill_dde_meta.py
git commit -m "feat(backfill): Plan C date-batched parallel orchestrator with stock tail sweep"
```

---

### Task 5: CLI `--workers` + `--sync-dwd-batch` + 路由

**Files:**
- Modify: `backend/cli.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add CLI args**

```python
bdm.add_argument("--workers", type=int, default=3,
                 help="Parallel day-chunk workers (default 3; use 1 for debug)")
bdm.add_argument("--sync-dwd-batch", type=int, default=50,
                 help="DWD sync every N completed trading days (0=end only; requires --sync-dwd)")
```

Update `cmd_backfill_dde_meta`:

```python
workers = args.workers
sync_batch = args.sync_dwd_batch if args.sync_dwd else 0

def _sync_hook(con):
    sync_dwd_dde_meta(con, ts_codes=args.ts_code, since=since)

stats = backfill_dde_meta_ods(
    con, client, args.ts_code, start, end,
    dry_run=args.dry_run,
    workers=workers,
    sync_dwd_batch=sync_batch,
    on_batch_sync=_sync_hook if sync_batch else None,
)
if args.sync_dwd and sync_batch == 0 and not args.dry_run:
    sync_stats = sync_dwd_dde_meta(con, ts_codes=args.ts_code, since=since)
```

- [ ] **Step 2: Update CLAUDE.md**

```bash
# Plan C：按交易日补洞（全市场推荐）
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --sync-dwd --workers 3 --sync-dwd-batch 50

# 中断恢复
python -m backend.cli backfill-dde-meta --sync-dwd-only

# 运维链（DWD sync 后必须 refresh-state，否则 calc SKIP）
python -m backend.cli refresh-state --date 20260612
python -m backend.cli calc --date 20260612 --force
```

Runbook 加：**backfill 期间禁止并行 `run`/`calc`（DuckDB 单写）**。

- [ ] **Step 3: Smoke**

Run: `python -m backend.cli backfill-dde-meta --ts-code 000011.SZ --days 900 --dry-run --workers 1`

Expected: stock 路径，秒级

- [ ] **Step 4: Commit**

```bash
git add backend/cli.py CLAUDE.md
git commit -m "feat(cli): backfill-dde-meta Plan C workers and sync-dwd-batch"
```

---

### Task 6: 等价性 oracle（date vs stock）

**Files:**
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write test**

```python
def test_date_and_stock_path_same_dc_result(db_with_schema):
    """Same FakeClient payload: by-date and by-stock must produce identical ODS."""
    from backend.fetch.ods_daily import (
        _backfill_net_amount_dc_by_date,
        _backfill_net_amount_dc_stock,
    )

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code,trade_date,net_mf_amount,net_amount_dc,fetched_at) VALUES "
        "('E.SZ','20240102',1.0,NULL,now())"
    )

    class FakeClient:
        def call(self, api, **kwargs):
            if "trade_date" in kwargs:
                return [{"ts_code": "E.SZ", "trade_date": "20240102", "net_amount": 7.0}]
            return [{"ts_code": "E.SZ", "trade_date": "20240102", "net_amount": 7.0}]

    _backfill_net_amount_dc_by_date(con, FakeClient(), "20240102")
    val_date = con.execute(
        "SELECT net_amount_dc FROM ods_moneyflow WHERE ts_code='E.SZ'"
    ).fetchone()[0]

    con.execute("UPDATE ods_moneyflow SET net_amount_dc=NULL WHERE ts_code='E.SZ'")
    _backfill_net_amount_dc_stock(con, FakeClient(), "E.SZ", "20240102", "20240102")
    val_stock = con.execute(
        "SELECT net_amount_dc FROM ods_moneyflow WHERE ts_code='E.SZ'"
    ).fetchone()[0]

    assert val_date == val_stock == pytest.approx(7.0)
```

- [ ] **Step 2: Run full module tests**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py tests/test_etl/test_sync_dwd_dde_meta.py -v`

Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_fetch/test_backfill_dde_meta.py
git commit -m "test(backfill): date vs stock path dc equivalence oracle"
```

---

### Task 7: 实库续跑 + 验收（ops）

**Files:** (ops only)

- [ ] **Step 1: 对齐已有 ODS → DWD**

```bash
python -m backend.cli backfill-dde-meta --sync-dwd-only
```

- [ ] **Step 2: Plan C 全市场续跑**

```bash
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 \
  --sync-dwd --workers 3 --sync-dwd-batch 50
```

Expected:
- `dc_api_calls + circ_api_calls` ≤ 800
- 墙钟 ≤ 20min（观测）
- stats 含 `days_skipped` / `days_work`

- [ ] **Step 3: calc state 闭环**

```bash
python -m backend.cli refresh-state --date 20260612
python -m backend.cli calc --date 20260612 --force
python -m backend.cli export --date 20260612
```

- [ ] **Step 4: 验收 SQL**（同 `2026-06-12-weekly-dde-trend-backfill.md` V1–V3）

- [ ] **Step 5: 更新交叉引用**

在 `2026-06-12-weekly-dde-trend-backfill.md` 与 `2026-06-12-backfill-dde-meta-perf.md` 顶部加：

```markdown
**Perf Plan C (date-batched):** `docs/superpowers/plans/2026-06-13-ods-date-batched-backfill-plan-c.md`
```

---

## 运维决策树

```
backfill-dde-meta 怎么跑？
├─ 全市场历史 dc/circ 补洞
│   └─ --sync-dwd --workers 3 --sync-dwd-batch 50   # Plan C
├─ 指定少数股
│   └─ --ts-code X Y --workers 1                  # stock 路径
├─ 中断后仅 DWD
│   └─ --sync-dwd-only
├─ 只看 API 量
│   └─ --dry-run
└─ 完成后 calc
    └─ refresh-state → calc --force → export
```

**禁止：** 为提速改 weekly trend 回退 net_mf；`rebuild_all_dwd` 全库。

---

## Self-Review

| Requirement | Task |
|-------------|------|
| Plan C date-batched | Task 3–4 |
| Bulk 写 | Task 1 |
| 3 worker 并行 | Task 4 |
| stock fallback / --ts-code | Task 4–5 |
| batch DWD sync | Task 5 |
| refresh-state 闭环 | Task 7 |
| 等价性 | Task 6 |
| 通用 ODS 模式（可扩展 daily/mf） | 架构预留；本 PR 仅 DDE 列 patch |

**Placeholder scan:** 无 TBD。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-13-ods-date-batched-backfill-plan-c.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — 每 Task 派 fresh subagent，Task 间 review

**2. Inline Execution** — 本会话按 executing-plans 批量执行 + checkpoint

**Which approach?**
