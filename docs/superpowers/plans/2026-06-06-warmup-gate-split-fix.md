# Warmup 门禁拆分修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拆分 calc 准入（日线 ≥250）与 fetch 门禁（成熟股 weekly ≥120 week-end），修复新股日线 calc 被误拦、无效 DWD rebuild、skip_log 根因丢失；补齐 health_check 阈值与 sideways 高亮。

**Architecture:** `check_data_completeness` 返回三桶：`ok`（daily_ok，可 calc）、`missing`（daily 不足，不可 calc）、`weekly_fetch`（daily_ok 但成熟股 week-end 历史仍不足，仅触发 fetch）。`weekly_required = min(120, available_week_ends_since_list_date)`。`run_calc` 只对 `missing` 写 skip_log；rebuild 仅在 `n_fetched > 0` 时针对实际 fetch 的 codes。

**Tech Stack:** Python 3.9+, DuckDB, pytest

**上游：** Code review + 交易专家建议（`2026-06-06-export-data-quality-fix.md` §3.3「上市不足 120 周除外」）

**硬编码常量（不可改）：**

| 常量 | 值 | 文件 |
|------|-----|------|
| `WARMUP_TDAYS` | 250 | `backend/etl/orchestrator.py` |
| `WEEKLY_WARMUP_WEEKS` | 120 | `backend/etl/orchestrator.py` |
| volume weekly window | 120 | `backend/etl/calc_volume.py` |

**不改动：** `calc_volume.py` 计算公式；Export 三分法逻辑；sideways schema CASE

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `backend/etl/orchestrator.py` | `_available_week_ends_batch`、`check_data_completeness` 三桶、`run_calc` fetch/rebuild、`_classify_still_missing` |
| `tests/test_etl/test_orchestrator.py` | 新股豁免、mature weekly_fetch、skip rebuild 单测 |
| `scripts/health_check.py` | `expect_min` + Section I 填充率 FAIL |
| `backend/export_wide.py` | sideways「均线走平」高亮 |
| `tests/test_export_wide.py` | `__w__` 列三分法单测（可选） |
| `CLAUDE.md` | G1 门禁语义更新 |
| `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` | calc vs fetch 门禁一句 |

---

### Task 1: `_available_week_ends_batch`

**Files:**
- Modify: `backend/etl/orchestrator.py:248`（`_count_week_end_bars_batch` 之前）
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_etl/test_orchestrator.py` 的 `# ── check_data_completeness batch ──` 之前插入：

```python
def test_available_week_ends_respects_list_date():
    """available_week_ends = [list_date, end_date] 内 dim_date week-end 数。"""
    import duckdb
    from backend.etl.orchestrator import _available_week_ends_batch

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    con.execute("INSERT INTO dim_stock VALUES ('IPO.SZ', '20260101', NULL)")
    con.execute("INSERT INTO dim_stock VALUES ('OLD.SZ', '20200101', NULL)")
    for td in _seq_week_end_dates(130, start=(2020, 1, 3)):
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
    for td in _seq_week_end_dates(10, start=(2026, 1, 2)):
        con.execute("INSERT OR IGNORE INTO dim_date VALUES (?, 1, 1)", [td])

    end = "20261231"
    counts = _available_week_ends_batch(con, ["IPO.SZ", "OLD.SZ"], end)
    assert counts["IPO.SZ"] == 10
    assert counts["OLD.SZ"] == 130

    con.close()
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `pytest tests/test_etl/test_orchestrator.py::test_available_week_ends_respects_list_date -v`

Expected: FAIL — `ImportError: cannot import name '_available_week_ends_batch'`

- [ ] **Step 3: Implement**

在 `backend/etl/orchestrator.py` 的 `_count_week_end_bars_batch` **之前**插入：

```python
def _available_week_ends_batch(con, ts_codes: list[str], end_date: str) -> dict:
    """Count week-end bars on dim_date between list_date and end_date (inclusive)."""
    if not ts_codes:
        return {}
    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(f"""
        SELECT s.ts_code, COUNT(d.trade_date)
        FROM dim_stock s
        LEFT JOIN dim_date d
          ON d.is_trade_day = 1 AND d.is_week_end = 1
         AND d.trade_date <= ?
         AND (s.list_date IS NULL OR d.trade_date >= s.list_date)
        WHERE s.ts_code IN ({placeholders})
        GROUP BY s.ts_code
    """, [end_date] + ts_codes).fetchall()
    return {r[0]: r[1] for r in rows}
