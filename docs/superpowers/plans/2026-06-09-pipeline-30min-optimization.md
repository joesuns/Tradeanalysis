# 新日 Pipeline ≤30min 优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 稳态真新日 `run --date YYYYMMDD`（fetch → DWD 最小增量 → calc APPEND → export）墙钟 **≤30 min**（典型 **8–20 min**）；同日复跑 **≤60s**；四条用户硬约束全部可验收。

**Architecture:** 在已落地 `DWD_INCREMENTAL` + `CALC_APPEND` 上，消除 DWD 层两处「有必要的数据、没必要的全量实现」：**(1) weekly 周分区增量**（tail 股只重算含新日的自然周）；(2) **qfq SQL UPDATE**（除权/qfq 漂移不 DELETE 全历史 daily）。Phase 3 加 rebuild 护栏 + `benchmark_run.py` SLA 门禁。Calc「整股 poison chunk」延 Phase 4（条件触发，非本 plan 阻塞项）。

**Tech Stack:** Python 3.9、DuckDB ≥1.0、pytest；周锚点 `date_trunc('week', …)` 与 `dim_date.is_week_end` 一致。

**前置文档：** `docs/superpowers/plans/2026-06-09-dwd-incremental-rebuild.md`（daily tail 已落地）、`docs/superpowers/plans/2026-06-09-calc-fundamental-performance.md`（Phase 4 引用）

**用户决策（已批准）：** 1a / 2a / 3b / 4b / 5a（DWD 后）/ 6

---

## 一、架构师评估结论（SLA 合同）

### 1.1 我们承诺什么（用户四条硬约束 — 2026-06-12 审批）

| # | 用户要求 | SLA（稳态真新日） | 验收命令 |
|---|---------|------------------|---------|
| **1** | 整条链路 ≤30 min（**含 export**） | `cli run` 墙钟 **≤1800s** | `python3 scripts/benchmark_run.py --date $ODS_MAX --run`（**不得** `--skip-export`） |
| **2** | 数据质量 | `health_check` 无 CRITICAL + 核心 pytest 全绿 | `python3 scripts/health_check.py`；`pytest tests/ -v` |
| **3** | 不做不必要计算 | stale 子集 DWD；APPEND/SKIP 路由；chunk 仅 FULL 指标（P4 后） | 日志 `mode=week=`；`chunk_work_items` / `full_by_indicator` |
| **4** | 不全库 rebuild | 日常仅 `rebuild_dwd_for_stale` | grep 无 `dwd.rebuild stocks=all` |

**同日复跑（辅助 KPI，非用户第 1 条字面范围）：** 第二次 `run --date X --skip-export` 目标 **≤60s**（当前 ~319s，待 P4 后优化）。

**典型墙钟（稳态，含 export）：** 8–20 min（fetch 3–5min + DWD 2–5min + calc 1–5min + export 2–5min）。

### 1.2 我们不承诺什么（必须在 benchmark 报告标注）

| 场景 | 原因 | 运维动作 |
|------|------|---------|
| **SIGNATURE / SPEC 迁移日** | 大量 FULL（如 kpattern 补 `pct_chg`） | 记录 `full_by_indicator`；**不拦 30min 交付**；跑后 L3 spot-check |
| 缺 state 首日 | 永久 FULL chunk | 一次性 `backfill-state` |
| `CALC_FORCE_HARD=1` | 人为全量 | runbook 禁止 |
| `DWD_INCREMENTAL=0` | 全量 rebuild | runbook 禁止 |
| tushare 限流/网络 | fetch 不可控 | 非本 plan |
| 单独 `cli calc`（无 run 编排） | 无 batch 上下文，墙钟失真 | 日常只用 `cli run` |

### 1.3 墙钟预算（优化后稳态）

| 阶段 | 当前瓶颈 | 目标 | 本 plan |
|------|---------|------|---------|
| fetch | ~3–5 min | ~3–5 min | **不改** |
| DWD | **10–57 min**（weekly 全历史） | **2–5 min** | Phase 1–2 |
| calc | 34s（稳态）～25 min（chunk 多） | **1–5 min** | 现有 APPEND；Phase 4 兜底 |
| export | ~2–5 min | ~2–5 min | **不改** |
| **合计** | 15–90+ min | **8–20 min** | **≤30 min 可达成** |

实库锚点：稳态 calc `batch_only=5183, chunk=205` → **45–256s**；灾难路径 `6916aea6`（DWD 57min）、`08e05f09`（FORCE >9h，已终止）。

---

## 二、四条硬约束 → 方案映射

| # | 用户要求 | 根因（已审计） | Plan 对策 | 验收 |
|---|---------|---------------|----------|------|
| **1** | 整条链路 ≤30 min | DWD weekly 全历史 DELETE+重插；偶发全量 daily rebuild | Phase 1 weekly 周分区；Phase 2 qfq UPDATE；Task 5 benchmark | `benchmark_run` PASS |
| **2** | 数据质量 | 增量路径无 oracle 则不可 ship | Task 2/3 等价测试 `atol=1e-6`；保留 APPEND 强签名 | pytest + health_check |
| **3** | 不做不必要计算 | tail 股 weekly 重算 2000+ 行/股；除权 DELETE 全历史；any-FULL 拖整股 chunk | Phase 1–2 消 DWD 浪费；Phase 4 **条件** 消 calc 浪费 | 日志 `mode=week=`；`chunk_codes` 计数 |
| **4** | 不全库 rebuild | `build_dwd_weekly_quote` 无 tail；除权走 full daily | stale 子集 + 三分支 daily + 双路径 weekly；Task 4 护栏 | 无 `dwd.rebuild stocks=all` |

### 日常 rebuild 最小域（目标语义）

```
新日 stale 股 (~5000)
├── tail 股 (~4990+)
│   ├── daily: INSERT 1 行 (trade_date)
│   ├── weekly: DELETE+INSERT 仅含 trade_date 的自然周 (~1–5 行)
│   └── moneyflow: INSERT 1 行
├── qfq 股 (~0–20/日)
│   ├── daily: UPDATE 全历史 qfq 四列（语义必要，实现轻）
│   ├── weekly: 整股 full（除权后 rolling 全历史变）
│   └── gap 时才有 suspension_fill
└── 新股 (~极少)
    └── daily + weekly: 该股 first-time full insert
```

**禁止：** 日常 `rebuild_all_dwd(con, 全市场)`；tail 股 weekly 全历史 DELETE。

---

## 三、差距清单（实施前 → 实施后）

