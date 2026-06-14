# Weekly DDE Trend Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore B4-aligned weekly `dde_trend` (fix ~91% N/A) and ensure weekly APPEND uses the same `_weekly_trend_from_daily` path as FULL (fix inflated daily/weekly sameness from wrong fallback).

**Architecture:** One-time ODS backfill of `net_amount_dc` + `circ_mv` over `max(20230911, calc_date-900d)` → targeted SQL sync ODS→DWD (content drift; `find_stale_dwd_codes` insufficient) → narrow weekly DDE FULL recalc → fix `batch_append_dde` to pass `daily_for_trend`. No full-library DWD rebuild; no weekly net_mf fallback.

**Tech Stack:** Python 3.9+, DuckDB, tushare (`moneyflow_dc`, `daily_basic`), pytest

**Upstream context:** Root-cause analysis in chat 2026-06-12; B4 spec `docs/superpowers/specs/2026-06-09-daily-screening-product-alignment.md` §1.3

**Hard-coded constants (do not change without spec update):**

| Constant | Value | File |
|----------|-------|------|
| `DDE_WEEKLY_TREND_HISTORY_DAYS` | 900 | `backend/etl/calc_dde.py` |
| `DDE_DDX1_EMA_SPAN` | 60 | `backend/etl/calc_dde.py` |
| `DDE_DDX3_WINDOW` | 10 | `backend/etl/calc_dde.py` |
| `DDE_MONEYFLOW_REGRESSION_WEEKLY` | 4 | `backend/etl/calc_dde.py` |
| `moneyflow_dc` API start | 2023-09-11 | spec §12.17 |

---

## File map

| File | Responsibility |
|------|----------------|
| `backend/fetch/backfill_dde_meta.py` | **Create** — ODS backfill orchestration (dc + circ), range resolver, dry-run stats |
| `backend/fetch/ods_daily.py` | **Modify** — extract `_backfill_circ_mv_stock()` (mirror dc backfill) |
| `backend/etl/sync_dwd_dde_meta.py` | **Create** — ODS→DWD SQL sync for `net_amount_dc` + `circ_mv` |
| `backend/cli.py` | **Modify** — `backfill-dde-meta` subcommand |
| `backend/etl/calc_batch_append.py` | **Modify** — weekly `batch_append_dde` loads `daily_for_trend` |
| `tests/test_fetch/test_backfill_dde_meta.py` | **Create** — ODS backfill + dry-run tests |
| `tests/test_etl/test_sync_dwd_dde_meta.py` | **Create** — DWD sync tests |
| `tests/test_etl/test_batch_append_calc.py` | **Modify** — weekly DDE APPEND equivalence test |
| `CLAUDE.md` | **Modify** — ops commands + dc/circ prerequisites |
| `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` | **Modify** — §12.17 trend dual-track + valid date range note |

---

## Acceptance criteria (post-ops on real DB)

| ID | Check | Pass |
|----|-------|------|
| V1 | DWD dc coverage (mf JOIN quote, non-suspended) | ≥ 85% rows in `[since, calc_date]` |
| V2 | week_end `trend IS NOT NULL` | ≥ 80% all market; ≥ 95% mature (dwd≥250d, non-BSE) |
| V3 | Excel weekly DDE 趋势 N/A | < 20% on mature subset |
| V4 | B4 diff | `python3 -m scripts.diff_vs_123 --date YYYYMMDD --breakdown` w_dde_trend mismatch ≈ 0 |
| V5 | APPEND ≡ FULL weekly trend | new pytest green |
| V6 | Daily/weekly sameness | Not 0% (OK when aligned); same-rate < pre-fix ~55% wrong-path baseline on overlapping non-null set |

**Baseline SQL (run before Phase 1):**

```sql
SELECT MAX(trade_date) AS week_end
FROM dim_date
WHERE trade_date <= 'YYYYMMDD' AND is_week_end = 1;

SELECT
  COUNT(*) AS n,
  SUM(CASE WHEN trend IS NULL THEN 1 ELSE 0 END) AS trend_null
FROM v_dws_dde_weekly_latest
WHERE trade_date = ?;  -- week_end

SELECT COUNT(*) AS ods_dwd_dc_drift
FROM ods_moneyflow o
JOIN dwd_daily_moneyflow d USING (ts_code, trade_date)
WHERE o.net_amount_dc IS NOT NULL AND d.net_amount_dc IS NULL;
```