```

- [ ] **Step 4: Run test — expect PASS**

Run: `pytest tests/test_etl/test_orchestrator.py::test_available_week_ends_respects_list_date -v`

- [ ] **Step 5: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "feat: add _available_week_ends_batch for IPO-aware weekly warmup"
```

---

### Task 2: `check_data_completeness` 三桶返回

**Files:**
- Modify: `backend/etl/orchestrator.py:265-326`
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: Write failing tests**

**Test A — 新股：daily_ok + week-end 不足但 available<120 → `ok`，不在 `missing`**

```python
def test_check_data_completeness_ipo_exempt_from_weekly_gate():
    """上市不足 120 周：daily_ok 即 ok，weekly 不足不阻断 calc。"""
    import duckdb
    from backend.etl.orchestrator import check_data_completeness

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dwd_daily_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_weekly_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    con.execute("INSERT INTO dim_stock VALUES ('IPO.SZ', '20260101', NULL)")
    for td in _seq_trade_dates(260):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('IPO.SZ', ?)", [td])
    for td in _seq_week_end_dates(50, start=(2026, 1, 2)):
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('IPO.SZ', ?)", [td])

    result = check_data_completeness(con, ["IPO.SZ"], calc_date="20261231", min_daily_rows=250)
    assert "IPO.SZ" in result["ok"]
    assert "IPO.SZ" not in result["missing"]
    assert "IPO.SZ" not in result.get("weekly_fetch", {})

    con.close()
```

**Test B — 成熟股：daily_ok + week-end<120 且 available≥120 → `ok` + `weekly_fetch`**

```python
def test_check_data_completeness_mature_weekly_fetch_bucket():
    """成熟股 week-end 不足：仍可 calc，但进 weekly_fetch 触发补历史。"""
    import duckdb
    from backend.etl.orchestrator import check_data_completeness, WEEKLY_WARMUP_WEEKS

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dwd_daily_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_weekly_quote (ts_code TEXT, trade_date TEXT)")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    con.execute("INSERT INTO dim_stock VALUES ('OLD.SZ', '20200101', NULL)")
    for td in _seq_week_end_dates(130, start=(2020, 1, 3)):
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
    for td in _seq_trade_dates(260):
        con.execute("INSERT INTO dwd_daily_quote VALUES ('OLD.SZ', ?)", [td])
    for td in _seq_week_end_dates(50, start=(2025, 1, 3)):
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('OLD.SZ', ?)", [td])

    result = check_data_completeness(con, ["OLD.SZ"], calc_date="20261231", min_daily_rows=250)
    assert "OLD.SZ" in result["ok"]
    assert "OLD.SZ" not in result["missing"]
    assert "OLD.SZ" in result["weekly_fetch"]
    wf = result["weekly_fetch"]["OLD.SZ"]
    assert wf["week_end_bars"] == 50
    assert wf["weekly_required"] == WEEKLY_WARMUP_WEEKS
    assert wf["available_week_ends"] >= WEEKLY_WARMUP_WEEKS

    con.close()
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_etl/test_orchestrator.py::test_check_data_completeness_ipo_exempt_from_weekly_gate tests/test_etl/test_orchestrator.py::test_check_data_completeness_mature_weekly_fetch_bucket -v`

- [ ] **Step 3: Rewrite `check_data_completeness`**

替换 `backend/etl/orchestrator.py` 中 `check_data_completeness` 函数（含 docstring）：