| ID | 问题 | 文件/证据 | Phase |
|----|------|----------|-------|
| G1 | weekly 无 `incremental_trade_date` | `build_dwd.py:167` 整子集 full | 1 |
| G2 | 除权/qfq 走 `build_dwd_daily_quote` DELETE+fill | `find_stocks_needing_full_daily_rebuild` | 2 |
| G3 | 无端到端 SLA 门禁 | 无 `benchmark_run.py` | 3 |
| G4 | 无日常 runbook | 终端 FORCE 误用 | 3 |
| G5 | any-FULL → 整股 chunk（calc 层） | `orchestrator._calc_stock_chunk` | 4（条件） |

---

## 四、File Map

| 文件 | 职责 |
|------|------|
| `backend/etl/dwd_weekly_sql.py` | **新建** — weekly rolling SQL 复用 |
| `backend/etl/build_dwd.py` | weekly 周分区；qfq UPDATE；`rebuild_dwd_incremental` 分叉；护栏 |
| `backend/etl/orchestrator.py` | 审计：auto-fetch 仅 `rebuild_dwd_for_stale` |
| `tests/test_etl/test_build_dwd_weekly_incremental.py` | **新建** — weekly oracle |
| `tests/test_etl/test_qfq_update.py` | **新建** — qfq UPDATE oracle |
| `tests/test_etl/test_build_dwd_guardrails.py` | **新建** — 大批量 rebuild WARNING |
| `scripts/benchmark_run.py` | **新建** — SLA 1800s + 分项摘要 |
| `docs/superpowers/plans/2026-06-09-daily-runbook.md` | **新建** — 决策 6 |
| `CLAUDE.md` / spec v1.12 | DWD 增量语义 + benchmark 命令 |

---

## 五、阶段门禁（Go / No-Go）

| 阶段 | 完成标准 | 未达标则 |
|------|---------|---------|
| **P0** 运维 baseline | `dwd.rebuild_incremental` 出现；baseline 写入 plan 附录 | 查 `DWD_INCREMENTAL` / 误走全量 |
| **P1** weekly | pytest PASS；DWD weekly **≤10 min**（同日复测） | 不进入 P2 |
| **P2** qfq | pytest PASS；除权日日志 `dwd.qfq_update` | 不进入 P3 |
| **P3** 验收 | `benchmark_run` **SLA PASS**；runbook 就位 | 启动 **P4** calc chunk |
| **P4** calc chunk | 引用 `calc-fundamental-performance`；calc **≤5 min** | 单独立项 |

**P4 启动条件（满足任一即启动）：** benchmark 端到端 **>1500s** 且 `chunk_codes>400`；或 calc 阶段 **>300s**。

---

## 六、实施任务

### Phase 0 — 运维 Baseline（决策 1a / 2a）

> 无代码。先停 FORCE，再正常 run。

#### Task 0: 补跑 20260609 + 记录 baseline

**Files:** 无

- [ ] **Step 1: 停止 `CALC_FORCE_HARD` 进程**

```bash
ps aux | grep "backend.cli calc" | grep -v grep
# 若有 → kill -INT <pid>
```

- [ ] **Step 2: 正常补跑**

```bash
cd /Users/joesun/Trae/Tradeanalysis
python -m backend.cli run --date 20260609
```

- [ ] **Step 3: 验证增量路径（2a）**

```bash
grep -E "progress dwd\.(rebuild_incremental|rebuild):" data/tradeanalysis.log | tail -5
```

Expected: `dwd.rebuild_incremental`；**不应** `dwd.rebuild: started | stocks=all`。

- [ ] **Step 4: 记录 baseline 到 plan 附录**

```sql
-- DuckDB
SELECT step_name, duration_sec, row_count, data_completeness
FROM ods_etl_log
WHERE step_name IN ('run_fetch','run_rebuild_dwd','calc','run_export')
ORDER BY created_at DESC LIMIT 8;
```

- [ ] **Step 5: health_check**

```bash
python -m scripts.health_check
```

**P0 Gate:** baseline 已记录 + `dwd.rebuild_incremental` 确认。

---

### Phase 1 — Weekly 周分区增量（决策 3b）

> 满足约束 **1、3、4** 的 DWD 主路径。

#### Task 1: 抽取 weekly SQL

**Files:**
- Create: `backend/etl/dwd_weekly_sql.py`
- Create: `tests/test_etl/test_build_dwd_weekly_incremental.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_etl/test_build_dwd_weekly_incremental.py
from backend.etl.dwd_weekly_sql import weekly_insert_select_sql


def test_weekly_sql_contains_date_trunc_partition():
    sql = weekly_insert_select_sql(
        ts_code_filter="AND d.ts_code IN (?)",
        week_filter="",
    )
    assert "date_trunc('week'" in sql
    assert "FIRST_VALUE(d.open_qfq) OVER w" in sql
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/test_etl/test_build_dwd_weekly_incremental.py::test_weekly_sql_contains_date_trunc_partition -v
```

- [ ] **Step 3: Implement `dwd_weekly_sql.py`**

```python
# backend/etl/dwd_weekly_sql.py
"""Shared SQL for dwd_weekly_quote rolling WTD bars."""


def _trade_date_to_date_expr(col: str = "d.trade_date") -> str:
    return (
        f"CAST(substr({col},1,4)||'-'||substr({col},5,2)||'-'||substr({col},7,2) AS DATE)"
    )


def week_trunc_expr(col: str = "d.trade_date") -> str:
    return f"date_trunc('week', {_trade_date_to_date_expr(col)})"


def weekly_insert_select_sql(ts_code_filter: str, week_filter: str = "") -> str:
    dt = _trade_date_to_date_expr("d.trade_date")
    return f"""
        SELECT
            d.ts_code,
            d.trade_date,
            FIRST_VALUE(d.open_qfq) OVER w AS open_qfq,
            MAX(d.high_qfq) OVER w AS high_qfq,
            MIN(d.low_qfq) OVER w AS low_qfq,
            d.close_qfq AS close_qfq,
            SUM(d.vol) OVER w / COUNT(*) OVER w * 5 AS vol,
            SUM(d.amount) OVER w / COUNT(*) OVER w * 5 AS amount,
            SUM(d.pct_chg) OVER w AS pct_chg,
            d.total_mv, d.pe_ttm, d.turnover_rate, d.volume_ratio,
            COUNT(*) OVER w AS active_days
        FROM dwd_daily_quote d
        WHERE d.is_suspended = 0 {ts_code_filter}{week_filter}
        WINDOW w AS (PARTITION BY d.ts_code, date_trunc('week', {dt})
                     ORDER BY d.trade_date
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    """
```

- [ ] **Step 4: Run — PASS**

```bash
pytest tests/test_etl/test_build_dwd_weekly_incremental.py::test_weekly_sql_contains_date_trunc_partition -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/etl/dwd_weekly_sql.py tests/test_etl/test_build_dwd_weekly_incremental.py
git commit -m "refactor: extract dwd weekly rolling SQL for reuse"
```

---

#### Task 2: `build_dwd_weekly_quote` 周分区增量

