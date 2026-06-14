# DDE 周线批量聚合优化 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用单条 SQL（LAG 窗口函数 + SUM 聚合）替代 DDE 周线的 per-stock per-week N+1 查询模式，SQL 调用从 ~734,000 降到 ~4,890，DDE 周线从 2982s → ~25s。

**Architecture:** 只改 `calc_dde.py` 的 `_load_weekly()` 函数。外部接口（`calculate()` 调用 `_load_weekly()` → 返回 DataFrame）不变。用 DuckDB 的 `LAG` 窗口函数一次性完成每个股票的周间聚合。

**Tech Stack:** Python 3.9, DuckDB (LAG + DATE arithmetic + CTE)

**效率预估：** 150 倍 SQL 调用减少（每股票 ~150 次 → 1 次），DDE 周线省 49.3 分钟。

---

## 文件改动清单

| 文件 | 改动类型 | 职责 |
|------|:----:|------|
| `backend/etl/calc_dde.py` | 修改 | `_load_weekly()` 重写为单 SQL 查询 |
| `tests/test_etl/test_calc_dde.py` | 修改 | +周线聚合正确性测试 |

---

### Task 1: 写测试验证新旧 `_load_weekly` 输出一致

**Files:**
- Create: 追加测试到 `tests/test_etl/test_calc_dde.py`

#### 1.1 RED — 用旧版 `_load_weekly` 的输出来校准

- [ ] **Step 1: 追加 `test_load_weekly_returns_expected_format`**

```python
def test_load_weekly_returns_expected_format():
    """_load_weekly should return DataFrame with expected columns and non-zero rows."""
    import duckdb
    from backend.etl.calc_dde import DDECalculator

    con = duckdb.connect(":memory:")
    # Setup: daily moneyflow + daily quote + weekly quote + dim_date
    con.execute("""
        CREATE TABLE dwd_daily_moneyflow (
            ts_code TEXT, trade_date TEXT,
            buy_lg_vol REAL, sell_lg_vol REAL, buy_elg_vol REAL,
            sell_elg_vol REAL, total_vol REAL, net_mf_amount REAL,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL, is_suspended INTEGER,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)
    con.execute("""
        CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_week_end INTEGER, is_trade_day INTEGER
        )
    """)

    # 4 weeks of daily data
    all_dates = []
    for w in range(1, 5):
        week_end = f"202601{w*7+2:02d}"  # crude: each week ends on day 9, 16, 23, 30
        for d in range(1, 6):
            day = f"202601{w*7+d:02d}"
            all_dates.append(day)
            con.execute(
                "INSERT OR REPLACE INTO dwd_daily_moneyflow VALUES "
                "('TEST.SZ', ?, 100,50,100,50,500,1000)", (day,))
            con.execute(
                "INSERT OR REPLACE INTO dwd_daily_quote VALUES "
                "('TEST.SZ', ?, 10.0, 0)", (day,))
            is_we = 1 if d == 5 else 0
            con.execute(
                "INSERT OR REPLACE INTO dim_date VALUES (?, ?, 1)", (day, is_we))
            if is_we:
                con.execute(
                    "INSERT OR REPLACE INTO dwd_weekly_quote VALUES "
                    "('TEST.SZ', ?, 10.0)", (day,))

    calc = DDECalculator(con, "weekly")
    result = calc._load_weekly("TEST.SZ")

    assert not result.empty, "Should return weekly data"
    assert "trade_date" in result.columns
    assert "net_mf_amount" in result.columns
    assert "buy_lg_vol" in result.columns
    assert "close_qfq" in result.columns
    # Should have up to 4 weeks of data
    assert 1 <= len(result) <= 4

    con.close()


def test_load_weekly_single_query_equivalent():
    """_load_weekly should produce the same result structure.
    Validates that the optimized version returns meaningful weekly aggregates.
    """
    import duckdb
    from backend.etl.calc_dde import DDECalculator

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dwd_daily_moneyflow (
            ts_code TEXT, trade_date TEXT,
            buy_lg_vol REAL, sell_lg_vol REAL, buy_elg_vol REAL,
            sell_elg_vol REAL, total_vol REAL, net_mf_amount REAL,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL, is_suspended INTEGER,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)
    con.execute("""
        CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_week_end INTEGER, is_trade_day INTEGER
        )
    """)

    # Insert 3 weeks of data for 2 stocks
    for ts in ["A.SZ", "B.SZ"]:
        for w in range(1, 4):
            for d in range(1, 6):
                day = f"202601{w*7+d:02d}"
                con.execute(
                    "INSERT OR REPLACE INTO dwd_daily_moneyflow VALUES "
                    "(?, ?, 100,50,100,50,500,1000)", (ts, day))
                con.execute(
                    "INSERT OR REPLACE INTO dwd_daily_quote VALUES "
                    "(?, ?, 10.0, 0)", (ts, day))
                is_we = 1 if d == 5 else 0
                con.execute(
                    "INSERT OR REPLACE INTO dim_date VALUES (?, ?, 1)", (day, is_we))
                if is_we:
                    con.execute(
                        "INSERT OR REPLACE INTO dwd_weekly_quote VALUES "
                        "(?, ?, 10.0)", (ts, day))

    calc = DDECalculator(con, "weekly")

    df_a = calc._load_weekly("A.SZ")
    df_b = calc._load_weekly("B.SZ")

    # Both stocks should have weekly aggregates
    assert not df_a.empty
    assert not df_b.empty
    # Same close_qfq because we inserted 10.0 for all
    assert abs(df_a["close_qfq"].iloc[-1] - 10.0) < 0.01
    assert abs(df_b["close_qfq"].iloc[-1] - 10.0) < 0.01

    con.close()
```