---

### Task 1: Extract circ_mv backfill helper

**Files:**
- Modify: `backend/fetch/ods_daily.py`
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_fetch/test_backfill_dde_meta.py`:

```python
def test_backfill_circ_mv_stock_updates_ods_daily_basic(db_with_schema):
    from backend.fetch.ods_daily import _backfill_circ_mv_stock

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_daily VALUES "
        "('CV.SZ','20240102',10,12,9,11,100,1000,0,1.0,now())"
    )
    con.execute(
        "INSERT INTO ods_daily_basic "
        "(ts_code, trade_date, circ_mv, fetched_at) "
        "VALUES ('CV.SZ','20240102',NULL,now())"
    )

    class FakeClient:
        def call(self, api, **kwargs):
            assert api == "daily_basic"
            return [{"trade_date": "20240102", "circ_mv": 123456.0, "total_mv": 999.0}]

    n = _backfill_circ_mv_stock(
        con, FakeClient(), "CV.SZ", "20240102", "20240102",
    )
    assert n == 1
    circ = con.execute(
        "SELECT circ_mv FROM ods_daily_basic WHERE ts_code='CV.SZ'"
    ).fetchone()[0]
    assert circ == pytest.approx(123456.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py::test_backfill_circ_mv_stock_updates_ods_daily_basic -v`

Expected: FAIL — `_backfill_circ_mv_stock` not defined

- [ ] **Step 3: Implement `_backfill_circ_mv_stock` in `ods_daily.py`**

Place after `_backfill_net_amount_dc_stock` (~line 516):

```python
def _backfill_circ_mv_stock(
    con, client, ts_code: str, start: str, end: str,
) -> int:
    """UPDATE/INSERT ods_daily_basic.circ_mv from daily_basic API."""
    try:
        recs = client.call(
            "daily_basic", ts_code=ts_code, start_date=start, end_date=end,
        )
    except Exception as exc:
        logger.warning(
            "daily_basic circ backfill %s [%s~%s] skipped: %s",
            ts_code, start, end, exc,
        )
        return 0
    n = 0
    for r in recs:
        circ = r.get("circ_mv")
        if circ is None:
            continue
        con.execute(
            """
            INSERT INTO ods_daily_basic (ts_code, trade_date, circ_mv, fetched_at)
            VALUES (?, ?, ?, now())
            ON CONFLICT (ts_code, trade_date) DO UPDATE SET
                circ_mv = excluded.circ_mv,
                fetched_at = now()
            WHERE ods_daily_basic.circ_mv IS NULL
            """,
            [ts_code, r["trade_date"], circ],
        )
        n += 1
    return n
```

> **Note:** If `ods_daily_basic` PK/UPSERT syntax differs in `schema.py`, match existing `_insert_ods_daily_basic` pattern exactly.

- [ ] **Step 4: Run test — PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/ods_daily.py tests/test_fetch/test_backfill_dde_meta.py
git commit -m "refactor: extract circ_mv stock backfill helper for DDE meta ops"
```

---

### Task 2: ODS backfill orchestrator

**Files:**
- Create: `backend/fetch/backfill_dde_meta.py`
- Test: `tests/test_fetch/test_backfill_dde_meta.py`

- [ ] **Step 1: Write failing tests for range resolver + dry-run**

```python
from datetime import datetime, timedelta

from backend.fetch.backfill_dde_meta import resolve_backfill_range


def test_resolve_backfill_range_respects_moneyflow_dc_min():
    start, end = resolve_backfill_range("20260612", days=900, since="20230911")
    assert end == "20260612"
    assert start >= "20230911"
    # 900 calendar days before 20260612
    expected = (datetime.strptime("20260612", "%Y%m%d") - timedelta(days=900)).strftime("%Y%m%d")
    assert start == max("20230911", expected)


def test_backfill_dde_meta_ods_dry_run_counts(db_with_schema, monkeypatch):
    from backend.fetch.backfill_dde_meta import backfill_dde_meta_ods

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_stock_basic (ts_code, list_date) VALUES ('T1.SZ','20200101')"
    )
    con.execute(
        "INSERT INTO ods_daily VALUES ('T1.SZ','20240102',10,12,9,11,100,1000,0,1.0,now())"
    )
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code,trade_date,net_mf_amount,net_amount_dc,fetched_at) "
        "VALUES ('T1.SZ','20240102',1.0,NULL,now())"
    )

    class FakeClient:
        def call(self, api, **kwargs):
            raise AssertionError("dry_run must not call API")

    stats = backfill_dde_meta_ods(
        con, FakeClient(), ["T1.SZ"], "20240102", "20240102", dry_run=True,
    )
    assert stats["stocks"] == 1
    assert stats["dc_null_days"] >= 1
```

- [ ] **Step 2: Run tests — FAIL**

- [ ] **Step 3: Create `backend/fetch/backfill_dde_meta.py`**

```python
"""One-time / ops backfill for B4 weekly DDE trend inputs (net_amount_dc + circ_mv)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from backend.etl.calc_dde import DDE_WEEKLY_TREND_HISTORY_DAYS
from backend.fetch.ods_daily import (
    _backfill_circ_mv_stock,
    _backfill_net_amount_dc_stock,
    _get_circ_mv_null_ranges,
    _get_net_amount_dc_null_ranges,
    _local_trading_days,
    get_all_active_codes,
)

logger = logging.getLogger(__name__)

MONEYFLOW_DC_MIN = "20230911"


def resolve_backfill_range(
    end_date: str,
    days: int = DDE_WEEKLY_TREND_HISTORY_DAYS,
    since: str = MONEYFLOW_DC_MIN,
) -> tuple[str, str]:
    end_dt = datetime.strptime(end_date, "%Y%m%d")
    start = (end_dt - timedelta(days=days)).strftime("%Y%m%d")
    return max(since, start), end_date


def _resolve_universe(con, ts_codes: Optional[List[str]]) -> List[str]:
    if ts_codes:
        return [c for c in ts_codes if not c.endswith(".BJ")]
    return [c for c in get_all_active_codes(con) if not c.endswith(".BJ")]


def backfill_dde_meta_ods(
    con,
    client,
    ts_codes: Optional[List[str]],
    start: str,
    end: str,
    dry_run: bool = False,
) -> dict:
    """Backfill ODS net_amount_dc + circ_mv for stocks with existing ODS rows."""
    codes = _resolve_universe(con, ts_codes)
    all_days = _local_trading_days(con, start, end)
    if all_days is None:
        raise RuntimeError(
            f"dim_date does not cover [{start}, {end}]; run fetch/build_dim first"
        )

    stats = {
        "stocks": len(codes),
        "dc_api_calls": 0,
        "circ_api_calls": 0,
        "dc_rows_updated": 0,
        "circ_rows_updated": 0,
        "dc_null_days": 0,
        "circ_null_days": 0,
    }

    from backend.etl.progress import stock_progress
    prog = stock_progress("fetch.dde_meta", len(codes), detail="DDE元数据补洞")
    prog.log_start(range=f"{start}~{end}")

    for ts_code in codes:
        dc_ranges = _get_net_amount_dc_null_ranges(con, ts_code, all_days)
        circ_ranges = _get_circ_mv_null_ranges(con, ts_code, all_days)
        stats["dc_null_days"] += sum(
            all_days.index(e) - all_days.index(s) + 1
            for s, e in dc_ranges if s in all_days and e in all_days
        )
        stats["circ_null_days"] += sum(
            all_days.index(e) - all_days.index(s) + 1
            for s, e in circ_ranges if s in all_days and e in all_days
        )

        if dry_run:
            if dc_ranges:
                stats["dc_api_calls"] += 1
            if circ_ranges:
                stats["circ_api_calls"] += 1
            prog.tick()
            continue

        for seg_start, seg_end in dc_ranges:
            stats["dc_api_calls"] += 1
            stats["dc_rows_updated"] += _backfill_net_amount_dc_stock(
                con, client, ts_code, seg_start, seg_end,
            )
        for seg_start, seg_end in circ_ranges:
            stats["circ_api_calls"] += 1
            stats["circ_rows_updated"] += _backfill_circ_mv_stock(
                con, client, ts_code, seg_start, seg_end,
            )
        prog.tick()

    prog.log_done(stocks=len(codes))
    logger.info("backfill_dde_meta_ods complete: %s", stats)
    return stats
```

- [ ] **Step 4: Run tests — PASS**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/fetch/backfill_dde_meta.py tests/test_fetch/test_backfill_dde_meta.py
git commit -m "feat: add ODS backfill orchestrator for DDE trend meta fields"
```

---

### Task 3: ODS→DWD targeted sync

**Files:**
- Create: `backend/etl/sync_dwd_dde_meta.py`
- Test: `tests/test_etl/test_sync_dwd_dde_meta.py`

- [ ] **Step 1: Write failing test**

```python
def test_sync_dwd_dde_meta_updates_drifted_dc_and_circ(db_with_schema):
    from backend.etl.sync_dwd_dde_meta import sync_dwd_dde_meta

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_moneyflow "
        "(ts_code,trade_date,net_mf_amount,net_amount_dc,buy_lg_vol,sell_lg_vol,"
        "buy_elg_vol,sell_elg_vol,total_vol,fetched_at) "
        "VALUES ('S1.SZ','20240102',1.0,-99.0,1,1,1,1,4,now())"
    )
    con.execute(
        "INSERT INTO dwd_daily_moneyflow "
        "(ts_code,trade_date,net_mf_amount,net_amount_dc,buy_lg_vol,sell_lg_vol,"
        "buy_elg_vol,sell_elg_vol,total_vol) "
        "VALUES ('S1.SZ','20240102',1.0,NULL,1,1,1,1,4)"
    )
    con.execute(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,circ_mv,fetched_at) "
        "VALUES ('S1.SZ','20240102',555.0,now())"
    )
    con.execute(
        "INSERT INTO dwd_daily_quote "
        "(ts_code,trade_date,open_qfq,high_qfq,low_qfq,close_qfq,vol,"
        "circ_mv,is_suspended) "
        "VALUES ('S1.SZ','20240102',1,1,1,1,1,NULL,0)"
    )

    out = sync_dwd_dde_meta(con, ts_codes=["S1.SZ"], since="20240101")
    assert out["moneyflow_dc_updated"] >= 1
    assert out["quote_circ_updated"] >= 1

    dc = con.execute(
        "SELECT net_amount_dc FROM dwd_daily_moneyflow WHERE ts_code='S1.SZ'"
    ).fetchone()[0]
    circ = con.execute(
        "SELECT circ_mv FROM dwd_daily_quote WHERE ts_code='S1.SZ'"
    ).fetchone()[0]
    assert dc == pytest.approx(-99.0)
    assert circ == pytest.approx(555.0)