**Files:**
- Modify: `backend/etl/build_dwd.py`
- Test: `tests/test_etl/test_build_dwd_weekly_incremental.py`

- [ ] **Step 1: Write failing test — 历史 frozen**

```python
def _seed_two_weeks(temp_db):
    from backend.db.schema import create_all_tables
    from backend.etl.build_dim import build_dim_stock, build_dim_date
    from backend.etl.build_dwd import build_dwd_daily_quote, build_dwd_weekly_quote

    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES "
        "('20260105',1),('20260106',1),('20260107',1),('20260108',1),('20260109',1),"
        "('20260112',1),('20260113',1)"
    )
    rows = [
        ("20260105", 10.0, 11.0, 9.0, 10.5, 100),
        ("20260106", 10.5, 12.0, 10.0, 11.0, 200),
        ("20260107", 11.0, 11.5, 10.5, 10.8, 150),
        ("20260108", 10.8, 12.5, 10.5, 12.0, 180),
        ("20260109", 12.0, 13.0, 11.5, 12.5, 220),
        ("20260112", 12.5, 13.5, 12.0, 13.0, 210),
    ]
    for td, o, h, l, c, v in rows:
        temp_db.execute(
            "INSERT INTO ods_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,adj_factor) "
            "VALUES ('TEST.SZ',?,?,?,?,?,?,?,0.01,1.0)",
            [td, o, h, l, c, v, float(v * 10)],
        )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    build_dwd_weekly_quote(temp_db, ["TEST.SZ"])


def test_weekly_incremental_preserves_prior_weeks(temp_db):
    from backend.etl.build_dwd import build_dwd_daily_quote, build_dwd_weekly_quote

    _seed_two_weeks(temp_db)
    before = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date <= '20260109' "
        "ORDER BY trade_date"
    ).fetchall()

    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,adj_factor) "
        "VALUES ('TEST.SZ','20260113',13.1,13.8,12.8,13.5,230,2300,0.02,1.0)"
    )
    build_dwd_daily_quote(temp_db, ["TEST.SZ"], incremental_trade_date="20260113")
    build_dwd_weekly_quote(temp_db, ["TEST.SZ"], incremental_trade_date="20260113")

    after_frozen = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date <= '20260109' "
        "ORDER BY trade_date"
    ).fetchall()
    assert before == after_frozen

    row = temp_db.execute(
        "SELECT active_days FROM dwd_weekly_quote "
        "WHERE ts_code='TEST.SZ' AND trade_date='20260113'"
    ).fetchone()
    assert row is not None
    assert row[0] == 2
```

- [ ] **Step 2: Run — FAIL**

```bash
pytest tests/test_etl/test_build_dwd_weekly_incremental.py::test_weekly_incremental_preserves_prior_weeks -v
```

- [ ] **Step 3: Implement incremental weekly**

`build_dwd_weekly_quote(con, ts_codes=None, incremental_trade_date=None)`:

- `incremental_trade_date is None` → 现有 full（DELETE 子集 + 全历史 INSERT，改用 `dwd_weekly_sql`）
- `incremental_trade_date` set → 必须带 `ts_codes`：
  - DELETE 仅 `date_trunc('week', w.trade_date) = date_trunc('week', incremental_trade_date)` 分区
  - INSERT 仅同分区 daily 行
  - 日志：`progress dwd.weekly_quote: started | stocks=N mode=week=YYYYMMDD`

- [ ] **Step 4: Wire `rebuild_dwd_incremental`**

```python
full_codes = sorted(set(qfq_codes) | set(insert_codes))  # Phase 2 命名；Task 2 先用 find_stocks_needing_full_daily_rebuild
tail_codes = [c for c in ts_codes if c not in set(full_codes)]

n_weekly = 0
if tail_codes:
    n_weekly += build_dwd_weekly_quote(
        con, tail_codes, incremental_trade_date=trade_date,
    )
if full_codes:
    n_weekly += build_dwd_weekly_quote(con, full_codes)
```

- [ ] **Step 5: Oracle test — incremental week == full rebuild**

```python
def test_weekly_incremental_matches_full_oracle(temp_db):
    from backend.etl.build_dwd import build_dwd_daily_quote, build_dwd_weekly_quote

    _seed_two_weeks(temp_db)
    temp_db.execute(
        "INSERT INTO ods_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,adj_factor) "
        "VALUES ('TEST.SZ','20260113',13.1,13.8,12.8,13.5,230,2300,0.02,1.0)"
    )
    build_dwd_daily_quote(temp_db, ["TEST.SZ"], incremental_trade_date="20260113")

    build_dwd_weekly_quote(temp_db, ["TEST.SZ"], incremental_trade_date="20260113")
    inc_rows = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date >= '20260112' "
        "ORDER BY trade_date"
    ).fetchall()

    temp_db.execute("DELETE FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date >= '20260112'")
    build_dwd_weekly_quote(temp_db, ["TEST.SZ"])
    full_rows = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, active_days "
        "FROM dwd_weekly_quote WHERE ts_code='TEST.SZ' AND trade_date >= '20260112' "
        "ORDER BY trade_date"
    ).fetchall()

    assert len(inc_rows) == len(full_rows)
    for a, b in zip(inc_rows, full_rows):
        assert a[0] == b[0]
        for i in range(1, len(a)):
            assert abs(float(a[i]) - float(b[i])) < 1e-6
```

- [ ] **Step 6: Run all**

```bash
pytest tests/test_etl/test_build_dwd.py tests/test_etl/test_build_dwd_weekly_incremental.py -v
```

- [ ] **Step 7: Commit**

```bash
git commit -m "feat: weekly partition incremental rebuild for tail stocks"
```

**P1 Gate:** pytest PASS + 实库/同日复测 DWD **≤600s**（日志 `mode=week=`）。

---

### Phase 2 — qfq SQL UPDATE（决策 4b）

> 满足约束 **2、3、4**：除权语义必要，DELETE 不必要。

#### Task 3: `refresh_qfq_prices`

