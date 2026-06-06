# 导出数据质量修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复周线量能全空、sideways 宽表漏映射、导出 `-`/`N/A` 语义混淆三类数据质量问题。

**Architecture:** 双轨 warmup（日线 250 日 + 周线 120 周-end bar）驱动 fetch/calc 门禁；`calc_volume` 公式不变；ADS 补 sideways CASE；Export 层事件/状态/基本面三分法。

**Tech Stack:** Python 3.9+, DuckDB, pytest, openpyxl

**上游设计：** [`2026-06-06-export-data-quality-fix.md`](2026-06-06-export-data-quality-fix.md)

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `backend/db/schema.py` | 4 处 `ma_alignment` CASE 补 `sideways` |
| `backend/etl/orchestrator.py` | `WEEKLY_WARMUP_WEEKS`、`resolve_weekly_warmup_start`、双维 `check_data_completeness`、`_compute_fetch_range` 扩历史 |
| `backend/export_wide.py` | 列 taxonomy + `-`/`N/A` 填充 |
| `scripts/health_check.py` | Section I 周线 volume 填充率 |
| `tests/test_schema.py` | sideways 宽表映射 |
| `tests/test_etl/test_orchestrator.py` | warmup / completeness 单测 |
| `tests/test_export_wide.py` | 三分法单测 |
| `tests/test_etl/test_calc_volume.py` | 130 week-end bar → pct_rank 非 NaN |
| `CLAUDE.md` | 双轨 warmup + 导出语义 |

**不改动：** `backend/etl/calc_volume.py` 计算逻辑

---

### Task 1: sideways 宽表映射（Batch 0）

**Files:**
- Modify: `backend/db/schema.py:610-621, 710-721, 783-794, ~848-859`
- Test: `tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_schema.py` 末尾新增：

```python
def test_wide_view_maps_sideways_alignment(db_with_schema):
    """DWS alignment=sideways 必须在 ADS 宽表可见，不能 ELSE NULL。"""
    from backend.db.schema import _ADS_WIDE_VIEWS_DDL

    for sql in _ADS_WIDE_VIEWS_DDL:
        db_with_schema.execute(sql)

    db_with_schema.execute("""
        INSERT INTO dwd_daily_quote (ts_code, trade_date, close_qfq, is_suspended)
        VALUES ('000001.SZ', '20260102', 10.0, 0)
    """)
    db_with_schema.execute("""
        INSERT INTO dws_ma_daily (ts_code, trade_date, calc_date, alignment)
        VALUES ('000001.SZ', '20260102', '20260102', 'sideways')
    """)

    row = db_with_schema.execute("""
        SELECT ma_alignment FROM v_ads_analysis_wide_daily
        WHERE ts_code = '000001.SZ' AND trade_date = '20260102'
    """).fetchone()

    assert row is not None
    assert row[0] is not None
    assert "走平" in row[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_schema.py::test_wide_view_maps_sideways_alignment -v`

Expected: FAIL — `row[0]` is `None`

- [ ] **Step 3: Add sideways to all 4 CASE blocks**

在 `backend/db/schema.py` 的 4 处 `WHEN 'tangle'` **之前**插入（daily/weekly/index/index_weekly）：

```sql
            WHEN 'sideways'      THEN '均线走平 — 双斜率近零，方向待定'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_schema.py::test_wide_view_maps_sideways_alignment -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/db/schema.py tests/test_schema.py
git commit -m "fix: map ma sideways alignment in ADS wide views"
```

---

### Task 2: resolve_weekly_warmup_start