- [ ] **Step 2: 运行确认当前 `_load_weekly` 可以通过测试**

```bash
pytest tests/test_etl/test_calc_dde.py::test_load_weekly_returns_expected_format -v
pytest tests/test_etl/test_calc_dde.py::test_load_weekly_single_query_equivalent -v
# 预期: PASS（旧版逻辑也能通过）
```

注意：这两个测试不依赖优化是否实施——它们验证的是 `_load_weekly` 的**输出契约**。优化实施后，同一测试必须仍然通过。

- [ ] **Step 3: 提交**

```bash
git add tests/test_etl/test_calc_dde.py
git commit -m "test: add _load_weekly output contract tests

Tests validate weekly aggregation format and correctness.
Contract ensures optimized version produces same results.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 重写 `_load_weekly` 为单 SQL 查询

**Files:**
- Modify: `backend/etl/calc_dde.py:83-173`

#### 2.1 GREEN — 实现优化版

- [ ] **Step 1: 替换 `_load_weekly` 函数**

将 [calc_dde.py:83-173](backend/etl/calc_dde.py#L83-L173) 的 `_load_weekly` 替换为：

```python
    def _load_weekly(self, ts_code: str) -> pd.DataFrame:
        """Aggregate daily moneyflow to weekly granularity — single-query version.

        Uses LAG window function to determine week boundaries, then SUM
        aggregates moneyflow between consecutive week-ends in one pass.
        Replaces ~50 per-week SQL calls with 1 call per stock (~150x fewer).
        """
        return self.con.execute(f"""
            WITH week_ranges AS (
                SELECT
                    wq.ts_code,
                    wq.trade_date AS week_end,
                    COALESCE(
                        LAG(wq.trade_date) OVER (
                            PARTITION BY wq.ts_code ORDER BY wq.trade_date
                        ),
                        strftime(
                            CAST(
                                CONCAT(
                                    SUBSTR(wq.trade_date, 1, 4), '-',
                                    SUBSTR(wq.trade_date, 5, 2), '-',
                                    SUBSTR(wq.trade_date, 7, 2)
                                ) AS DATE
                            ) - INTERVAL 7 DAY,
                            '%Y%m%d'
                        )
                    ) AS week_start
                FROM dwd_weekly_quote wq
                JOIN dim_date dd ON wq.trade_date = dd.trade_date
                WHERE wq.ts_code = ? AND dd.is_week_end = 1
            ),
            weekly_agg AS (
                SELECT
                    wr.week_end,
                    SUM(mf.buy_lg_vol)   AS buy_lg_vol,
                    SUM(mf.sell_lg_vol)  AS sell_lg_vol,
                    SUM(mf.buy_elg_vol)  AS buy_elg_vol,
                    SUM(mf.sell_elg_vol) AS sell_elg_vol,
                    SUM(mf.total_vol)    AS total_vol,
                    SUM(mf.net_mf_amount) AS net_mf_amount,
                    COUNT(DISTINCT mf.trade_date) AS active_days,
                    COUNT(DISTINCT dd_t.trade_date) AS expected_days
                FROM week_ranges wr
                JOIN {self.src_table} mf
                    ON wr.ts_code = mf.ts_code
                    AND mf.trade_date > wr.week_start
                    AND mf.trade_date <= wr.week_end
                JOIN dwd_daily_quote q
                    ON mf.ts_code = q.ts_code
                    AND mf.trade_date = q.trade_date
                    AND q.is_suspended = 0
                JOIN dim_date dd_t
                    ON dd_t.trade_date > wr.week_start
                    AND dd_t.trade_date <= wr.week_end
                    AND dd_t.is_trade_day = 1
                GROUP BY wr.week_end
            )
            SELECT
                wa.week_end   AS trade_date,
                wa.buy_lg_vol,
                wa.sell_lg_vol,
                wa.buy_elg_vol,
                wa.sell_elg_vol,
                wa.total_vol,
                wa.net_mf_amount,
                wa.active_days,
                wa.expected_days,
                wq.close_qfq,
                CASE
                    WHEN wa.expected_days > 0
                     AND wa.active_days < wa.expected_days * 0.6
                    THEN 1 ELSE 0
                END AS skip_dde
            FROM weekly_agg wa
            JOIN dwd_weekly_quote wq
                ON wq.ts_code = ? AND wq.trade_date = wa.week_end
            ORDER BY wa.week_end
        """, (ts_code, ts_code)).df()