**Files:**
- Modify: `backend/etl/build_dwd.py`
- Create: `tests/test_etl/test_qfq_update.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_etl/test_qfq_update.py
from backend.etl.build_dwd import (
    build_dwd_daily_quote,
    refresh_qfq_prices,
    find_stocks_needing_qfq_refresh,
)


def _seed_adj_stock(temp_db):
    from backend.db.schema import create_all_tables
    from backend.etl.build_dim import build_dim_stock, build_dim_date

    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','Test')"
    )
    temp_db.execute(
        "INSERT INTO ods_trade_cal (cal_date,is_open) VALUES "
        "('20260101',1),('20260102',1),('20260103',1)"
    )
    temp_db.executemany(
        "INSERT INTO ods_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg,adj_factor) "
        "VALUES ('TEST.SZ',?,?,?,?,?,?,?,0.01,1.0)",
        [
            ("20260101", 10.0, 10.5, 9.5, 10.0, 100, 1000.0),
            ("20260102", 10.2, 10.3, 10.0, 10.2, 110, 1100.0),
            ("20260103", 10.5, 10.6, 10.1, 10.5, 120, 1200.0),
        ],
    )
    temp_db.executemany(
        "INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) VALUES ('TEST.SZ',?,1000)",
        [("20260101",), ("20260102",), ("20260103",)],
    )
    build_dim_stock(temp_db)
    build_dim_date(temp_db)
    build_dwd_daily_quote(temp_db, ["TEST.SZ"])


def test_refresh_qfq_matches_full_rebuild_after_adj_change(temp_db):
    _seed_adj_stock(temp_db)
    oracle = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq "
        "FROM dwd_daily_quote WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    temp_db.execute(
        "UPDATE ods_daily SET adj_factor=2.0 WHERE ts_code='TEST.SZ' AND trade_date='20260101'"
    )
    assert find_stocks_needing_qfq_refresh(temp_db, ["TEST.SZ"], "20260103") == ["TEST.SZ"]

    refresh_qfq_prices(temp_db, ["TEST.SZ"])
    updated = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq "
        "FROM dwd_daily_quote WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    build_dwd_daily_quote(temp_db, ["TEST.SZ"])
    rebuilt = temp_db.execute(
        "SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq "
        "FROM dwd_daily_quote WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()

    assert len(updated) == len(rebuilt)
    for u, r in zip(updated, rebuilt):
        assert u[0] == r[0]
        for i in range(1, 5):
            assert abs(float(u[i]) - float(r[i])) < 1e-6
    assert abs(float(updated[0][4]) - 20.0) < 1e-6
```

- [ ] **Step 2: Run — FAIL**

```bash
pytest tests/test_etl/test_qfq_update.py -v
```

- [ ] **Step 3: Implement split + UPDATE**

新增：

- `find_stocks_needing_qfq_refresh(con, ts_codes, trade_date)` — adj 变 + qfq drift
- `find_stocks_needing_full_daily_insert(con, ts_codes, trade_date)` — 无 DWD 历史新股
- `refresh_qfq_prices(con, ts_codes)` — UPDATE 四列 qfq（见上一版 plan SQL）
- `_suspension_fill_subset(con, gap_codes)` — 从 `build_dwd_daily_quote` 提取 fill 循环

`rebuild_dwd_incremental` daily 三分支 + weekly 双路径（qfq/insert → full weekly；tail → week 分区）。

- [ ] **Step 4: 迁移现有测试**

- `test_find_stocks_needing_full_daily_rebuild` → 拆为 qfq_refresh / full_insert
- `test_rebuild_dwd_incremental_full_path_on_adj_drift` → 断言 `dwd.qfq_update` 而非 DELETE

- [ ] **Step 5: Run**

```bash
pytest tests/test_etl/test_qfq_update.py tests/test_etl/test_build_dwd.py -v
```

- [ ] **Step 6: Commit**

```bash
git commit -m "feat: qfq refresh via SQL UPDATE instead of full daily rebuild"
```

**P2 Gate:** pytest PASS + 除权日日志见 `dwd.qfq_update`（非 `full daily rebuild`）。

---

### Phase 3 — 护栏 + Benchmark + Runbook（决策 2a / 6）

#### Task 4: Rebuild 护栏

**Files:**
- Modify: `backend/etl/build_dwd.py`
- Modify: `backend/etl/orchestrator.py`（只读审计 + 必要时小改）
- Create: `tests/test_etl/test_build_dwd_guardrails.py`

- [ ] **Step 1: Test — >500 股 WARNING**

```python
import logging


def test_rebuild_all_dwd_warns_on_large_subset(temp_db, caplog):
    from backend.etl.build_dwd import rebuild_all_dwd

    codes = ["{:06d}.SZ".format(i) for i in range(501)]
    with caplog.at_level(logging.WARNING):
        rebuild_all_dwd(temp_db, codes)
    assert any("large subset rebuild" in r.message for r in caplog.records)
```

- [ ] **Step 2: Implement `_LARGE_REBUILD_WARN = 500`**

- [ ] **Step 3: 审计 orchestrator**

确认 `run_calc` 仅 `rebuild_dwd_for_stale`（行 1331/1356）；`rebuild_all_dwd` 仅 legacy `run_etl`。

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: warn on large rebuild_all_dwd subsets"
```

---

#### Task 5: `scripts/benchmark_run.py`

**Files:**
- Create: `scripts/benchmark_run.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: 实现 SLA 脚本（1800s 默认）**

```python
#!/usr/bin/env python3
"""End-to-end run benchmark + SLA gate (default 1800s)."""
import argparse
import subprocess
import sys
import time


def main():
    p = argparse.ArgumentParser(description="Daily pipeline benchmark + SLA gate")
    p.add_argument("--date", required=True)
    p.add_argument("--sla-sec", type=int, default=1800)
    p.add_argument("--skip-export", action="store_true")
    args = p.parse_args()

    cmd = [sys.executable, "-m", "backend.cli", "run", "--date", args.date]
    if args.skip_export:
        cmd.append("--skip-export")

    t0 = time.monotonic()
    rc = subprocess.call(cmd)
    elapsed = time.monotonic() - t0
    print(f"benchmark_run: elapsed={elapsed:.1f}s exit={rc}")

    if rc != 0:
        sys.exit(rc)
    if elapsed > args.sla_sec:
        print(f"SLA FAIL: {elapsed:.1f}s > {args.sla_sec}s", file=sys.stderr)
        sys.exit(2)
    print("SLA PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 扩展 — 读 `ods_etl_log` 打印分项 + grep 提示**

跑完后 SQL 取最近 `run_fetch`/`run_rebuild_dwd`/`calc`/`run_export` 的 `duration_sec`；打印是否需查 `dwd.rebuild_incremental` / `mode=week=`。

- [ ] **Step 3: CLAUDE.md 增加命令**

```bash
python scripts/benchmark_run.py --date 20260609
```

- [ ] **Step 4: Commit**

```bash
git commit -m "feat: benchmark_run SLA gate for daily pipeline"
```

---

#### Task 6: Runbook（决策 6）

**Files:**
- Create: `docs/superpowers/plans/2026-06-09-daily-runbook.md`

- [ ] **Step 1: 写入 runbook**

必含：

| 类别 | 规则 |
|------|------|
| 日常 | `python -m backend.cli run --date YYYYMMDD` |
| 禁止 | `CALC_FORCE_HARD=1`、`DWD_INCREMENTAL=0`、日常 FORCE |
| 同日复跑 | `run --skip-export` 或 `export --date X` |
| 验收 grep | `dwd.rebuild_incremental`、`mode=week=`、`batch_only=` |
| 一次性 | `backfill-state`、`repair-weekly --execute` |
| SLA | `python scripts/benchmark_run.py --date X` |

- [ ] **Step 2: Commit**

```bash
git commit -m "docs: daily pipeline runbook"
```

**P3 Gate（本 plan 交付完成）：**

```bash
pytest tests/test_etl/test_build_dwd_weekly_incremental.py tests/test_etl/test_qfq_update.py tests/test_etl/test_build_dwd_guardrails.py -v
python scripts/benchmark_run.py --date <最新真新日>
python -m scripts.health_check
```

全部 PASS → 约束 **1–4** 在稳态场景验收。

---

### Phase 4 — Calc 指标级 Chunk（决策 5a，条件触发）

> **不在本 plan 阻塞 30 min SLA**；仅当 P3 benchmark 显示 calc/chunk 为余量瓶颈时启动。

**引用：** `docs/superpowers/plans/2026-06-09-calc-fundamental-performance.md` Phase 1（`calc_executor.py`、指标级 FULL 队列）。

| 触发条件 | 动作 |
|---------|------|
| benchmark **≤1500s** 且 chunk **≤400** | **跳过 P4** |
| benchmark **>1500s** 或 calc **>300s** 或 chunk **>400** | 启动 P4 独立 PR |

目标：calc **≤300s**；同日复跑 **≤60s**。

---

### Phase 5 — 文档同步

#### Task 7: CLAUDE.md + Spec v1.12

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`