**Files:**
- Modify: `backend/etl/orchestrator.py:202-203`
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_etl/test_orchestrator.py` 的 `check_data_completeness` 段之前插入：

```python
def test_resolve_weekly_warmup_start_counts_week_ends():
    """第 120 个 is_week_end=1 交易日即为 weekly warmup 起点。"""
    import duckdb
    from backend.etl.orchestrator import resolve_weekly_warmup_start, WEEKLY_WARMUP_WEEKS

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY,
            is_trade_day INTEGER,
            is_week_end INTEGER
        )
    """)
    # 130 个 week-end，日期递减插入
    for i in range(130):
        d = f"2025{100 - i:03d}01"  # 简化：仅测试 ROW_NUMBER 逻辑
        con.execute(
            "INSERT INTO dim_date VALUES (?, 1, 1)", [d]
        )
    # 补非 week-end 日
    con.execute(
        "INSERT INTO dim_date VALUES ('20260101', 1, 0)"
    )

    start = resolve_weekly_warmup_start(con, "20250001", WEEKLY_WARMUP_WEEKS)
    assert start == "20250011"  # 第 120 个 week-end（130-i=11 when i=119）

    con.close()
```

**注意：** 上面日期是占位；实现时用真实递增日期更稳。推荐测试数据：

```python
    dates = []
    for w in range(130):
        # 每周一个 week-end：YYYYMMDD 递增
        dates.append(f"2020{1 + w // 52:02d}{(w % 52) * 7 + 5:02d}01")
    for d in dates:
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [d])
    con.execute("INSERT INTO dim_date VALUES (?, 1, 0)", [dates[-1]])

    start = resolve_weekly_warmup_start(con, dates[-1], 120)
    assert start == dates[130 - 120]  # index 10
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `pytest tests/test_etl/test_orchestrator.py::test_resolve_weekly_warmup_start_counts_week_ends -v`

Expected: FAIL — `ImportError: cannot import name 'resolve_weekly_warmup_start'`

- [ ] **Step 3: Implement constants + function**

在 `backend/etl/orchestrator.py` 的 `WARMUP_TDAYS` 下方：

```python
WEEKLY_WARMUP_WEEKS = 120  # volume weekly pct_rank/zone window (120 week-end bars)


def resolve_weekly_warmup_start(con, end_date: str,
                                n_weeks: int = WEEKLY_WARMUP_WEEKS):
    """Return trade_date of the n_weeks-th week-end bar looking back from end_date."""
    row = con.execute("""
        SELECT trade_date FROM (
            SELECT trade_date,
                   ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
            FROM dim_date
            WHERE is_trade_day = 1 AND is_week_end = 1 AND trade_date <= ?
        ) WHERE rn = ?
    """, [end_date, n_weeks]).fetchone()
    return row[0] if row else None
```

- [ ] **Step 4: Run test — expect PASS**

Run: `pytest tests/test_etl/test_orchestrator.py::test_resolve_weekly_warmup_start_counts_week_ends -v`

- [ ] **Step 5: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "feat: add resolve_weekly_warmup_start for 120-week volume window"
```

---

### Task 3: _needed_history_start + _compute_fetch_range 扩历史

**Files:**
- Modify: `backend/etl/orchestrator.py:361-413`
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
def test_compute_fetch_range_uses_weekly_warmup_when_deeper():
    """weekly 120 周起点早于 daily 250 日时，fetch 应从更早日期开始。"""
    import duckdb
    from backend.etl.orchestrator import _compute_fetch_range

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dim_stock (
            ts_code TEXT PRIMARY KEY, list_date TEXT, delist_date TEXT)
    """)
    con.execute("""
        CREATE TABLE dim_date (
            trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER)
    """)
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")

    # 600 个交易日 + 150 week-end（简化：每 4 日一个 week-end）
    tdays = []
    for i in range(600):
        td = f"2024{(i // 30) + 1:02d}{(i % 30) + 1:02d}"
        we = 1 if (i % 4 == 3) else 0
        con.execute(
            "INSERT OR REPLACE INTO dim_date VALUES (?, 1, ?)", [td, we]
        )
        tdays.append(td)
    end = tdays[-1]

    con.execute(
        "INSERT INTO dim_stock VALUES ('DEEP.SZ', '20200101', NULL)"
    )
    # ODS 只有最近 250 日
    for td in tdays[-250:]:
        con.execute(
            "INSERT INTO ods_daily VALUES ('DEEP.SZ', ?)", [td]
        )

    start, end_out = _compute_fetch_range(con, "DEEP.SZ", end)
    assert start is not None
    assert start < tdays[-250]  # 必须比 daily-only 250 起点更早

    con.close()
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `pytest tests/test_etl/test_orchestrator.py::test_compute_fetch_range_uses_weekly_warmup_when_deeper -v`

Expected: FAIL — `start >= tdays[-250]`

- [ ] **Step 3: Implement _needed_history_start**

在 `resolve_weekly_warmup_start` 之后添加：