```python
def check_data_completeness(con, ts_codes: list[str],
                             calc_date: str = None,
                             min_daily_rows: int = WARMUP_TDAYS,
                             min_week_end_bars: int = WEEKLY_WARMUP_WEEKS) -> dict:
    """检查 DWD 完整度，拆分 calc 准入与 weekly fetch 需求。

    返回:
        {
            "ok": [...],              # daily_ok → 可进入 calc
            "missing": {...},         # NOT daily_ok → 不可 calc，可 auto-fetch
            "weekly_fetch": {...},    # daily_ok 但成熟股 week-end 仍不足 → 仅 fetch
        }
    missing / weekly_fetch 条目均含:
        dwd_rows, week_end_bars, weekly_required, available_week_ends,
        min_date, max_date, reason
    """
    ok = []
    missing = {}
    weekly_fetch = {}

    if not ts_codes:
        return {"ok": ok, "missing": missing, "weekly_fetch": weekly_fetch}

    if calc_date is None:
        row = con.execute(
            "SELECT MAX(trade_date) FROM dim_date WHERE is_trade_day = 1"
        ).fetchone()
        calc_date = row[0] if row and row[0] else datetime.now().strftime("%Y%m%d")

    placeholders = ",".join(["?" for _ in ts_codes])
    rows = con.execute(f"""
        SELECT ts_code, COUNT(*), MIN(trade_date), MAX(trade_date)
        FROM dwd_daily_quote WHERE ts_code IN ({placeholders})
        GROUP BY ts_code
    """, ts_codes).fetchall()

    dwd_data = {r[0]: {"dwd_rows": r[1], "min_date": r[2], "max_date": r[3]}
                for r in rows}

    week_end_counts = _count_week_end_bars_batch(con, ts_codes)
    available_counts = _available_week_ends_batch(con, ts_codes, calc_date)

    for ts_code in ts_codes:
        info = dwd_data.get(ts_code)
        dwd_rows = info["dwd_rows"] if info else 0
        week_end_bars = week_end_counts.get(ts_code, 0)
        available_we = available_counts.get(ts_code, 0)
        weekly_required = min(min_week_end_bars, available_we)
        daily_ok = info is not None and dwd_rows >= min_daily_rows
        weekly_ok = week_end_bars >= weekly_required

        base = {
            "dwd_rows": dwd_rows,
            "week_end_bars": week_end_bars,
            "weekly_required": weekly_required,
            "available_week_ends": available_we,
            "min_date": info["min_date"] if info else None,
            "max_date": info["max_date"] if info else None,
        }

        if daily_ok:
            ok.append(ts_code)
            if not weekly_ok and available_we >= min_week_end_bars:
                weekly_fetch[ts_code] = {**base, "reason": "weekly_warmup"}
        else:
            if not weekly_ok:
                reason = "both"
            else:
                reason = "daily_warmup"
            missing[ts_code] = {**base, "reason": reason}

    return {"ok": ok, "missing": missing, "weekly_fetch": weekly_fetch}
```

- [ ] **Step 4: Update existing tests**

**`test_check_data_completeness_batch_results`** — 补 `dim_stock` + `calc_date`，A/B/C 断言不变（A 已在 ok）。

**`test_check_data_completeness_requires_week_end_bars`** — 重命名并改断言：

```python
def test_check_data_completeness_mature_low_week_ends_goes_to_weekly_fetch():
    ...
    result = check_data_completeness(con, ["W.SZ"], calc_date="20261231", min_daily_rows=250)
    assert "W.SZ" in result["ok"]
    assert "W.SZ" not in result["missing"]
    assert "W.SZ" in result["weekly_fetch"]
    assert result["weekly_fetch"]["W.SZ"]["week_end_bars"] == 50
```

为 W.SZ 的 `dim_date` 补 ≥120 个 week-end（`list_date=20200101`），使 `available_week_ends >= 120`。

- [ ] **Step 5: Run completeness tests**