- [ ] **Step 1: CLAUDE.md** — weekly 周分区、qfq UPDATE、benchmark、runbook 链接

- [ ] **Step 2: Spec v1.12** — DWD 增量三节（daily 三分支 / weekly 双路径 / 禁止全库）

- [ ] **Step 3: Commit**

```bash
git commit -m "docs: DWD incremental semantics v1.12 + benchmark runbook"
```

---

## 七、总验收清单

| # | 约束 | 验收项 |
|---|------|--------|
| 1 | ≤30 min | `benchmark_run` exit 0，elapsed ≤1800s |
| 2 | 质量 | pytest oracle PASS；`health_check` 无 CRITICAL |
| 3 | 无多余计算 | 日志 `mode=week=`；无 tail 股 weekly 全历史 DELETE |
| 4 | 不全库 rebuild | 无 `dwd.rebuild stocks=all`；>500 WARNING 可见 |
| + | 同日复跑 | 第二次 `run --skip-export` ≤60s |

```bash
pytest tests/test_etl/test_build_dwd.py tests/test_etl/test_build_dwd_weekly_incremental.py \
  tests/test_etl/test_qfq_update.py tests/test_etl/test_build_dwd_guardrails.py -v
pytest tests/ -v
python scripts/benchmark_run.py --date YYYYMMDD
python -m scripts.health_check
```

---

## 八、Self-Review

| 用户约束 | Plan 覆盖 | 门禁 |
|---------|----------|------|
| 1 ≤30 min | P1–2 降 DWD；Task 5 benchmark | P3 SLA PASS |
| 2 质量 | Task 2/3 oracle | pytest |
| 3 无多余计算 | weekly 周分区；qfq UPDATE；P4 条件 | 日志 + chunk 计数 |
| 4 不全库 rebuild | stale 子集；Task 4；runbook | grep |

**Placeholder scan:** Task 2/3 测试已写完整 fixture；无 TBD。

---

## 九、执行顺序

```
P0 baseline（停 FORCE → run 20260609）
  → P1 weekly 周分区 [Gate: DWD ≤600s]
  → P2 qfq UPDATE [Gate: dwd.qfq_update 日志]
  → P3 护栏 + benchmark + runbook [Gate: 墙钟 PASS]
  → P5 文档
  → P4 calc chunk（已立项 2026-06-12，附录 D）[Gate: chunk<400, calc≤300s]
```

---

## 附录 A — Baseline 记录（P0 完成，2026-06-11）

| step | duration_sec | row_count | notes |
|------|-------------|-----------|-------|
| run_fetch | 2 | 21749 | analysis_date=20260609 |
| run_rebuild_dwd | 3425 | 32955736 | weekly 全子集 full；瓶颈 |
| calc_dws | 1529 | 10354837 | batch_only=74, chunk=5315 |
| run_export | 119 | 5277 | |
| **total** | **~5075** | | **84 min，远超 SLA** |

对比好路径（同日前序）：`batch_only=5183, chunk=205` → calc **45–256s**。

P0 运维：`CALC_FORCE_HARD` 进程已终止；`DWD_INCREMENTAL=1` 代码路径存在（`rebuild_dwd_incremental`）。

---

## 十、Subagent 执行计划（已批准 2026-06-11）

**模式：** Subagent-Driven（选项 1）  
**协调者：** 父 agent 派 subagent 实施 → 父 agent 以数据架构师身份做里程碑审核 → 修复偏差

### 里程碑与门禁

| 里程碑 | 任务 | 完成标准 | 架构师审核清单 |
|--------|------|---------|---------------|
| **M0** | P0 baseline | 附录 A 已填；无 FORCE 进程 | baseline 数字与 ods_etl_log 一致 |
| **M1** | P1 Task 1–2 | weekly 周分区 + oracle pytest PASS | ① tail 股只删当周分区 ② 历史周 frozen ③ 日志 `mode=week=` ④ 无 tail weekly 全历史 DELETE |
| **M2** | P2 Task 3 | qfq UPDATE + oracle pytest PASS | ① 除权走 UPDATE 非 daily DELETE ② qfq 股 weekly full 保留 ③ `find_stocks_needing_qfq_refresh` 拆分正确 |
| **M3** | P3 Task 4–6 | 护栏 + benchmark 增强 + runbook | ① benchmark 合并现有脚本 ② 输出 chunk/batch ③ runbook 禁 FORCE/DWD_INCREMENTAL=0 |
| **M4** | P3 Gate | `benchmark_run` SLA + health_check | ① 墙钟 ≤1800s ✅ ② chunk<400 ✅（稳态） ③ health_check ✅ |
| **M5** | P5 Task 7 | CLAUDE.md + spec v1.12 | 文档与代码语义一致 |
| **M6** | **P4（已立项 2026-06-12）** | calc chunk 独立 PR | 见附录 D；实施计划 `2026-06-09-calc-fundamental-performance.md` |

### 执行顺序

```
M0 ✓ → M1 (Task1 SQL抽取 → Task2 weekly增量) → [架构师审核]
     → M2 (Task3 qfq UPDATE) → [架构师审核]
     → M3 (Task4–6) → [架构师审核]
     → M4 SLA 验收（墙钟 PASS / chunk FAIL）→ M5 文档
     → M6 P4 已立项（2026-06-12，见附录 D）
```