```python
def _needed_history_start(con, end_date: str, list_date: str = None) -> Optional[str]:
    """Earliest trade_date required for daily+weekly warmup (more history = smaller date)."""
    daily_row = con.execute("""
        SELECT trade_date FROM (
            SELECT trade_date,
                   ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
            FROM dim_date WHERE is_trade_day = 1 AND trade_date <= ?
        ) WHERE rn = ?
    """, [end_date, WARMUP_TDAYS]).fetchone()
    daily_start = daily_row[0] if daily_row else None
    weekly_start = resolve_weekly_warmup_start(con, end_date)

    if daily_start and weekly_start:
        needed = min(daily_start, weekly_start)
    else:
        needed = daily_start or weekly_start
    if not needed:
        return None
    if list_date and list_date > needed:
        return list_date
    return needed
```

重写 `_compute_fetch_range` 步骤 3：用 `_needed_history_start(con, end_date, list_date)` 替代单一 `lookback_tdays` ROW_NUMBER。

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_etl/test_orchestrator.py::test_compute_fetch_range_uses_weekly_warmup_when_deeper tests/test_etl/test_orchestrator.py::test_compute_fetch_range_accepts_full_coverage -v`

Expected: PASS（既有测试仍绿）

- [ ] **Step 5: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "feat: extend fetch range for 120-week volume warmup"
```

---

### Task 4: check_data_completeness 双维度

**Files:**
- Modify: `backend/etl/orchestrator.py:205-250`
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
def test_check_data_completeness_requires_week_end_bars():
    """dwd_rows 够但 week_end_bars < 120 → missing（weekly_warmup）。"""
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
    con.execute("INSERT INTO dim_stock VALUES ('W.SZ', '20200101', NULL)")

    for i in range(260):
        td = f"2026{i//12:02d}{i%12+1:02d}"
        con.execute("INSERT INTO dwd_daily_quote VALUES ('W.SZ', ?)", [td])
    # 仅 50 个 week-end bar
    for i in range(50):
        td = f"2025{i+1:02d}05"
        con.execute("INSERT INTO dim_date VALUES (?, 1, 1)", [td])
        con.execute("INSERT INTO dwd_weekly_quote VALUES ('W.SZ', ?)", [td])

    result = check_data_completeness(con, ["W.SZ"], min_daily_rows=250)
    assert "W.SZ" in result["missing"]
    assert result["missing"]["W.SZ"]["week_end_bars"] == 50
    assert result["missing"]["W.SZ"]["reason"] == "weekly_warmup"

    con.close()
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `pytest tests/test_etl/test_orchestrator.py::test_check_data_completeness_requires_week_end_bars -v`

- [ ] **Step 3: Extend check_data_completeness**

```python
def _count_week_end_bars_batch(con, ts_codes: list[str]) -> dict:
    if not ts_codes:
        return {}
    ph = ",".join(["?"] * len(ts_codes))
    rows = con.execute(f"""
        SELECT w.ts_code, COUNT(*)
        FROM dwd_weekly_quote w
        JOIN dim_date d ON w.trade_date = d.trade_date AND d.is_week_end = 1
        WHERE w.ts_code IN ({ph})
        GROUP BY w.ts_code
    """, ts_codes).fetchall()
    return {r[0]: r[1] for r in rows}


def check_data_completeness(con, ts_codes: list[str],
                             min_daily_rows: int = WARMUP_TDAYS,
                             min_week_end_bars: int = WEEKLY_WARMUP_WEEKS) -> dict:
    ...
    week_end_counts = _count_week_end_bars_batch(con, ts_codes)

    for ts_code in ts_codes:
        info = dwd_data.get(ts_code)
        dwd_rows = info["dwd_rows"] if info else 0
        week_end_bars = week_end_counts.get(ts_code, 0)
        daily_ok = info is not None and dwd_rows >= min_daily_rows
        weekly_ok = week_end_bars >= min_week_end_bars

        if daily_ok and weekly_ok:
            ok.append(ts_code)
        else:
            if not daily_ok and not weekly_ok:
                reason = "both"
            elif not daily_ok:
                reason = "daily_warmup"
            else:
                reason = "weekly_warmup"
            missing[ts_code] = {
                "dwd_rows": dwd_rows,
                "week_end_bars": week_end_bars,
                "min_date": info["min_date"] if info else None,
                "max_date": info["max_date"] if info else None,
                "reason": reason,
            }
```

更新 `run_calc` 日志：`lack sufficient data` 时打印 `reason` 字段。

- [ ] **Step 4: Fix existing test if needed**

Run: `pytest tests/test_etl/test_orchestrator.py -v -k completeness`

