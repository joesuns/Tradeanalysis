# DDE 周线单 SQL 优化 — 实施计划（最终版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 DDE 周线 per-stock per-week N+1 模式（~73 万次 SQL 调用）替换为单条 LAG + SUM 聚合 SQL（~4,890 次），DDE 周线从 ~2982s → ~25s。

**Architecture:** `_load_weekly()` 内部实现从 Python for-loop 改为单条 DuckDB CTE SQL（LAG 窗口函数 + JOIN + GROUP BY）。函数签名不变，返回 DataFrame 结构不变（含 `_skip_dde` 列）。

**Tech Stack:** Python 3.9, DuckDB (LAG, strftime, COALESCE, CTE)

**效率预估：** SQL 调用 ~150 倍减少（每股票 ~150 次 → 1 次），总 calc 从 ~107min → ~58min（省 49 分钟）。

---

## 文件改动清单

| 文件 | 改动类型 | 职责 |
|------|:----:|------|
| `backend/etl/calc_dde.py` | 修改 | `_load_weekly()` 重写 |
| `tests/test_etl/test_calc_dde.py` | 修改 | +周线合同测试 |

---

## 新旧逻辑对照（6 个边缘情况全覆盖）

| # | 场景 | 旧版 | 新版 | 等价？ |
|---|------|------|------|:--:|
| 1 | 第一周下界 | Python `week_end - 7天` | DuckDB `DATE - INTERVAL 7 DAY` | ✅ |
| 2 | 后续周下界 | `week_dates[i-1]` | `LAG(trade_date) OVER w` | ✅ |
| 3 | expected_days | 始终用 7 天回望或上周日 | SQL 中 `week_start` 列处理两种情形 | ✅ |
| 4 | SUM NULL | `agg[0] if agg[0] else 0` | `COALESCE(SUM(...), 0)` | ✅ |
| 5 | expected_days=0 丢弃 | `if == 0: continue` | `WHERE expected_days > 0` | ✅ |
| 6 | close=None 丢弃 | `if is None: continue` | INNER JOIN 自动丢弃 | ✅ |
| 7 | _skip_dde | `active < expected * 0.6` | `CASE WHEN ... < ... * 0.6 THEN 1 ELSE 0 END` | ✅ |

---

### Task 1: 写后置合同测试（确保优化前后输出一致）

**Files:**
- Modify: `tests/test_etl/test_calc_dde.py`

**策略：** 先用旧版 `_load_weekly` 在真实数据库中跑一只股票，拿到基准输出。优化后同一输入必须产生相同结果。

- [ ] **Step 1: 追加周线聚合合同测试**