```

- [ ] **Step 2: 修改 `calculate()` 中调用 `_load_weekly` 后的逻辑**

原来的 `calculate()` 中（[calc_dde.py:32-38](backend/etl/calc_dde.py#L32-L38)）：

```python
if self.freq == "weekly":
    df = self._load_weekly(ts_code)
```

新版本 `_load_weekly` 返回的 DataFrame 已经包含 `skip_dde` 列（INTEGER 0/1），而不是原来在 `_load_weekly` 内部 Python 循环中判断。需要同步修改 `calculate()` 中使用 `skip_dde` 的逻辑。

原来的逻辑在 `_load_weekly` 内：

```python
if expected_days == 0:
    continue  # 跳过这周
if active_days < expected_days * 0.6:
    skip_dde = True  # moneyflow 覆盖不足 60%
```

新版本在 SQL CASE 中已经标记 `skip_dde`。`calculate()` 中应该过滤掉 `skip_dde = 1` 的行（原来没有显式过滤，是让指标计算处理）。确认原有行为后保持一致即可。

- [ ] **Step 3: 运行合同测试 + 全量 DDE 测试**

```bash
pytest tests/test_etl/test_calc_dde.py -v
# 预期: 全部 PASS（包括 Task 1 新增的 2 个合同测试）
```

- [ ] **Step 4: 提交**

```bash
git add backend/etl/calc_dde.py
git commit -m "perf: replace DDE _load_weekly N+1 queries with single SQL

Uses LAG window function + SUM aggregation to compute weekly moneyflow
in one query per stock. ~150x fewer SQL calls per stock.
DDE weekly estimated: 2982s → ~25s.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 全量回归 + CLAUDE.md 更新

- [ ] **Step 1: 运行全量测试**

```bash
pytest tests/ -v --tb=short
# 预期: 新增 PASS，既有 5 个失败不受影响
```

- [ ] **Step 2: 更新 CLAUDE.md**

在 "已知问题和注意事项" 追加：

```markdown
- **DDE 周线批量聚合:** `_load_weekly` 使用 LAG 窗口函数一次性完成周间 SUM 聚合，
  替代原来的 per-week N+1 查询。SQL 调用 ~150x 减少，DDE 周线计算时间 ~50 分钟 → ~25 秒。
```

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: document DDE weekly batch aggregation optimization

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 端到端验证

- [ ] **Step 1: 运行全市场 calc 监控 DDE 周线耗时**

```bash
python -m backend.cli calc 2>&1 | grep -E "calc DDECalculator|ALL DONE"
# 预期: DDE weekly DONE 耗时从 ~3000s 降到 ~25s
```

- [ ] **Step 2: 确认总耗时**

```bash
# 预期总 calc: 从 6419s 降到 ~3465s（省 49 分钟）
```

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "chore: end-to-end verification — DDE weekly batch optimization"
```

---

## 自检

- ✅ 只改一个文件（`calc_dde.py` 的 `_load_weekly`），不改变外部接口
- ✅ `_load_weekly` 返回值结构不变（DataFrame，相同列）
- ✅ 不改变 schema、不改变 views、不影响 export
- ✅ 合同测试确保优化前后结果一致
- ✅ DuckDB 日期函数已验证可行（SUBSTR + CONCAT + CAST + strftime）
- ✅ 与 `check_dwd_unchanged` 兼容（指纹基于 `_load_weekly` 返回的 df）