更新 `test_check_data_completeness_batch_results`：无 `dwd_weekly_quote` 表时 week_end_bars=0 → 所有股 missing。测试需 CREATE dwd_weekly_quote + dim_date 或 mock `_count_week_end_bars_batch`。

**最小修复：** 在 `test_check_data_completeness_batch_results` 中为 A.SZ 插入 120+ week-end 行。

- [ ] **Step 5: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "feat: dual-dimension completeness check for weekly volume warmup"
```

---

### Task 5: volume weekly 集成测试（130 week-end → pct_rank）

**Files:**
- Test: `tests/test_etl/test_calc_volume.py`

- [ ] **Step 1: Write the failing test**

```python
def test_weekly_pct_vol_rank_with_130_week_end_bars():
    """130 根 week-end bar 时 pct_vol_rank 末行非 NaN。"""
    import numpy as np
    import pandas as pd
    from backend.etl.calc_volume import VolumeCalculator

    n = 130
    df = pd.DataFrame({
        "trade_date": [f"2020{(i//52)+1:02d}{(i%52)+1:02d}" for i in range(n)],
        "vol": np.random.uniform(1e6, 5e6, n),
        "close_qfq": np.linspace(10, 20, n),
    })
    calc = VolumeCalculator(con=None, freq="weekly")
    out = calc._compute_indicators(df)
    ranks = out["pct_vol_rank"].values
    assert np.isfinite(ranks[-1])
    assert out["zone"].iloc[-1] in ("normal", "explosive", "low_volume")
```

- [ ] **Step 2: Run — expect PASS already**（验证公式无需改，测试锁定行为）

Run: `pytest tests/test_etl/test_calc_volume.py::test_weekly_pct_vol_rank_with_130_week_end_bars -v`

若 PASS：测试作为回归护栏，直接 commit。

- [ ] **Step 3: Commit**

```bash
git add tests/test_etl/test_calc_volume.py
git commit -m "test: lock weekly volume pct_rank with 130 week-end bars"
```

---

### Task 6: Export 三分法（Batch 2）

**Files:**
- Modify: `backend/export_wide.py:86-90, 245-256, 279-295`
- Test: `tests/test_export_wide.py`

- [ ] **Step 1: Write the failing test**

```python
def test_export_display_null_semantics():
    """事件列 NULL→'-'；状态列 NULL→'N/A'；PE NULL→'N/A'。"""
    from backend.export_wide import apply_display_nulls
    import pandas as pd

    df = pd.DataFrame({
        "macd_divergence": [None],
        "pct_vol_rank": [None],
        "pe_ttm": [None],
        "ma_alignment": [None],
    })
    out = apply_display_nulls(df)
    assert out["macd_divergence"].iloc[0] == "-"
    assert out["pct_vol_rank"].iloc[0] == "N/A"
    assert out["pe_ttm"].iloc[0] == "N/A"
    assert out["ma_alignment"].iloc[0] == "N/A"
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_export_wide.py::test_export_display_null_semantics -v`

- [ ] **Step 3: Implement column taxonomy + helper**

在 `backend/export_wide.py` 替换 `_SIGNAL_COLS` 定义：

```python
_EVENT_SIGNAL_COLS = {
    "kpattern", "kpattern_strength",
    "macd_divergence", "macd_turning_point", "macd_alert",
    "ma_turning_point", "dde_alert", "dde_divergence",
    "vol_divergence", "vol_signal",
}
_STATE_METRIC_COLS = {
    "pct_vol_rank", "vol_zone", "ma_alignment",
    "macd_zone", "macd_trend", "macd_trend_strength",
    "dde_trend", "dde_trend_strength",
    "vol_trend", "vol_trend_strength", "volume_ratio",
    "price_position_60d", "price_position_120d", "price_position_250d",
}
_FUNDAMENTAL_NA_COLS = {"pe_ttm"}
# 兼容旧名
_SIGNAL_COLS = _EVENT_SIGNAL_COLS | _STATE_METRIC_COLS | _FUNDAMENTAL_NA_COLS


def apply_display_nulls(df: pd.DataFrame) -> pd.DataFrame:
    """Apply export display semantics before Chinese rename."""
    df = df.copy()
    for col in _EVENT_SIGNAL_COLS:
        if col in df.columns:
            df[col] = df[col].fillna("-")
        wcol = f"__w__{col}"
        if wcol in df.columns:
            df[wcol] = df[wcol].fillna("-")
    for col in _STATE_METRIC_COLS | _FUNDAMENTAL_NA_COLS:
        if col in df.columns:
            df[col] = df[col].fillna("N/A")
        wcol = f"__w__{col}"
        if wcol in df.columns:
            df[wcol] = df[wcol].fillna("N/A")
    return df