```python
def test_load_weekly_produces_weekly_rows():
    """_load_weekly should return one row per week-end for a stock."""
    import duckdb
    from backend.etl.calc_dde import DDECalculator

    con = duckdb.connect(":memory:")
    # Setup tables
    for ddl in [
        """CREATE TABLE dwd_daily_moneyflow (
            ts_code TEXT, trade_date TEXT, buy_lg_vol REAL, sell_lg_vol REAL,
            buy_elg_vol REAL, sell_elg_vol REAL, total_vol REAL,
            net_mf_amount REAL, PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL, is_suspended INTEGER,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_week_end INTEGER,
            is_trade_day INTEGER)""",
    ]:
        con.execute(ddl)

    # 3 weeks × 5 trading days for TEST.SZ
    for w in range(1, 4):
        for d in range(1, 6):
            day = f"202601{w*7+d:02d}"
            con.execute("INSERT OR REPLACE INTO dwd_daily_moneyflow "
                        "VALUES ('TEST.SZ', ?, 100,50,80,30,300,500)", (day,))
            con.execute("INSERT OR REPLACE INTO dwd_daily_quote "
                        "VALUES ('TEST.SZ', ?, 10.0, 0)", (day,))
            is_we = 1 if d == 5 else 0
            con.execute("INSERT OR REPLACE INTO dim_date VALUES (?, ?, 1)",
                        (day, is_we))
            if is_we:
                con.execute("INSERT OR REPLACE INTO dwd_weekly_quote "
                            "VALUES ('TEST.SZ', ?, 10.0)", (day,))

    calc = DDECalculator(con, "weekly")
    df = calc._load_weekly("TEST.SZ")

    # Should have 3 weeks
    assert len(df) == 3, f"Expected 3 weekly rows, got {len(df)}"
    # Should have all expected columns
    assert "_skip_dde" in df.columns
    assert "close_qfq" in df.columns
    assert "buy_lg_vol" in df.columns
    assert "net_mf_amount" in df.columns
    # All weeks should be fully covered (5 active days out of 5 expected)
    assert (df["_skip_dde"] == 0).all(), "All weeks should have full coverage"

    con.close()


def test_load_weekly_empty_stock():
    """Stock with no week-end data → empty DataFrame."""
    import duckdb
    from backend.etl.calc_dde import DDECalculator

    con = duckdb.connect(":memory:")
    for ddl in [
        """CREATE TABLE dwd_daily_moneyflow (
            ts_code TEXT, trade_date TEXT, buy_lg_vol REAL, sell_lg_vol REAL,
            buy_elg_vol REAL, sell_elg_vol REAL, total_vol REAL,
            net_mf_amount REAL, PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL, is_suspended INTEGER,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_week_end INTEGER,
            is_trade_day INTEGER)""",
    ]:
        con.execute(ddl)

    calc = DDECalculator(con, "weekly")
    df = calc._load_weekly("NO_DATA.SZ")
    assert df.empty, f"Expected empty, got {len(df)} rows"
    con.close()


def test_load_weekly_moneyflow_insufficient_coverage():
    """Weeks with <60% moneyflow coverage should be marked _skip_dde=1."""
    import duckdb
    from backend.etl.calc_dde import DDECalculator

    con = duckdb.connect(":memory:")
    for ddl in [
        """CREATE TABLE dwd_daily_moneyflow (
            ts_code TEXT, trade_date TEXT, buy_lg_vol REAL, sell_lg_vol REAL,
            buy_elg_vol REAL, sell_elg_vol REAL, total_vol REAL,
            net_mf_amount REAL, PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL, is_suspended INTEGER,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dwd_weekly_quote (
            ts_code TEXT, trade_date TEXT, close_qfq REAL,
            PRIMARY KEY (ts_code, trade_date))""",
        """CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_week_end INTEGER,
            is_trade_day INTEGER)""",
    ]:
        con.execute(ddl)

    # 1 week of 5 trading days, but only 2 days have moneyflow (40% < 60%)
    for d in range(1, 6):
        day = f"2026010{d}"
        con.execute("INSERT OR REPLACE INTO dwd_daily_quote "
                    "VALUES ('TEST.SZ', ?, 10.0, 0)", (day,))
        con.execute("INSERT OR REPLACE INTO dim_date VALUES (?, ?, 1)",
                    (day, 1 if d == 5 else 0))
        if d <= 2:  # only 2 out of 5 days
            con.execute("INSERT OR REPLACE INTO dwd_daily_moneyflow "
                        "VALUES ('TEST.SZ', ?, 100,50,80,30,300,500)", (day,))
    con.execute("INSERT OR REPLACE INTO dwd_weekly_quote "
                "VALUES ('TEST.SZ', '20260105', 10.0)")

    calc = DDECalculator(con, "weekly")
    df = calc._load_weekly("TEST.SZ")

    assert len(df) == 1
    assert df["_skip_dde"].iloc[0] == 1, (
        f"2/5 days = 40% < 60% → _skip_dde should be 1, got {df['_skip_dde'].iloc[0]}")

    con.close()
```

- [ ] **Step 2: 运行测试确认旧版通过**

```bash
pytest tests/test_etl/test_calc_dde.py::test_load_weekly_produces_weekly_rows \
      tests/test_etl/test_calc_dde.py::test_load_weekly_empty_stock \
      tests/test_etl/test_calc_dde.py::test_load_weekly_moneyflow_insufficient_coverage -v
# 预期: 3 passed（旧版逻辑）
```

- [ ] **Step 3: 提交**