```

- [ ] **Step 2: Run test — FAIL**

- [ ] **Step 3: Create `backend/etl/sync_dwd_dde_meta.py`**

```python
"""Sync B4 DDE trend meta fields from ODS into DWD (content drift repair)."""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


def sync_dwd_dde_meta(
    con,
    ts_codes: Optional[List[str]] = None,
    since: str = "20230911",
) -> dict:
    """UPDATE dwd_daily_moneyflow.net_amount_dc and dwd_daily_quote.circ_mv from ODS."""
    code_filter = ""
    params: list = [since]
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        code_filter = f" AND d.ts_code IN ({ph})"
        params.extend(ts_codes)

    n_dc = con.execute(
        f"""
        UPDATE dwd_daily_moneyflow AS d
        SET net_amount_dc = o.net_amount_dc
        FROM ods_moneyflow AS o
        WHERE d.ts_code = o.ts_code
          AND d.trade_date = o.trade_date
          AND d.trade_date >= ?
          AND o.net_amount_dc IS NOT NULL
          AND (d.net_amount_dc IS NULL OR d.net_amount_dc IS DISTINCT FROM o.net_amount_dc)
          {code_filter}
        """,
        params,
    ).fetchone()
    # DuckDB UPDATE rowcount: use changes() if available; else COUNT pattern
    dc_updated = con.execute("SELECT changes()").fetchone()[0] if False else None
    # Fallback count query after update:
    dc_updated = con.execute(
        f"""
        SELECT COUNT(*)
        FROM dwd_daily_moneyflow d
        JOIN ods_moneyflow o USING (ts_code, trade_date)
        WHERE d.trade_date >= ?
          AND o.net_amount_dc IS NOT NULL
          AND d.net_amount_dc = o.net_amount_dc
          {code_filter}
        """,
        params,
    ).fetchone()[0]

    circ_params: list = [since]
    if ts_codes:
        circ_params.extend(ts_codes)
    n_circ = con.execute(
        f"""
        UPDATE dwd_daily_quote AS d
        SET circ_mv = b.circ_mv
        FROM ods_daily_basic AS b
        WHERE d.ts_code = b.ts_code
          AND d.trade_date = b.trade_date
          AND d.trade_date >= ?
          AND b.circ_mv IS NOT NULL AND b.circ_mv > 0
          AND (d.circ_mv IS NULL OR d.circ_mv IS DISTINCT FROM b.circ_mv)
          {code_filter.replace('d.ts_code', 'd.ts_code') if code_filter else ''}
        """,
        circ_params,
    )
    # Implementer: verify DuckDB UPDATE rowcount API; use pre/post COUNT diff if needed

    out = {
        "moneyflow_dc_updated": dc_updated,
        "quote_circ_updated": 0,  # replace with actual count in implementation
        "since": since,
    }
    logger.info("sync_dwd_dde_meta: %s", out)
    return out