### 用户四条约束 — 里程碑验收映射

| 约束 | M1 | M2 | M4 |
|------|----|----|-----|
| 1 ≤30min | DWD 从 57min→≤10min | qfq 减 daily DELETE | benchmark_run PASS |
| 2 质量 | weekly oracle | qfq oracle | health_check |
| 3 无多余计算 | 周分区 | qfq UPDATE | 日志 mode=week= |
| 4 不全库 rebuild | stale+最小域 | 三分支 daily | 无 stocks=all |

**Plan approved. Execution started 2026-06-11 (Subagent-Driven).**

---

## 附录 B — M4 验收事故与恢复（2026-06-11）

| 项 | 状态 |
|----|------|
| M1–M3 + M5 | ✅ 代码与分项验收通过（DWD 增量 ~28s） |
| M4 端到端 SLA | ⚠️ **墙钟 PASS / chunk FAIL**（2026-06-12 第 5 轮签字，见下表） |
| M4 事故（首次） | ❌ chunk=5374、ETA ~2.4h；根因 DWD rebuild 后未 refresh → 全股 FULL |
| 修复 | ✅ `DWD_REBUILD_REFRESH_STATE` + `maybe_refresh_state_after_dwd_rebuild` 接线 |
| 业务交付 | ✅ `export --date 20260609` → 5277 行 |
| state 恢复 | ✅ `refresh-state`（dde_weekly SQL 后 `updated=5405`，preflight `full=0 chunk=0`） |
| 同日复跑验证 | ✅ silent-gap 后 **319s**；`chunk=0`；dde_weekly **37s** |
| batch 假卡死 | ✅ `2026-06-11-batch-preflight-silent-gap.md` Task 1–5 |
| M4 事故（第 4 轮） | ❌ exit=1 @1185s；kpattern `KeyError: pct_chg` |
| 根因（第 4 轮） | batch `quote_tail_columns` 用 SIGNATURE 并集，缺 kpattern 计算列 `pct_chg` |
| 修复（第 4 轮） | ✅ `2026-06-12-kpattern-batch-tail-columns.md` |
| 续跑（第 5 轮） | ✅ `cli calc --date 20260610` exit=0 @**2211s**；chunk=5389；3027165 rows |
| 签字 benchmark（第 5 轮） | ⚠️ 见下表；**触发 P4 立项**（附录 D） |

### M4 签字（2026-06-12 第 5 轮）

```bash
cp data/tradeanalysis.duckdb data/tradeanalysis.pre-$(date +%Y%m%d).duckdb
# 稳态签字须含 export（用户约束 1）；迁移日可用 --skip-export 观测 calc 分项
python3 scripts/benchmark_run.py --date $ODS_MAX --run
python3 scripts/health_check.py
```

**PASS 标准（双档，2026-06-12 审批）：**

| 档位 | 条件 |
|------|------|
| **稳态真新日** | exit 0 + 墙钟 ≤1800s（**含 export**）+ `health_check` 无 CRITICAL + `chunk_stocks<400` |
| **迁移日** | exit 0 + 墙钟 ≤1800s + `health_check` 无 CRITICAL + 记录 `full_by_indicator`；**不卡 chunk** |

| 检查项 | 续跑 `cli calc` | 签字 `benchmark_run --run` | 判定 |
|--------|----------------|---------------------------|------|
| exit | 0 | 0 | ✅ |
| 墙钟 | 2211s | **977s** | ✅ |
| calc_dws | 2211s | **598s** | — |
| batch_only | — | **386** | — |
| chunk_stocks | **5389** | **5003** | ❌ |
| health_check | — | 全 PASS | ✅ |
| **附录 B 正式签字** | — | — | **❌ FAIL**（仅 chunk） |

**第 5 轮根因（chunk 仍高）：** kpattern 周线 `SIGNATURE_COLS` 补 `pct_chg` → 签名变更 → ~5000 股 FULL 进 `calc.stocks`；batch 仅消化 386 股。功能与数据正确性已验收（无 KeyError；`health_check` 全绿）。

**教训：** 禁止验收前全库 rebuild；**DWD rebuild 后必须对齐 state fp**（现默认自动 `run_refresh_state`）；算法变更后仍可手动 `refresh-state`；验收前备份。`dde_weekly` ~38s（附录 C ✅）。**算法/SIGNATURE 变更后须预期 chunk 尖峰**，稳态真新日复测或等 P4 指标级队列落地后再验 chunk<400。

### P4 后签字（20260610 同日复跑，含 export）

| 检查项 | 值 | 判定 |
|--------|-----|------|
| 墙钟 | **1297.6s (~21.6min)** | ✅ SLA |
| chunk_stocks | 0 | ✅ |
| batch_full_items | 5003 | 迁移日 DDE daily |
| health_check | PASS | ✅ |

### 真新日基线（run_id `0fd66428`，20260611，含 export）

| 检查项 | 值 | 判定 |
|--------|-----|------|
| 墙钟 | **4078s (68.0min)** | ❌ SLA |
| fetch+DWD | 29s | ✅ 非瓶颈 |
| refresh_state | 344s | 必要门禁 |
| batch_tails+preflight | ~253s | **M1 目标 <15s** |
| batch_compute | 2925s（MACD 806 + 量能 944 + DDE 746） | **M2 目标** |
| batch_state | 345s | **M1 批写** |
| chunk_stocks | **0** | ✅ P4 达标 |
| batch_only | 5389 | ✅ |
| export | 5187 行 / 105s | ✅ |
| health_check | PASS | ✅ |

**整合结论（2026-06-13）：** P4 编排层已达标；SLA 未过因 **(1) run→calc 无 Context 重复路由 ~11min** + **(2) 慢指标 batch_compute ~49min**。M1 → `2026-06-13-calc-preflight-context-p0.md`；M2 → `calc-fundamental-performance.md` 附录 M2。

### M2d MACD weekly B4 target_indices（2026-06-13）

| 项 | 值 |
|----|-----|
| 计划 | `docs/superpowers/plans/2026-06-13-calc-macd-b4-weekly-m2d.md` |
| 代码 | ✅ `b4_weekly_series_from_daily(target_indices)` + `require_b4_weekly_target_indices` + batch/append 接线 |
| 单元测试 | ✅ `test_b4_macd_weekly_append.py` + `test_append_calc` MACD weekly |
| profile | `python3 scripts/profile_macd_b4_weekly.py --stocks 500 --bars 245` |
| 实库 E4 | 待签字：MACD 周线 APPEND `batch_compute` **4484s → <600s**（须真新日或 `CALC_FORCE_HARD=1`） |
| 20260612 基线（优化前） | run_id `3ad2a26d`：墙钟 **~130min**；MACD 周线 **4484s**；`preflight_source=cold` |
| M1.1 签字（同日复跑） | run_id `a8132c19`：`preflight_source=refresh` ✅；`--force` 未重跑 MACD（state SKIP），仅 DDE FULL |