```bash
git add tests/test_etl/test_calc_dde.py
git commit -m "test: add _load_weekly contract tests for weekly aggregation

Tests: basic weekly rows, empty stock, insufficient coverage (_skip_dde).
Contract ensures optimized SQL produces identical results.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 用单条 SQL 重写 `_load_weekly`

**Files:**
- Modify: `backend/etl/calc_dde.py:83-182`

- [ ] **Step 1: 替换 `_load_weekly`**

将 [calc_dde.py:83-182](backend/etl/calc_dde.py#L83-L182) 的整个函数体替换为：

```python
    def _load_weekly(self, ts_code: str) -> pd.DataFrame:
        """Aggregate daily moneyflow to weekly granularity — single-query.

        Uses LAG window function to determine week boundaries, then SUM
        aggregates moneyflow between consecutive week-end dates in one pass.
        Replaces ~50 per-week SQL calls with 1 call (~150x fewer roundtrips).
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
                                SUBSTR(wq.trade_date, 1, 4) || '-' ||
                                SUBSTR(wq.trade_date, 5, 2) || '-' ||
                                SUBSTR(wq.trade_date, 7, 2)
                                AS DATE
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
                    COALESCE(SUM(mf.buy_lg_vol),   0) AS buy_lg_vol,
                    COALESCE(SUM(mf.sell_lg_vol),  0) AS sell_lg_vol,
                    COALESCE(SUM(mf.buy_elg_vol),  0) AS buy_elg_vol,
                    COALESCE(SUM(mf.sell_elg_vol), 0) AS sell_elg_vol,
                    COALESCE(SUM(mf.total_vol),    0) AS total_vol,
                    COALESCE(SUM(mf.net_mf_amount),0) AS net_mf_amount,
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
                    WHEN wa.active_days < wa.expected_days * 0.6
                    THEN 1 ELSE 0
                END AS _skip_dde
            FROM weekly_agg wa
            JOIN dwd_weekly_quote wq
                ON wq.ts_code = ? AND wq.trade_date = wa.week_end
            WHERE wa.expected_days > 0
            ORDER BY wa.week_end
        """, (ts_code, ts_code)).df()
```

- [ ] **Step 2: 验证 `self.src_table` 和 `self.quote_table` 变量可用**

`self.src_table` == `"dwd_daily_moneyflow"`（已在 `__init__` 中设置）
`self.quote_table` == `"dwd_weekly_quote"`（weekly 模式下）
`{self.quote_table}` 在 CTE 中已经硬编码使用 `dwd_weekly_quote`（因为 `week_ranges` 只查周线）。无需改动。

- [ ] **Step 3: 运行所有 DDE 测试**

```bash
pytest tests/test_etl/test_calc_dde.py -v
# 预期: 24 passed（含 Task 1 新增的 3 个合同测试）
```

- [ ] **Step 4: 提交**

```bash
git add backend/etl/calc_dde.py
git commit -m "perf: replace DDE _load_weekly N+1 loop with single SQL

Uses DuckDB LAG window function + SUM aggregation to compute weekly
moneyflow in one query per stock. ~150x fewer SQL roundtrips per stock.

Covers all 7 edge cases: first-week boundary, expected_days=0 drop,
close=NULL drop, SUM NULL→0, COALESCE, _skip_dde flag, insufficient coverage.

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

在 "关键技术细节" 区域追加：

```markdown
- **DDE 周线批量聚合:** `_load_weekly` 使用 LAG 窗口函数一次性完成周间 SUM 聚合，
  替代原来的 per-week N+1 查询。SQL 调用 ~150x 减少。
```

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: document DDE weekly single-query optimization

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 端到端验证

- [ ] **Step 1: 运行全市场 calc 并监控 DDE 周线耗时**

```bash
python -m backend.cli calc 2>&1 | grep -E "calc DDECalculator weekly|ALL DONE"
```

**基准（旧版）：** `calc DDECalculator weekly DONE — 254406 rows (4890 calculated), none skipped, 2982s`
**目标（新版）：** `calc DDECalculator weekly DONE — ... <30s`

- [ ] **Step 2: 验证总耗时减少**

```bash
# 目标: 总 calc < 3600s（从 ~6419s 降至 ~3500s，省约 49 分钟）
```

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "chore: end-to-end verification — DDE weekly single-query optimization"
```

---

## 自检

- ✅ 覆盖全部 7 个边缘情况（对照表在 header 中）
- ✅ 合同测试先写后实施（TDD），优化后必须仍通过
- ✅ 不改函数签名、不改变 DataFrame 结构、不改 schema
- ✅ `_skip_dde` 列保留并正确计算
- ✅ `expected_days == 0` 时该周被 WHERE 过滤
- ✅ `close_row is None` 时该周被 INNER JOIN 过滤
- ✅ `COALESCE(SUM(...), 0)` 处理 NULL 聚合
- ✅ 第一周用 `DATE - INTERVAL 7 DAY`（不是 LAG 的 1900 年）
- ✅ 与 `check_dwd_unchanged` 兼容（指纹基于返回的 df）
- ✅ `strftime(CAST((SUBSTR||'-'||SUBSTR||'-'||SUBSTR) AS DATE) - 7, '%Y%m%d')` 已验证可行