Run: `pytest tests/test_etl/test_orchestrator.py -v -k completeness`

Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "fix: split calc ok from weekly_fetch in check_data_completeness"
```

---

### Task 3: `run_calc` fetch / rebuild / skip_log

**Files:**
- Modify: `backend/etl/orchestrator.py:661-805`
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: Write failing test — 无 fetch 时不 rebuild**

```python
def test_run_calc_skips_rebuild_when_only_weekly_fetch_and_ods_full(monkeypatch):
    """weekly_fetch + ODS 已满 + to_fetch 空 → 不得 rebuild_all_dwd。"""
    import duckdb
    from backend.etl import orchestrator as orch

    con = duckdb.connect(":memory:")
    rebuild_calls = []

    def fake_rebuild(con, ts_codes=None):
        rebuild_calls.append(list(ts_codes) if ts_codes else None)
        return {"daily_quote": 0, "weekly_quote": 0, "moneyflow": 0}

    monkeypatch.setattr(orch, "rebuild_all_dwd", fake_rebuild)
    monkeypatch.setattr(orch, "_filter_delisted", lambda c, codes, d: (codes, {}))
    monkeypatch.setattr(orch, "check_data_completeness", lambda c, codes, **kw: {
        "ok": ["OLD.SZ"],
        "missing": {},
        "weekly_fetch": {"OLD.SZ": {"reason": "weekly_warmup", "week_end_bars": 50}},
    })
    monkeypatch.setattr(orch, "_compute_fetch_range", lambda *a, **k: (None, None))
    monkeypatch.setattr(orch, "find_stale_ods_codes", lambda *a, **k: [])
    monkeypatch.setattr(orch, "_calc_stock_chunk", lambda *a, **k: 0)
    monkeypatch.setattr(orch, "log_etl_start", lambda *a: ("lid", 0))
    monkeypatch.setattr(orch, "log_etl_end", lambda *a, **k: None)
    monkeypatch.setattr(orch, "run_checkpoint", lambda *a: None)

    orch.run_calc(con, ["OLD.SZ"], calc_date="20260605", auto_fetch=True)

    assert rebuild_calls == []
    con.close()
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `pytest tests/test_etl/test_orchestrator.py::test_run_calc_skips_rebuild_when_only_weekly_fetch_and_ods_full -v`

Expected: FAIL — `rebuild_calls` 非空

- [ ] **Step 3: Implement `run_calc` changes**

**3a.** 所有 `check_data_completeness(con, ts_codes)` 改为 `check_data_completeness(con, ts_codes, calc_date=calc_date)`。

**3b.** 日志拆分：

```python
logger.info(
    "DWD completeness: calc_ok=%d/%d, missing=%d, weekly_fetch=%d",
    len(completeness["ok"]), len(ts_codes),
    len(completeness["missing"]), len(completeness.get("weekly_fetch", {})),
)
```

**3c.** auto-fetch 目标 = `missing` keys + `weekly_fetch` keys：

```python
fetch_candidates = list(completeness["missing"].keys()) + list(
    completeness.get("weekly_fetch", {}).keys()
)
to_fetch = []
for ts_code in fetch_candidates:
    needed_start, needed_end = _compute_fetch_range(con, ts_code, calc_date)
    if needed_start is None:
        continue
    to_fetch.append((ts_code, needed_start, needed_end))
```

**3d.** rebuild 仅在有增量时：

```python
if n_fetched > 0:
    fetched_codes = list({c for c, _, _ in to_fetch})
    rebuild_all_dwd(con, fetched_codes)
    completeness = check_data_completeness(con, ts_codes, calc_date=calc_date)
else:
    # 删除原 L760-763 else 分支的 rebuild_all_dwd(missing_codes)
    logger.info("No ODS rows fetched; skipping DWD rebuild")
```

**3e.** skip_log 仅写 `missing`（不含 `weekly_fetch`）：

```python
if completeness["missing"]:
    classified = _classify_still_missing(con, completeness["missing"])
    _write_skip_log_batch(con, calc_date, "dwd", "both", classified)
```

**3f.** `codes_to_calc = completeness["ok"]` 保持不变。

- [ ] **Step 4: Run test — expect PASS**

Run: `pytest tests/test_etl/test_orchestrator.py::test_run_calc_skips_rebuild_when_only_weekly_fetch_and_ods_full -v`

