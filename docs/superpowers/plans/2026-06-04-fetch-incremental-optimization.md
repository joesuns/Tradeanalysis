# 数据获取与增量检测优化 — 实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 date-batched 模式整日粒度增量检测 bug（P0）+ 增加数据质量门禁（P1）。全市场首次 fetch 从 ~4.5 小时降到 ~2 分钟，数据完整率从 10.5% 提升到 100%。

**Architecture:** 借鉴 123 项目的逐股票增量检测策略：(1) `_get_trading_days` 增加 `ts_codes` 参数实现 per-target-stock 增量跳过；(2) `_compute_fetch_range` 覆盖率 95%→100%；(3) 新增 `_validate_ods_batch` 数据质量门禁。

**Tech Stack:** Python 3.9, DuckDB, tushare, pytest

**效率预估:** 全市场首次拉取 ~4.5h→~2min (135x)，成功率 10.5%→100%

---

## 文件改动清单

| 文件 | 改动类型 | 职责 |
|------|:----:|------|
| `backend/fetch/ods_daily.py` | 修改 | `_get_trading_days` + ts_codes；新增 `_validate_ods_batch`；调用处传参 |
| `backend/etl/orchestrator.py` | 修改 | `_compute_fetch_range` 0.95→1.0 |
| `tests/test_fetch/test_ods_daily.py` | 修改 | 增量检测 + 质量校验测试 |
| `tests/test_etl/test_orchestrator.py` | 修改 | 100% 覆盖率阈值测试 |

---

### Task 1: `_get_trading_days` 增加 per-target-stock 增量检测（P0）

**Files:**
- Modify: `backend/fetch/ods_daily.py:98-121,304`
- Modify: `tests/test_fetch/test_ods_daily.py`

#### 1.1 RED — 写测试

- [ ] **Step 1: 在 `tests/test_fetch/test_ods_daily.py` 末尾追加测试**

```python
def test_get_trading_days_with_ts_codes_partial_coverage():
    """Date skipped ONLY when ALL target stocks have data (not just any stock)."""
    import duckdb
    from backend.fetch.ods_daily import _get_trading_days

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT,
            open REAL, high REAL, low REAL, close REAL, vol REAL,
            amount REAL, pct_chg REAL, adj_factor REAL)
    """)
    # Stock A: all 3 dates. Stock B: only 20260101.
    for d in ["20260101", "20260102", "20260103"]:
        con.execute("INSERT INTO ods_daily VALUES ('A.SZ', ?, 10,11,9,10,100,1000,0,1)", (d,))
    con.execute("INSERT INTO ods_daily VALUES ('B.SZ', '20260101', 10,11,9,10,100,1000,0,1)")

    class FakeClient:
        def call(self, api, **kwargs):
            return [{"cal_date": d} for d in ["20260101", "20260102", "20260103"]]

    # Without ts_codes: date-global — ANY stock has data → date skipped → 0 new
    days_all = _get_trading_days(FakeClient(), "20260101", "20260103", con=con)
    assert len(days_all) == 0

    # With ts_codes=["B.SZ"]: ONLY B has all 3 dates? No — B missing 01/02, 01/03
    # So no date has ALL target stocks → all 3 dates returned
    days_b = _get_trading_days(FakeClient(), "20260101", "20260103",
                               con=con, ts_codes=["B.SZ"])
    assert len(days_b) == 3, f"B missing 2 dates, but A has them — per-stock should return all"

    # With ts_codes=["A.SZ"]: A has all 3 dates → all skipped
    days_a = _get_trading_days(FakeClient(), "20260101", "20260103",
                               con=con, ts_codes=["A.SZ"])
    assert len(days_a) == 0

    # With ts_codes=["A.SZ", "B.SZ"]: B missing → no date fully covered
    days_both = _get_trading_days(FakeClient(), "20260101", "20260103",
                                  con=con, ts_codes=["A.SZ", "B.SZ"])
    assert len(days_both) == 3

    con.close()
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_fetch/test_ods_daily.py::test_get_trading_days_with_ts_codes_partial_coverage -v
# 预期: TypeError — ts_codes 参数不存在
```

#### 1.2 GREEN — 实现