```

在 `_write_sheet_merged` 中，enum 翻译之后、写 cell 之前调用 `apply_display_nulls(df)`，**删除**原有 `_SIGNAL_COLS` fillna 循环。

- [ ] **Step 4: Run export tests**

Run: `pytest tests/test_export_wide.py -v`

Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add backend/export_wide.py tests/test_export_wide.py
git commit -m "feat: export three-way null display (- / N/A / value)"
```

---

### Task 7: health_check Section I

**Files:**
- Modify: `scripts/health_check.py:157-164`
- Test: 手动运行（无单测文件）

- [ ] **Step 1: Add Section I after Section H**

```python
    print("=== I. 周线 volume 状态指标 ===")
    c.info("volume_weekly pct_vol_rank 非空",
           "SELECT COUNT(*) FROM v_dws_volume_weekly_latest v "
           "JOIN dim_date d ON v.trade_date=d.trade_date AND d.is_week_end=1 "
           "WHERE v.pct_vol_rank IS NOT NULL")
    c.info("volume_weekly zone 非空",
           "SELECT COUNT(*) FROM v_dws_volume_weekly_latest v "
           "JOIN dim_date d ON v.trade_date=d.trade_date AND d.is_week_end=1 "
           "WHERE v.zone IS NOT NULL")
```

- [ ] **Step 2: Run health check**

Run: `python -m scripts.health_check`

Expected: Section I 输出 info 行（修复前 pct_vol_rank 可能为 0）

- [ ] **Step 3: Commit**

```bash
git add scripts/health_check.py
git commit -m "feat: health_check weekly volume fill rate info"
```

---

### Task 8: 文档更新

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`（§6.5 周线 120 周 + 导出语义）

- [ ] **Step 1: CLAUDE.md 追加**

在「Freshness 三门禁」或 warmup 段落添加：

```markdown
- **双轨 warmup：** 日线 `WARMUP_TDAYS=250`；周线 volume `WEEKLY_WARMUP_WEEKS=120`（week-end bar）。fetch/calc 门禁取两者较早起点。`check_data_completeness` 检查 `dwd_rows≥250` 且 `week_end_bars≥120`。
- **导出语义：** `-`=当日无事件信号；`N/A`=不可算或源端无数据（如亏损股 PE、历史不足量能分位）。
```

- [ ] **Step 2: spec §6.5 补一句**

> 周线 `dws_volume_weekly` 的 120 窗口指 **120 根 week-end bar**（约 2.5 年），非 120 个交易日。

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md
git commit -m "docs: dual warmup and export null semantics"
```

---

### Task 9: 全量测试 + 运维验收（人工）

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v`

Expected: ALL PASS

- [ ] **Step 2: 实库 schema 刷新**

Run: `python -c "from backend.db.connection import get_connection; from backend.db.schema import create_all_tables; c=get_connection(); create_all_tables(c)"`

- [ ] **Step 3: 补历史 + 重算（运维窗口，耗时数小时）**

```bash
python -m backend.cli fetch
python -m backend.cli calc --date 20260605
python -m scripts.health_check
python -m backend.cli export --date 20260605
```

- [ ] **Step 4: 验收 SQL**

```sql
SELECT COUNT(*) FILTER (WHERE pct_vol_rank IS NOT NULL),
       COUNT(*) FILTER (WHERE zone IS NOT NULL)
FROM v_dws_volume_weekly_latest
WHERE trade_date = (SELECT MAX(trade_date) FROM dim_date WHERE is_week_end=1);
-- 期望 >> 0（上市满 120 周的股票）
```

---

## Self-Review

| 检查项 | 结果 |
|--------|------|
| Spec: 分层 warmup A | Task 2-4 |
| Spec: sideways 映射 | Task 1 |
| Spec: 导出三分法 B | Task 6 |
| Spec: health_check | Task 7 |
| Spec: calc_volume 不改 | Task 5 仅测试 |
| 无 TBD/placeholder | PASS |
| 类型/命名一致 | `resolve_weekly_warmup_start`, `week_end_bars`, `apply_display_nulls` |

## GSTACK REVIEW REPORT

Quality score: 9/10 — 可执行 TDD 步骤齐全，运维验收独立 Task 9。