- [ ] **Step 5: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "fix: run_calc fetch weekly_fetch only, skip noop rebuild"
```

---

### Task 4: `_classify_still_missing` 根因 detail

**Files:**
- Modify: `backend/etl/orchestrator.py:538-557`
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: Write failing test**

```python
def test_classify_still_missing_includes_week_end_bars_in_detail():
    from backend.etl.orchestrator import _classify_still_missing
    from backend.etl.base import SkipReason

    missing = {
        "X.SZ": {
            "dwd_rows": 100,
            "week_end_bars": 50,
            "weekly_required": 120,
            "available_week_ends": 130,
            "min_date": "20250101",
            "max_date": "20260601",
            "reason": "daily_warmup",
        }
    }
    classified = _classify_still_missing(None, missing)
    detail = classified[SkipReason.INSUFFICIENT_ROWS][0][1]
    assert "week_end_bars=50" in detail
    assert "weekly_required=120" in detail
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_etl/test_orchestrator.py::test_classify_still_missing_includes_week_end_bars_in_detail -v`

- [ ] **Step 3: Implement**

```python
        else:
            reason = SkipReason.INSUFFICIENT_ROWS
            detail = (
                f"DWD rows={dwd_rows}, week_end_bars={info.get('week_end_bars', '?')}"
                f"/{info.get('weekly_required', '?')}"
                f" (min_date={info['min_date']}, max_date={info['max_date']},"
                f" reason={info.get('reason', '?')})"
            )
```

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "fix: richer skip_log detail for warmup classification"
```

---

### Task 5: health_check Section I 填充率 FAIL

**Files:**
- Modify: `scripts/health_check.py:23-48, 159-167`
- Test: `tests/test_health_check.py`（新建）

- [ ] **Step 1: Write failing test**

创建 `tests/test_health_check.py`：

```python
import duckdb
from scripts.health_check import Checker


def test_checker_expect_min():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (v INTEGER)")
    con.execute("INSERT INTO t VALUES (5)")
    c = Checker(con)
    c.expect_min("five", "SELECT v FROM t", minimum=3)
    assert c.failures == 0
    c.expect_min("too low", "SELECT v FROM t", minimum=10)
    assert c.failures == 1
    con.close()
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_health_check.py::test_checker_expect_min -v`

- [ ] **Step 3: Add `expect_min` and Section I threshold**

在 `Checker` 类中 `info` 之后：

```python
    def expect_min(self, label, sql, minimum: int):
        try:
            v = self._scalar(sql) or 0
        except Exception as e:
            print(f"  [ERR ] {label}: {e}")
            self.failures += 1
            return
        ok = v >= minimum
        if not ok:
            self.failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {v:,} (min {minimum:,})")
```

替换 Section I（`scripts/health_check.py`）：

```python
    print("=== I. 周线 volume 状态指标（成熟股） ===")
    mature_vol_sql = """
        SELECT COUNT(*) FROM v_dws_volume_weekly_latest v
        JOIN dim_date d ON v.trade_date = d.trade_date AND d.is_week_end = 1
        JOIN dim_stock s ON v.ts_code = s.ts_code
        WHERE v.pct_vol_rank IS NOT NULL
          AND s.list_date IS NOT NULL
          AND s.list_date <= (
              SELECT trade_date FROM (
                  SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
                  FROM dim_date WHERE is_trade_day=1 AND is_week_end=1
              ) WHERE rn = 120
          )
    """
    mature_universe_sql = """
        SELECT COUNT(*) FROM dim_stock s
        WHERE s.list_date IS NOT NULL
          AND s.list_date <= (
              SELECT trade_date FROM (
                  SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
                  FROM dim_date WHERE is_trade_day=1 AND is_week_end=1
              ) WHERE rn = 120
          )
    """
    c.info("成熟股 universe", mature_universe_sql)
    c.expect_min("volume_weekly pct_vol_rank 非空(成熟股)", mature_vol_sql, minimum=4000)
    c.expect_min(
        "volume_weekly zone 非空(成熟股)",
        mature_vol_sql.replace("pct_vol_rank", "zone"),
        minimum=4000,
    )
```

> **阈值说明：** `4000` ≈ 活跃非 ST 股的 80% 下限（全市场 ~5000）。实库首次补历史后应 >>4000；若 FAIL 说明 P0 回归。可按实库 `health_check` info 输出微调。

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_health_check.py -v`

- [ ] **Step 5: Commit**

```bash
git add scripts/health_check.py tests/test_health_check.py
git commit -m "feat: health_check fail threshold for mature weekly volume fill rate"
```

---

### Task 6: sideways Excel 高亮

**Files:**
- Modify: `backend/export_wide.py:465-466`
- Test: 无单测（可选手动）；或断言 `_write_sheet_merged` 字典含 key

- [ ] **Step 1: Add highlight mapping**

```python
        "均线形态": {"多头强势": green, "多头初建": green, "多头衰竭": blue, "多头翻转": blue,
                     "空头强势": red, "空头初建": red, "空头衰竭": blue, "空头翻转": blue,
                     "均线缠绕": blue, "均线走平": blue},