- [ ] **Step 3: 修改 `_get_trading_days`** — [ods_daily.py:98-121](backend/fetch/ods_daily.py#L98-L121)

将函数签名和逻辑改为：

```python
def _get_trading_days(client, start: str, end: str,
                      con=None, ts_codes: list[str] = None) -> list[str]:
    """Get list of trading days in date range from tushare trade_cal.

    When con + ts_codes: per-target-stock incremental — only skip dates
    where ALL target stocks already have ODS data.
    When con only: original date-global incremental.
    """
    recs = client.call("trade_cal", exchange="SSE", start_date=start, end_date=end,
                       is_open=1)
    days = sorted([r["cal_date"] for r in recs])

    if con and ts_codes:
        placeholders = ",".join(["?" for _ in ts_codes])
        rows = con.execute(f"""
            SELECT trade_date, COUNT(DISTINCT ts_code) AS n
            FROM ods_daily
            WHERE ts_code IN ({placeholders})
            AND trade_date >= ? AND trade_date <= ?
            GROUP BY trade_date
        """, (*ts_codes, start, end)).fetchall()
        n_targets = len(ts_codes)
        existing = {r[0] for r in rows if r[1] >= n_targets}
        if existing:
            new_days = [d for d in days if d not in existing]
            logger.info("Incremental (per-stock): %d/%d dates fully covered, "
                        "%d new to fetch (%s~%s)",
                        len(existing), len(days), len(new_days), start, end)
            return new_days
        return days

    if con:
        existing = set(r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM ods_daily "
            "WHERE trade_date >= ? AND trade_date <= ?",
            (start, end)
        ).fetchall())
        if existing:
            new_days = [d for d in days if d not in existing]
            logger.info("Incremental: %d/%d dates already in DB, %d new to fetch "
                        "(%s~%s)", len(existing), len(days), len(new_days), start, end)
            return new_days

    return days
```

- [ ] **Step 4: 修改 `fetch_by_date_range_parallel` 调用处** — [ods_daily.py:304](backend/fetch/ods_daily.py#L304)

```python
# 改前：
days = _get_trading_days(client, start, end, con=con)
# 改后：
days = _get_trading_days(client, start, end, con=con, ts_codes=ts_codes)
```

- [ ] **Step 5: 运行测试确认通过**

```bash
pytest tests/test_fetch/test_ods_daily.py -v
# 预期: 全部 PASS
```

- [ ] **Step 6: 提交**

```bash
git add backend/fetch/ods_daily.py tests/test_fetch/test_ods_daily.py
git commit -m "feat: per-target-stock incremental detection in _get_trading_days

When ts_codes is provided to _get_trading_days, only skip dates where
ALL target stocks already have ODS data. Fixes date-batched mode incorrectly
skipping dates due to partial data from other stocks (e.g. 659 pre-fetched
stocks causing 4731 others to be missed).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `_compute_fetch_range` 覆盖率 95%→100%（P0）

**Files:**
- Modify: `backend/etl/orchestrator.py:294`
- Modify: `tests/test_etl/test_orchestrator.py`

**依据：** 123 项目的 `max_cache_date >= end_date and min_cache_date <= start_date` 是 100% 覆盖才跳过。Tradeanalysis 的 95% 允许 250 天中缺失 12 天仍跳过，过于宽松，应严格化。

#### 2.1 RED — 写测试

- [ ] **Step 1: 在 `tests/test_etl/test_orchestrator.py` 末尾追加测试**

```python
def test_compute_fetch_range_requires_full_coverage():
    """When ODS has data but < 100% coverage, _compute_fetch_range should NOT
    return (None, None). It should return an actual fetch range."""
    import duckdb
    from backend.etl.orchestrator import _compute_fetch_range, _count_trading_days

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_stock (ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    con.execute("INSERT INTO dim_stock VALUES ('TEST.SZ', '20250101', NULL)")
    con.execute("""
        CREATE TABLE dim_date (trade_date TEXT PRIMARY KEY, is_trade_day INTEGER)
    """)
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    con.execute("CREATE TABLE dwd_daily_quote (ts_code TEXT, trade_date TEXT)")

    # 100 trading days
    days = [f"2026{i:04d}"[:8] for i in range(10001, 10101)]  # 100 fake YYYYMMDD
    for d in days:
        con.execute("INSERT INTO dim_date VALUES (?, 1)", (d,))

    # 96% ODS coverage — was >= 95%, but < 100%
    for d in days[:96]:
        con.execute("INSERT INTO ods_daily VALUES ('TEST.SZ', ?)", (d,))

    start, end = _compute_fetch_range(con, "TEST.SZ", "20260604", lookback_tdays=50)
    # With 96% coverage and old 95% threshold: returns (None, None).
    # With 100% threshold: returns actual range since 96% < 100%.
    assert start is not None, "96% coverage should NOT skip — requires 100%"
    assert end is not None

    con.close()
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_etl/test_orchestrator.py::test_compute_fetch_range_requires_full_coverage -v
# 预期: FAIL — (None, None) returned but start is not None expected
```

#### 2.2 GREEN — 实现

- [ ] **Step 3: 修改阈值** — [orchestrator.py:294](backend/etl/orchestrator.py#L294)

```python
# 改前：
if actual > 0 and actual >= expected * 0.95:
# 改后：
if actual > 0 and actual >= expected:  # 100% coverage — same as 123's strict check
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_etl/test_orchestrator.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "fix: require 100% ODS coverage before skipping fetch

Change _compute_fetch_range threshold from 95% to 100%.
Aligned with 123 project's strict cache-completeness check:
max(trade_date) >= end_date AND min(trade_date) <= start_date.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 数据质量门禁（P1）

**Files:**
- Modify: `backend/fetch/ods_daily.py`
- Modify: `tests/test_fetch/test_ods_daily.py`

**依据：** 123 项目 `check_data_quality()` 在缓存命中后校验必需字段/NaN比例/数据量。Tradeanalysis 当前零校验，脏数据直接流入下游。

- [ ] **Step 1: 在 `ods_daily.py` 新增 `_validate_ods_batch`**

在 `_get_trading_days` 之后插入：

```python
def _validate_ods_batch(recs: list[dict], api_name: str,
                        trade_date: str = None) -> tuple[int, int]:
    """Validate ODS batch before INSERT. Returns (valid_count, invalid_count).

    Checks:
      1. Required fields present (open/high/low/close/vol/amount)
      2. NaN ratio < 50% on price fields
      3. OHLC logic: high >= low, high >= open, high >= close, low <= open, low <= close

    Returns counts; invalid rows are logged and excluded.
    """
    required = ["open", "high", "low", "close", "vol", "amount"]
    valid = 0
    invalid = 0

    for r in recs:
        # Check 1: required fields present and non-null
        missing = [f for f in required if r.get(f) is None]
        if missing:
            logger.debug("_validate_ods_batch: %s %s missing fields: %s",
                         api_name, r.get("ts_code", "?"), missing)
            invalid += 1
            continue

        # Check 2: OHLC sanity
        try:
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        except (ValueError, TypeError):
            invalid += 1
            continue

        if h < l or h < o or h < c or l > o or l > c:
            logger.debug("_validate_ods_batch: %s %s OHLC logic failed "
                         "O=%s H=%s L=%s C=%s",
                         api_name, r.get("ts_code", "?"), o, h, l, c)
            invalid += 1
            continue

        # Check 3: NaN ratio on price fields
        nan_count = sum(1 for f in ["open", "high", "low", "close"]
                        if pd.isna(r.get(f)))
        if nan_count >= 2:  # ≥50% NaN
            invalid += 1
            continue

        valid += 1

    if invalid > 0:
        logger.warning("_validate_ods_batch: %s %s %d/%d rows rejected",
                       api_name, trade_date or "", invalid, len(recs))

    return valid, invalid
```

- [ ] **Step 2: 在 `fetch_by_date_range_parallel` 和 `fetch_stocks_incremental` 的 INSERT 前调用**

在 `fetch_by_date_range_parallel` 的 `_fetch_chunk` 内（[ods_daily.py:332-333](backend/fetch/ods_daily.py#L332-L333)），daily INSERT 前加：

```python
recs = thread_client.call("daily", trade_date=trade_date)
valid_count, _ = _validate_ods_batch(recs, "daily", trade_date)
if valid_count == 0:
    continue
for r in recs:
    ...
```

同理在 `fetch_stocks_incremental`（[ods_daily.py:206-207](backend/fetch/ods_daily.py#L206-L207)）的 daily 拉取后加校验。

实际上，为避免在循环内逐行校验开销，把校验放在 `for r in recs:` 循环内的关键检查处即可——OHLC 取值时做 safety check。

**更轻量的实现：** 在 `fetch_stocks_incremental` 和 `_fetch_chunk` 的 daily INSERT 处增加 try/except 和异常计数，异常数超阈值时写 skip log。不增加独立校验函数，降低侵入性。

```python
# 在 INSERT 循环内：
try:
    o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
    if h < l:
        raise ValueError(f"high({h}) < low({l})")
except (ValueError, TypeError, KeyError) as e:
    bad_rows += 1
    continue
```

- [ ] **Step 3: 写测试**

```python
def test_validate_ods_batch_rejects_bad_ohlc():
    """Rows with high < low or missing close should be rejected."""
    from backend.fetch.ods_daily import _validate_ods_batch

    recs = [
        {"ts_code": "A.SZ", "trade_date": "20260101",
         "open": 10, "high": 12, "low": 9, "close": 11, "vol": 100, "amount": 1000},
        {"ts_code": "B.SZ", "trade_date": "20260101",
         "open": 10, "high": 8, "low": 9, "close": 11, "vol": 100, "amount": 1000},  # high < low
        {"ts_code": "C.SZ", "trade_date": "20260101",
         "open": 10, "high": 12, "low": 9, "close": None, "vol": 100, "amount": 1000},  # missing close
    ]
    valid, invalid = _validate_ods_batch(recs, "daily")
    assert valid == 1
    assert invalid == 2


def test_validate_ods_batch_all_valid():
    """Clean data should all pass."""
    from backend.fetch.ods_daily import _validate_ods_batch

    recs = [
        {"ts_code": "A.SZ", "trade_date": "20260101",
         "open": 10, "high": 12, "low": 9, "close": 11, "vol": 100, "amount": 1000},
        {"ts_code": "B.SZ", "trade_date": "20260101",
         "open": 20, "high": 22, "low": 19, "close": 21, "vol": 200, "amount": 2000},
    ]
    valid, invalid = _validate_ods_batch(recs, "daily")
    assert valid == 2
    assert invalid == 0
```

- [ ] **Step 4: 运行测试**

```bash
pytest tests/test_fetch/test_ods_daily.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add backend/fetch/ods_daily.py tests/test_fetch/test_ods_daily.py
git commit -m "feat: add _validate_ods_batch — data quality gate for ODS ingestion

Validates OHLC logic (high>=low, required fields non-null) before INSERT.
Rejects rows with bad data; logs warnings for monitoring.
Aligned with 123 project's check_data_quality() pattern.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 全量测试回归 + CLAUDE.md 更新

- [ ] **Step 1: 运行全量测试**

```bash
pytest tests/ -v --tb=short
# 预期: 新增测试全部 PASS，既有 5 个失败不受影响
```

- [ ] **Step 2: 更新 CLAUDE.md**

在 CLAUDE.md 的 "已知问题和注意事项" 区块末尾追加：

```
- **Date-batched 增量检测 (v2):** `_get_trading_days` 支持 `ts_codes` 参数，per-target-stock 粒度。
  只有 ALL 目标股票都已有 ODS 数据时该日才跳过，不再受其他股票残留数据污染。
- **Fetch 覆盖率:** `_compute_fetch_range` 要求 100% ODS 覆盖才跳过（之前为 95%），对齐 123 项目严格检查模式。
- **数据质量门禁:** `_validate_ods_batch` 在 ODS INSERT 前校验 OHLC 逻辑和必需字段，脏数据写 skip log。
```

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md — per-stock incremental detection + quality gate"
```

---

### Task 5: 端到端验证 — 重跑 20260601 全市场分析

- [ ] **Step 1: 查看当前数据库状态**

```bash
python -m backend.cli status
```

- [ ] **Step 2: 清理上次残留的部分 DWS 数据（可选，让 calc 重新计算）**

```bash
# 上次只有 583 只成功，这次应覆盖全市场
```

- [ ] **Step 3: 运行 calc --all**

```bash
python -m backend.cli calc --all 2>&1 | tee /tmp/calc_20260604_v2.log
```

关键日志预期：
```
Auto-fetch bucket [YYYYMMDD~20260604]: N stocks, 250 tdays → date-batched parallel mode
Incremental (per-stock): X/250 dates fully covered, Y new to fetch
# 大 bucket 不应再因残留数据被完全跳过
# 应看到 date-batched 实际拉取了数据
```

- [ ] **Step 4: 验证完整度**

```bash
python -m backend.cli status
# 预期: DWD 和 DWS 行数大幅增长，股票数接近全市场
```

- [ ] **Step 5: 导出 Excel**

```bash
python -m backend.cli export --date 20260601 --output exports/analysis_20260601.xlsx
```

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "chore: end-to-end verification — per-stock incremental + quality gate"
```

---

## 自检

- ✅ 两个 P0 改动独立可测：`_get_trading_days` ts_codes 参数 + `_compute_fetch_range` 阈值
- ✅ P1 质量门禁不改变正常流程（有效数据通过，无效数据拒绝）
- ✅ 每个 Task 有 RED→GREEN TDD 循环
- ✅ 不影响 CLI fetch 层（fetch 层已有正确的双模式分发）
- ✅ 向后兼容：无 ts_codes 时 `_get_trading_days` 行为不变
- ✅ `fetch_by_date_range_parallel` 的 `ts_codes` 参数原本已存在并用于 INSERT 过滤，现在也用于增量检测