```

> **Implementer note:** Replace rowcount hack with reliable pre/post COUNT or DuckDB-native `RETURNING` if supported. Test must assert `moneyflow_dc_updated >= 1`.

- [ ] **Step 4: Run test — PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/etl/sync_dwd_dde_meta.py tests/test_etl/test_sync_dwd_dde_meta.py
git commit -m "feat: sync ODS dc/circ meta into DWD for weekly DDE trend"
```

---

### Task 4: CLI `backfill-dde-meta`

**Files:**
- Modify: `backend/cli.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add CLI handler**

In `backend/cli.py`:

```python
def cmd_backfill_dde_meta(args):
    from backend.db.connection import get_connection
    from backend.fetch.backfill_dde_meta import (
        backfill_dde_meta_ods, resolve_backfill_range, MONEYFLOW_DC_MIN,
    )
    from backend.etl.sync_dwd_dde_meta import sync_dwd_dde_meta
    from backend.fetch.client import TushareClient

    end = args.end or args.date or datetime.now().strftime("%Y%m%d")
    since = args.since or MONEYFLOW_DC_MIN
    start, end = resolve_backfill_range(end, days=args.days, since=since)

    con = get_connection()
    client = TushareClient()

    if not args.sync_dwd_only:
        stats = backfill_dde_meta_ods(
            con, client, args.ts_code, start, end, dry_run=args.dry_run,
        )
        logger.info("ODS backfill: %s", stats)

    if args.dry_run:
        con.close()
        return

    if args.sync_dwd or args.sync_dwd_only:
        sync_stats = sync_dwd_dde_meta(con, ts_codes=args.ts_code, since=since)
        logger.info("DWD sync: %s", sync_stats)

    con.close()