### M1.1 partial merge 签字槽（2026-06-13）

| 项 | 值 |
|----|-----|
| 计划 | `docs/superpowers/plans/2026-06-13-calc-preflight-merge-m1.1.md` |
| 代码 | ✅ `merge_context_patch` + orchestrator G3/auto-fetch merge |
| 单元/集成测试 | ✅ `test_calc_preflight_context` / `test_orchestrator` / `test_batch_append_hot_path_after_partial_ctx_merge` |
| 实库 E4 | 待签字：`preflight_source=refresh` 且 `preflight_elapsed_sec < 30s`（含 auto_fetch 真新日） |
| 20260612 基线 | MACD 周线 batch ~3800s+ @89%（M2d 优化前） |

### M2c profiling 签字（volume_trend_v2，2026-06-13）

| 项 | 结果 |
|----|------|
| 脚本 | `scripts/profile_volume_trend_v2.py` |
| 样本 | 500 股 × 245 bar（外推 5389） |
| 瓶颈 | `compute_volume_trend_series` O(n²) expanding → **98.7%** volume derived |
| cProfile | 200 股：37200 次 `volume_trend_v2`；热点 `np.percentile` + `pandas rolling.mean` |
| 候选 | APPEND `target_indices` 仅算 new_bars（**~184×** trend 段；末 bar 50/50 等价） |
| 外推收益 | 量能 batch_compute 944s → **~12s**（trend 段）；E2E 仍须 M1+M2 实库签字 |
| 状态 | ✅ M2c+ APPEND + **P2+ FULL 写窗**（`batch_full_volume`）；pytest Q2 ✅；profile 写窗 ~188×；实库 E3 待 M5 |

```bash
python3 scripts/profile_volume_trend_v2.py --stocks 500 --bars 245
python3 scripts/profile_volume_trend_v2.py --stocks 200 --cprofile
pytest tests/test_etl/test_vector_append.py::test_volume_trend_last_bar_matches_expanding_series -v
```

### P2+ batch FULL 算域 + MACD B4 fast（2026-06-17）

| 项 | 值 |
|----|-----|
| 计划 | `docs/superpowers/plans/2026-06-17-batch-full-compute-domain-optimization.md` |
| M1 运维 | ✅ `cli ops spec-status` + `calc --refresh-spec --dry-run` + `v_dq_spec_freshness` |
| M2 Volume FULL | ✅ `batch_full_volume` → `trend_target_indices=resolve_compute_indices(...)` |
| M3 MACD B4 FULL | ✅ `b4_weekly_series_from_daily_fast` + `CALC_B4_WEEKLY_FAST=1` + `batch_full_macd` 写窗 |
| pytest Q1–Q4 | ✅ `test_b4_macd_weekly_append` / `test_batch_full_compute_domain` / `test_append_calc` |
| profile Q5/Q6 | MACD 100×245 **10.9×**（`profile_macd_b4_weekly --mode fast`）；Volume 写窗 **~188×** |
| 实库 E1–E3 | ✅ M5 pilot @ 20260616（见 `evidence/2026-06-17-m5-pilot/`）；Pipeline 终验见附录 F |

```bash
python3 scripts/profile_macd_b4_weekly.py --stocks 100 --bars 245 --mode fast_write_window
pytest tests/test_etl/test_b4_macd_weekly_append.py tests/test_etl/test_batch_full_compute_domain.py -v
```

---

## 附录 C — DDE 周线尾窗「假卡死」优化（✅ 2026-06-11）

### 优化前现象

| 步骤 | 墙钟 | 日志特征 |
|------|------|----------|
| quote_daily / quote_weekly / dde_daily | ~30s 合计 | `log_timed_step` 有 done |
| **`dde_weekly`** | **~483–555s** | `loading dde_weekly` 后 **8–9min 零输出** → `dde_weekly done` |

### 根因

1. **观测：** `log_timed_step` 包住整次 batch，14 chunk 期间无中间日志。
2. **计算：** 全历史周聚合后再 `ROW_NUMBER<=245`；`dim_date` 区间 join 行膨胀。

### 实施（P0+P1，方案 B）

| 项 | 改动 | 验收 |
|----|------|------|
| P0 | `_load_weekly_batch` 每 chunk `progress calc.dde_weekly: i/N chunks` | grep 可见 14 步进度 |
| P1a | `tail_window` 时 `recent_weeks` 只扫 **245+1** 周再聚合 | 等价测试 PASS |
| P1b | `expected_days` 改标量子查询，去掉 `dim_date` 笛卡尔 join | `_load_weekly` 同步 |
| P2 | `--skip-dde-weekly` | 未做（非刚需） |

### 实测（2026-06-11，5524 股）

```
batch_load_dde_tails(weekly)  37.9s   （优化前 ~520s，约 14×）
```

同日复跑 `dde_weekly` 步骤预期从 ~8min 降至 **~40s**；`run --skip-export` 墙钟有望从 ~18.6min 降至 **~11min**（待复测）。

### 涉及模块

- `backend/etl/calc_dde.py` — `_load_weekly` / `_load_weekly_batch`
- 调用方不变：`calc_fast_skip.batch_load_dde_tails` → `calc.batch_tails` / `refresh_state` / `calc.chunk_tails`

---

## 附录 D — P4 Calc Chunk 立项（2026-06-12，用户审批）

### 与用户四条硬约束的关系

| # | 要求 | 稳态 30min | P4 角色 |
|---|------|-----------|---------|
| 1 | 整条链路 ≤30min（含 export） | **M1–M3 后已可达**（20260610 无 export 16min + export ~2min） | P4 **非 30min 前提**；降 calc 余量、迁移日保险 |
| 2 | 数据质量 | health_check + golden | P4 **禁止**弱签名/改公式 |
| 3 | 无不必要计算 | DWD 增量已落地 | **P4 核心**：Task 5/5b 指标级调度 + Batch FULL |
| 4 | 不全库 rebuild | stale 子集已落地 | P4 不引入全库 rebuild |

### 立项依据

| 门禁 | 阈值 | 实测（20260610） | 触发 |
|------|------|-----------------|------|
| 端到端墙钟（无 export） | >1500s | 977s | — |
| chunk_stocks | >400 | **5003** | ✅ |
| calc 阶段 | >300s | **598s** | ✅ |

### 目标（稳态 vs 迁移）

| 指标 | 迁移日（20260610） | 稳态 P4 目标 |
|------|-------------------|-------------|
| 整条链路含 export | ~18min（推算） | **≤30min**（合同） |
| calc_dws | 598s | **≤300s** |
| chunk_stocks | 5003 | **<400**（**仅稳态门禁**） |
| chunk_work_items | — | 可观测（新增） |