```

- [ ] **Step 2: Commit**

```bash
git add backend/export_wide.py
git commit -m "fix: highlight sideways ma_alignment in Excel export"
```

---

### Task 7: 文档更新

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`

- [ ] **Step 1: CLAUDE.md — 替换双轨 warmup 段落**

```markdown
- **双轨 warmup（拆分门禁）：**
  - **calc 准入：** `dwd_rows ≥ 250`（`check_data_completeness.ok`）
  - **fetch 门禁：** 成熟股（available week-end ≥ 120）且 `week_end_bars < 120` → `weekly_fetch` 桶，触发 auto-fetch；**上市不足 120 周除外**（`weekly_required = min(120, available)`）
  - fetch 起点：`min(250td_start, 120 week-end_start, list_date)`
- **导出语义：** `-`=当日无事件信号；`N/A`=不可算或源端无数据
```

- [ ] **Step 2: spec §6.5 追加**

> calc 与 fetch 门禁分离：新股 daily_ok 即可 calc（周线 volume 窗口不足导出 N/A）；仅 mature 股 history 不足时进入 `weekly_fetch` 补拉。

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md
git commit -m "docs: split calc vs fetch warmup gate semantics"
```

---

### Task 8: 全量测试 + 实库验收

- [ ] **Step 1: Run full suite**

Run: `pytest tests/ -v`

Expected: ALL PASS

- [ ] **Step 2: 实库验收（运维窗口）**

```bash
python -m backend.cli fetch
python -m backend.cli calc --date 20260605
python -m scripts.health_check
python -m backend.cli export --date 20260605
```

验收点：

| 检查 | 期望 |
|------|------|
| calc_ok 数量 | ≈ 全活跃股（含新股），不再因 weekly  alone 少 5–12% |
| Section I pct_vol_rank | ≥4000（成熟股） |
| 新股 export | 日线指标有值；周线量能 `N/A` |
| mature 股 export | 周线 pct_vol_rank / vol_zone 非 N/A |

- [ ] **Step 3: SQL 抽检**

```sql
-- 成熟股最新 week-end 周线 volume 填充
SELECT COUNT(*) FILTER (WHERE pct_vol_rank IS NOT NULL),
       COUNT(*) FILTER (WHERE zone IS NOT NULL)
FROM v_dws_volume_weekly_latest v
JOIN dim_date d ON v.trade_date = d.trade_date AND d.is_week_end = 1
WHERE v.trade_date = (SELECT MAX(trade_date) FROM dim_date WHERE is_week_end = 1);
```

---

## Self-Review

| 检查项 | 结果 |
|--------|------|
| 设计 doc「上市不足 120 周除外」 | Task 2 `weekly_required = min(120, available)` |
| 不破坏日线路径 | Task 2 `ok = daily_ok` |
| mature 股 P0 fetch 补历史 | Task 2 `weekly_fetch` + Task 3 fetch_candidates |
| 无效 rebuild 消除 | Task 3 `n_fetched > 0` 才 rebuild |
| skip_log 根因 | Task 4 |
| health_check 闭环 | Task 5 |
| 无 placeholder / TBD | PASS |
| calc_volume 不改 | PASS |

## 风险

| 风险 | 缓解 |
|------|------|
| `calc_date` 新参数遗漏调用点 | grep `check_data_completeness` 全项目，run_calc 必传 |
| health_check 4000 阈值过严 | 首次补历史前可能 FAIL；文档注明首次 fetch 后重跑 |
| `dim_stock` 缺 list_date | `available_we=0` → weekly_required=0 → weekly_ok；需确保 build_dim 有 list_date |

---

## GSTACK REVIEW REPORT

Quality score: 9/10 — 可执行 TDD 步骤齐全；Batch A（Task 1–4）为必做，Batch B（Task 5–6）建议同 PR。