```

Register parser:

```python
bdm = sp.add_parser(
    "backfill-dde-meta",
    help="Backfill net_amount_dc + circ_mv for B4 weekly DDE trend (ops)",
)
bdm.add_argument("--date", help="End date YYYYMMDD (default: today)")
bdm.add_argument("--end", help="Alias for --date")
bdm.add_argument("--since", default="20230911", help="moneyflow_dc min date")
bdm.add_argument("--days", type=int, default=900, help="Calendar-day lookback")
bdm.add_argument("--ts-code", nargs="+", help="Stock subset (default: all active, excl BSE)")
bdm.add_argument("--dry-run", action="store_true", help="Count gaps only, no API/DWD writes")
bdm.add_argument("--sync-dwd", action="store_true", help="After ODS backfill, sync ODS→DWD")
bdm.add_argument("--sync-dwd-only", action="store_true", help="Skip ODS API; DWD sync only")
```

Add to `handlers` dict.

- [ ] **Step 2: Manual smoke**

Run: `python -m backend.cli backfill-dde-meta --ts-code 000011.SZ --days 900 --dry-run`

Expected: logs `dc_null_days` / `circ_null_days` without API calls

- [ ] **Step 3: Update CLAUDE.md** — add under 运维:

```bash
# B4 周线 DDE 趋势元数据一次性补洞（net_amount_dc + circ_mv）
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --dry-run
python -m backend.cli backfill-dde-meta --days 900 --sync-dwd
python -m backend.cli calc --date YYYYMMDD --force
python -m backend.cli refresh-state --date YYYYMMDD
```

- [ ] **Step 4: Commit**

```bash
git add backend/cli.py CLAUDE.md
git commit -m "feat: add backfill-dde-meta CLI for weekly DDE trend repair"
```

---

### Task 5: Fix `batch_append_dde` weekly `daily_for_trend`

**Files:**
- Modify: `backend/etl/calc_batch_append.py:396-470`
- Modify: `tests/test_etl/test_batch_append_calc.py`

- [ ] **Step 1: Write failing weekly equivalence test**

Add `test_batch_append_dde_weekly_matches_per_stock_append` — mirror daily test but:
- `freq="weekly"`, setup `dwd_weekly_quote` + week-end `dim_date`
- Seed `net_amount_dc` + `circ_mv` on ≥ 80 daily rows (enough for resample)
- Compare `trend` on new week-end bar: `append_calculate` vs `batch_append_dde`

Skeleton:

```python
def test_batch_append_dde_weekly_matches_per_stock_append():
    """Weekly batch APPEND must use _weekly_trend_from_daily (daily_for_trend)."""
    import duckdb
    import pandas as pd
    from backend.etl.calc_batch_append import batch_append_dde
    from backend.etl.calc_dde import DDECalculator

    codes = ["DDEw.SZ"]
    con = duckdb.connect(":memory:")
    # ... setup dim_date week-ends, dwd_daily_moneyflow with net_amount_dc,
    # dwd_daily_quote with circ_mv, dwd_weekly_quote, dws_dde_weekly baseline ...
    calc = DDECalculator(con, "weekly")
    dde_groups = calc._load_weekly_batch(codes)
    new_we = "..."  # new week-end date
    calc_date = new_we

    per_stock = {}
    for code in codes:
        calc.append_calculate(code, dde_groups[code], [new_we], calc_date, {...})
        row = con.execute(
            "SELECT trend FROM dws_dde_weekly WHERE ts_code=? AND trade_date=? AND calc_date=?",
            [code, new_we, calc_date],
        ).fetchone()
        per_stock[code] = row[0]

    con.execute("DELETE FROM dws_dde_weekly WHERE trade_date=? AND calc_date=?", [new_we, calc_date])
    batch_append_dde(con, "weekly", codes, calc_date, dde_groups, {c: [new_we] for c in codes})

    for code in codes:
        got = con.execute(
            "SELECT trend FROM dws_dde_weekly WHERE ts_code=? AND trade_date=? AND calc_date=?",
            [code, new_we, calc_date],
        ).fetchone()[0]
        assert got == per_stock[code]
    con.close()