### 实施计划映射（附录 D ↔ 主计划 Phase）

| 附录 D 优先级 | 主计划 Phase / Task | 状态 |
|--------------|---------------------|------|
| ~~P0 calc_gate~~ | Phase 0 Task 0–1 | ✅ 已 ship |
| ~~P0 backfill-state~~ | Phase 1 Task 2 | ✅ 已 ship |
| **P0 Task 5** | Phase 2 Task 5 — 指标级 chunk worker | ⏳ **首个实施** |
| **P0 Task 5b** | Phase 2 Task 5b — Batch FULL（新增） | ⏳ 待做 |
| P1 | Phase 3 Task 7 — 物化尾窗 | 未开始 |
| P2 | Phase 4 Task 8–9 — 向量化 APPEND | 未开始 |
| P2 | Phase 5 Task 10 — fetch 解耦 | 未开始 |

**子计划（Task 5 + 5b）：** `docs/superpowers/plans/2026-06-12-p4-indicator-chunk-impl.md`

### 验收门禁（双档，2026-06-12 审批）

**稳态真新日：**

```bash
python3 scripts/benchmark_run.py --date $ODS_MAX --run    # 含 export，墙钟 ≤1800s
python3 scripts/health_check.py
# ods_etl_log: chunk_stocks < 400, calc_dws ≤ 300s
pytest tests/ -v
pytest tests/test_etl/test_append_calc.py -v
```

**迁移日（SIGNATURE/SPEC 变更）：**

```bash
python3 scripts/benchmark_run.py --date $DATE --run
python3 scripts/health_check.py
# 记录 full_by_indicator；L3 spot-check（50 股 × 受影响指标，atol=1e-9）
# 不卡 chunk_stocks
```

### Migration playbook（算法变更日）

1. 变更前：`cp data/tradeanalysis.duckdb data/tradeanalysis.pre-YYYYMMDD.duckdb`
2. 预期 mass FULL → 日志记录 `full_by_indicator`，**不视为 SLA 失败**
3. 跑后：受影响指标 L3 spot-check（新旧 `calc_date` 同 `trade_date`）
4. 可选：`backfill-state` 仅当缺 state，非签名迁移必选

### 状态

| 项 | 值 |
|----|-----|
| 立项日 | 2026-06-12 |
| 用户审批 | 2026-06-12（四条约束 SLA 合同） |
| 首个 Task | **Phase 2 Task 5**（非 Phase 0） |
| M6 里程碑 | **进行中** |

**Plan approved. P4 execution starts at Task 5.**

---

## 附录 E5 — Calc 路由双指纹 + chunk 修复（2026-06-14，✅ 代码落地）

**Plan：** `docs/superpowers/plans/2026-06-14-calc-routing-dual-fp-export-opt.md`

**锚点 run：** `7c090546`（calc_date=20260612，0 calculated，Step2 ~515s）

| 包 | 改动 | E4 签字指标 |
|----|------|------------|
| Calc-R1 | `_compute_chunk_codes` 收尾重算 | `chunk_stocks=0`（同日复跑）；`fallthrough=0` |
| Calc-R2 | `CALC_DWD_FP_GATE` + preflight 门 + batch_full state 对齐 | `full_by_indicator.dde_weekly=0`；`batch_full_items=0` |
| Calc-O1 | 热路径 cold merge 进度 + `cold_merge_*` 审计 | grep `calc.batch_preflight` 可见 |

**实库复验（✅ 2026-06-17，run_id `ded70a43`）：**

```bash
python3 -m backend.cli run --date 20260612 --skip-export
python3 scripts/health_check.py
```

| 指标 | 期望 | 实测 |
|------|------|------|
| chunk_stocks | ≈0 | **0** |
| batch_full_items | 小量 DDE 运维 | **118**（dde 日+周窄窗） |
| batch_only | 全市场 APPEND/SKIP | **5391** |
| Step2 墙钟 | <120s（同日复跑） | **~256s**（含 refresh_state + batch_append 138s） |
| health_check | PASS | ✅ |

**运维一次性（B4 元数据回填后）：** `python -m backend.cli refresh-state --date 20260612`

**Export-E1**（building sheets 119s）→ 独立 plan，不在本附录。

---

## 附录 F — Pipeline 30min 终验签字（2026-06-17）

**分支：** `perf/b2-polyfit-vectorization` @ `e556538`（含 PR #14 spec-gate hotfix）  
**DB 备份：** `data/tradeanalysis.pre-pipeline-signoff-20260617.duckdb`  
**证据目录：** `docs/superpowers/plans/evidence/2026-06-17-pipeline-signoff/`

### F.1 稳态真新日（20260615 首次 `cli run`）

| 阶段 | 墙钟 | 判定 |
|------|------|------|
| run_fetch | 2.9s | ✅ |
| run_rebuild_dwd | 27.0s | ✅ |
| run_refresh_state | 305.4s | ⚠️ 占 62%；后续优化项 |
| calc_dws（batch APPEND 路径） | 0.1s log + preflight 40s | ✅ chunk=0 |
| run_export | 155.4s | ✅ |
| **合计（logged steps）** | **~491s (~8.2min)** | **✅ SLA** |

### F.2 同日复跑 + benchmark（20260616，`benchmark_run --run`）

| 检查项 | 值 | 判定 |
|--------|-----|------|
| 墙钟（live run） | **145.3s** | ✅（L0 pipeline_shortcut + export） |
| run_export | 136.4s | ✅ |
| chunk_stocks | 0 | ✅ |
| health_check | PASS（Section J/K） | ✅ |
| PR #14 spec-gate | merged → default branch | ✅ |

### F.3 真新日 S5（20260617）

| 项 | 结果 |
|----|------|
| 命令 | `python3 -m backend.cli run --date 20260617` |
| 结果 | **阻塞** — tushare fetch 返回 0 行（`ods_max=20260616`）；`calc_date > ods_max` 门禁拒绝 |
| 后续 | 下一交易日 ODS 就绪后复跑；F.1 已覆盖稳态真新日 SLA |

### F.4 四条硬约束终判

| # | 约束 | 判定 | 证据 |
|---|------|------|------|
| 1 | 整条链路 ≤30min（含 export） | **✅ PASS** | F.1 ~491s；F.2 145s |
| 2 | 数据质量 | **✅ PASS** | health_check 2026-06-17 |
| 3 | 不做不必要计算 | **✅ PASS** | E5 chunk=0；日志 `mode=week=` |
| 4 | 不全库 rebuild | **✅ PASS** | stale 子集；无 `stocks=all` |

**Plan status：** **M4 签字完成**（2026-06-17）。同日复跑 ≤60s 仍为辅助 KPI（`refresh_state` ~300–390s 为主因）。