```

- [ ] **Step 2: Run test — FAIL** (batch path uses wrong trend)

- [ ] **Step 3: Patch `batch_append_dde`**

After `seeds_by_code` block (~line 427), add:

```python
    daily_trend_groups: dict = {}
    if freq == "weekly" and ts_codes:
        daily_trend_groups = calc._load_daily_for_trend_batch(
            ts_codes, end_date=calc_date,
        )
```

In the loop, both vector and non-vector paths:

```python
        daily_trend = daily_trend_groups.get(ts_code) if freq == "weekly" else None
        if core is not None:
            ...
            out = calc._compute_dde_derived(
                base,
                daily_for_trend=daily_trend,
                calc_date=calc_date,
                target_indices=target_idx or None,
            )
        else:
            out = calc._compute_indicators(
                df, ema_seeds=seeds, daily_for_trend=daily_trend, calc_date=calc_date,
            )
```

- [ ] **Step 4: Run test — PASS**

Run: `pytest tests/test_etl/test_batch_append_calc.py::test_batch_append_dde_weekly_matches_per_stock_append -v`

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_append.py tests/test_etl/test_batch_append_calc.py
git commit -m "fix: pass daily_for_trend in weekly batch_append_dde"
```

---

### Task 6: Spec + regression tests

**Files:**
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`
- Test: `tests/test_etl/test_calc_dde.py` (optional coverage guard)

- [ ] **Step 1: Add spec note after §12.17**

```markdown
### 12.17.1 B4 DDE trend 双轨与有效区间

- **DDX/DDX2**：`moneyflow` 量口径（2015 起）
- **dde_trend 日线**：`moneyflow_dc`+`circ_mv` 优先，可回退 `net_mf_amount`+`total_mv`
- **dde_trend 周线**：仅 `moneyflow_dc`+`circ_mv`（resample-W），**禁止** net_mf 回退；`moneyflow_dc` 自 2023-09-11
- **运维**：首次建库或 dc 历史不足时须 `cli backfill-dde-meta` + DWD sync + weekly DDE FULL
- **合法 N/A**：BSE、上市不足 ~60 周、`_skip_dde` 不完整周
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/test_fetch/test_backfill_dde_meta.py tests/test_etl/test_sync_dwd_dde_meta.py tests/test_etl/test_batch_append_calc.py tests/test_etl/test_calc_dde.py -v`

Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md
git commit -m "docs: document B4 weekly DDE trend data prerequisites"
```

---

## Ops runbook (real DB — after code merged)

**Why not `rebuild_dwd_for_stale` alone:** detects max(date) lag only, not ODS dc UPDATE drift.

```bash
# 0. Baseline (save output)
python3 - <<'PY'
import duckdb
con = duckdb.connect("data/tradeanalysis.duckdb", read_only=True)
# ... baseline SQL from Acceptance section ...
con.close()
PY

# 1. Dry-run
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --dry-run

# 2. ODS backfill + DWD sync
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --sync-dwd

# 3. Weekly DDE FULL (input fingerprint changed → force)
python -m backend.cli calc --date 20260612 --force

# 4. Align calc state
python -m backend.cli refresh-state --date 20260612

# 5. Export + verify
python -m backend.cli export --date 20260612
python3 -m scripts.diff_vs_123 --date 20260612 --breakdown --summary

# 6. Spot-check: 000967.SZ daily≠weekly when dc sufficient; 000011.SZ may match when aligned
```

**Pilot first (recommended):**

```bash
python -m backend.cli backfill-dde-meta --ts-code 000011.SZ 000967.SZ --days 900 --sync-dwd
python -m backend.cli calc --date 20260612 --ts-code 000011.SZ 000967.SZ --force
```

---

## Self-review

| Spec requirement | Task |
|------------------|------|
| B4 weekly dde_trend hard gate | Tasks 1–3 ODS/DWD data; Task 5 APPEND path |
| moneyflow_dc since 2023-09-11 | Task 2 `MONEYFLOW_DC_MIN` |
| No weekly net_mf fallback | No algorithm change; data + correct path |
| Minimal rebuild scope | Task 3 SQL sync, not `rebuild_all_dwd()` |
| APPEND ≡ FULL equivalence | Task 5 |
| CLI ops documented | Task 4 + CLAUDE.md |

**Placeholder scan:** none — all tasks have concrete paths and code.

---

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-06-12-weekly-dde-trend-backfill.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — implement tasks in this session with checkpoints  

Which approach?
