# 日终选股产品对齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每日 `cli run` 产出全市场最新交易日 + 周线 `__w__` 列 Excel；B4 **12 列硬门禁**（不含 `ma_alignment`）通过后冻结 G2；稳态完整 `run` ≤30min；`health_check` 软告警接入 `run`。

**Architecture:** 轨 A（验收）与轨 B（性能）并行起步：先建 `b4_gate` 脚手架 + weekly APPEND 等价测试；用 `diff_vs_123` 对齐 123 后冻结 `golden_{date}.csv`；golden 冻结后才认 30min KPI。存储收敛 Phase 2（250td / 250 week-end tail）放在稳态运维后可选落地。DWD/ODS 保持长历史；DWS 先 `prune --keep 1` 运维等价。

**Tech Stack:** Python 3.9、DuckDB、pytest、openpyxl（仅导出验收）、环境变量 `REF_123_DUCKDB_PATH`（123 参照库，过渡期）。

**上游 Spec（已审批 2026-06-09）：** `docs/superpowers/specs/2026-06-09-daily-screening-product-alignment.md`

**P3 闸门：** 闸门1 B4 硬门禁 12 列 mismatch≈0 → 闸门2 golden 冻结 → 闸门3 30min（golden 前不认 KPI）→ 闸门4 删 123。

**2026-06-09 决策（`ma_alignment`）：** 保留 TA MA5/MA10 十值枚举；`ma_alignment` / `w_ma_alignment` 移出 123 硬比，归入软层。`b4_gate` 用 `B4_HARD_*`（12 列）做 diff/golden；`extract` 仍拉 14 列供 Excel。

---

## File Map

| 文件 | 职责 |
|------|------|
| `backend/b4_gate/columns.py` | B4 extract 14 列 + 硬门禁 12 列；123↔TA 映射 |
| `backend/b4_gate/enums.py` | 123 枚举 → TA 枚举归一化 |
| `backend/b4_gate/extract.py` | 从 TA / 123 DuckDB 抽取 B4 截面 |
| `backend/b4_gate/sample.py` | 加载 `sample_500.csv`、BSE bucket |
| `backend/b4_gate/diff.py` | 两表逐行 diff、mismatch 报告 |
| `scripts/diff_vs_123.py` | 过渡期 CLI：5 日 × 500 股 diff |
| `scripts/verify_b4_gate.py` | golden vs 实库；exit≠0 硬阻断 |
| `scripts/benchmark_run.py` | 墙钟模板 + daily/weekly APPEND 计数 |
| `tests/fixtures/b4_gate/dates.txt` | 5 代表日（B4 通过后写入） |
| `tests/fixtures/b4_gate/sample_500.csv` | G2 分层 ~500 股 |
| `tests/fixtures/b4_gate/golden_{date}.csv` | 冻结 golden（12 列硬门禁 + week_end） |
| `tests/test_b4_gate_regression.py` | pytest 硬门禁 |
| `tests/test_etl/test_append_calc.py` | 补充 weekly APPEND≡FULL |
| `backend/cli.py` | `run` 末尾 health_check 软告警；可选 `--verify-b4` |
| `backend/db/dws_tail.py` | Phase 4 可选：按 bar 数 tail DELETE |
| `CLAUDE.md` | B4 / benchmark / 双轨 tail 命令 |
| `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` | v1.11 双轨 tail 注记 |

---

## Phase 0 — 脚手架（轨 A + 轨 B 并行）

### Task 1: B4 列与枚举映射模块

**Files:**
- Create: `backend/b4_gate/columns.py`
- Create: `backend/b4_gate/enums.py`
- Test: `tests/test_b4_gate_columns.py`

- [ ] **Step 1: Write the failing test**

```python
"""B4 column registry and 123 mapping."""
from backend.b4_gate.columns import B4_DAILY_FIELDS, B4_WEEKLY_FIELDS, map_123_daily_col


def test_b4_field_count():
    assert len(B4_DAILY_FIELDS) == 7
    assert len(B4_WEEKLY_FIELDS) == 7


def test_123_daily_macd_trend_maps_to_macd_trend():
    assert map_123_daily_col("short_macd_trend") == "macd_trend"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_b4_gate_columns.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

`backend/b4_gate/columns.py`:

```python
"""B4 hard-gate columns: daily 7 + weekly 7 (symmetric field names)."""
from typing import Dict, List

# Tradeanalysis DWS / v_ads 字段（raw 枚举，非 Excel 中文）
B4_DAILY_FIELDS: List[str] = [
    "macd_trend",
    "macd_zone",
    "macd_alert",
    "ma_alignment",
    "dde_trend",
    "dde_alert",
    "vol_trend",
]

B4_WEEKLY_FIELDS: List[str] = list(B4_DAILY_FIELDS)  # 同字段名，freq=weekly

B4_ALL_FIELDS: List[str] = B4_DAILY_FIELDS + [f"w_{f}" for f in B4_WEEKLY_FIELDS]

# 123 日线列名 → TA 字段
MAP_123_DAILY: Dict[str, str] = {
    "short_macd_trend": "macd_trend",
    "short_macd_signal": "macd_zone",
    "daily_rev_macd_hist_turn": "macd_alert",
    "short_ma_regime": "ma_alignment",
    "short_dde_trend": "dde_trend",
    "daily_rev_ddx2_slope_reversal": "dde_alert",
    "short_volume_trend": "vol_trend",
}

# 123 周线仍用 short_* / daily_rev_* 列名，按 freq=weekly 映射
MAP_123_WEEKLY: Dict[str, str] = dict(MAP_123_DAILY)


def map_123_daily_col(col_123: str) -> str:
    return MAP_123_DAILY[col_123]


def map_123_weekly_col(col_123: str) -> str:
    return MAP_123_WEEKLY[col_123]
```

`backend/b4_gate/enums.py`（节选，按 123 实库枚举补全）：

```python
"""Normalize 123 string labels to Tradeanalysis DWS enum values."""
from typing import Optional

# 123 short_macd_trend 示例 → TA macd_trend
MACD_TREND_123_TO_TA = {
    "上升": "up",
    "下降": "down",
    "走平": "flat",
    # 英文/数字码按 diff 首轮 mismatch 补全
}

MA_ALIGNMENT_123_TO_TA = {
    # 123 short_ma_regime → TA ma_alignment (8+sideways+tangle)
    # 首轮 diff 后从 mismatch 样本回填
}


def normalize_value(field: str, raw: Optional[str], source: str) -> Optional[str]:
    if raw is None or raw == "" or raw == "-":
        return None
    if source == "ta":
        return raw
    table = {
        "macd_trend": MACD_TREND_123_TO_TA,
        "ma_alignment": MA_ALIGNMENT_123_TO_TA,
    }.get(field, {})
    return table.get(raw, raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_b4_gate_columns.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/b4_gate/columns.py backend/b4_gate/enums.py tests/test_b4_gate_columns.py
git commit -m "feat(b4): add B4 column registry and 123 field mapping"
```

---

### Task 2: G2 分层抽样 `sample_500.csv`

**Files:**
- Create: `tests/fixtures/b4_gate/sample_500.csv`
- Create: `backend/b4_gate/sample.py`
- Test: `tests/test_b4_gate_sample.py`

- [ ] **Step 1: Write the failing test**

```python
from backend.b4_gate.sample import load_sample, SampleRow


def test_load_sample_has_buckets():
    rows = load_sample()
    assert 450 <= len(rows) <= 550
    buckets = {r.bucket for r in rows}
    assert "bse" in buckets
    assert "main_mature" in buckets
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `pytest tests/test_b4_gate_sample.py -v`

- [ ] **Step 3: 生成 sample_500.csv**

从实库 `dim_stock` + `dwd_daily_quote` 分层抽样（脚本或一次性 SQL），列：

`ts_code,bucket,note`

桶与 spec §1.5 对齐：main_mature ~200、chinext ~100、star ~80、young ~50、active ~50、bse ~20。

`backend/b4_gate/sample.py`:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import List

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests/fixtures/b4_gate"
SAMPLE_CSV = FIXTURE_DIR / "sample_500.csv"


@dataclass
class SampleRow:
    ts_code: str
    bucket: str
    note: str = ""


def load_sample() -> List[SampleRow]:
    import csv
    rows: List[SampleRow] = []
    with SAMPLE_CSV.open() as f:
        for row in csv.DictReader(f):
            rows.append(SampleRow(row["ts_code"], row["bucket"], row.get("note", "")))
    return rows


def is_bse(ts_code: str) -> bool:
    return ts_code.endswith(".BJ")


def skip_dde_compare(ts_code: str, bucket: str) -> bool:
    """BSE 或 bse bucket：DDE 不与 123 比。"""
    return is_bse(ts_code) or bucket == "bse"
```

- [ ] **Step 4: Run test — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/b4_gate/sample_500.csv backend/b4_gate/sample.py tests/test_b4_gate_sample.py
git commit -m "feat(b4): add G2 stratified sample_500 fixture"
```

---

### Task 3: B4 截面抽取（TA + 123）

**Files:**
- Create: `backend/b4_gate/extract.py`
- Test: `tests/test_b4_gate_extract.py`

- [ ] **Step 1: Write the failing test**（用 `tests/conftest.py` 内存库或小型 fixture DB）

```python
import duckdb
from backend.b4_gate.extract import extract_ta_b4, resolve_week_end


def test_resolve_week_end_before_analysis_date():
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE dim_date(trade_date VARCHAR, is_trade_day INT, is_week_end INT);
        INSERT INTO dim_date VALUES
          ('20260603',1,0),('20260604',1,0),('20260605',1,1);
    """)
    assert resolve_week_end(con, "20260604") == "20260603"  # 上周五
    assert resolve_week_end(con, "20260605") == "20260605"


def test_extract_ta_b4_returns_14_columns():
    # 使用 conftest 已建 schema 的 temp_db；至少断言列齐全
    from tests.conftest import ...  # 按项目 fixture 调整
    rows = extract_ta_b4(con, "20260605", ["000001.SZ"])
    assert "trade_date" in rows.columns
    assert "week_end" in rows.columns
    for f in ["macd_trend", "w_macd_trend"]:
        assert f in rows.columns
```

- [ ] **Step 2: Run test — expect FAIL**

- [ ] **Step 3: Implement extract**

核心 SQL（日线 B4 来自 DWS latest，**不用** `v_ads` 的中文 `ma_alignment`）：

```python
import pandas as pd
import duckdb

from backend.b4_gate.columns import B4_DAILY_FIELDS, B4_WEEKLY_FIELDS


def resolve_week_end(con: duckdb.DuckDBPyConnection, analysis_date: str) -> str | None:
    row = con.execute("""
        SELECT MAX(trade_date) FROM dim_date
        WHERE is_trade_day = 1 AND is_week_end = 1 AND trade_date <= ?
    """, [analysis_date]).fetchone()
    return row[0] if row else None


def extract_ta_b4(
    con: duckdb.DuckDBPyConnection,
    analysis_date: str,
    ts_codes: list[str],
) -> pd.DataFrame:
    if not ts_codes:
        return pd.DataFrame()
    ph = ",".join(["?"] * len(ts_codes))
    week_end = resolve_week_end(con, analysis_date)
  # 日线：JOIN 6 张 v_dws_*_daily_latest 取 B4 列
    daily_sql = f"""
        SELECT q.ts_code, q.trade_date,
               m.trend AS macd_trend, m.zone AS macd_zone, m.alert AS macd_alert,
               a.alignment AS ma_alignment,
               d.trend AS dde_trend, d.alert AS dde_alert,
               v.trend AS vol_trend
        FROM dwd_daily_quote q
        LEFT JOIN v_dws_macd_daily_latest m
          ON q.ts_code=m.ts_code AND q.trade_date=m.trade_date
        LEFT JOIN v_dws_ma_daily_latest a
          ON q.ts_code=a.ts_code AND q.trade_date=a.trade_date
        LEFT JOIN v_dws_dde_daily_latest d
          ON q.ts_code=d.ts_code AND q.trade_date=d.trade_date
        LEFT JOIN v_dws_volume_daily_latest v
          ON q.ts_code=v.ts_code AND q.trade_date=v.trade_date
        WHERE q.trade_date = ? AND q.ts_code IN ({ph})
          AND q.is_suspended = 0
    """
    daily = con.execute(daily_sql, [analysis_date] + ts_codes).df()
    if week_end:
        weekly_sql = f"""
            SELECT qw.ts_code,
                   m.trend AS w_macd_trend, m.zone AS w_macd_zone, m.alert AS w_macd_alert,
                   a.alignment AS w_ma_alignment,
                   d.trend AS w_dde_trend, d.alert AS w_dde_alert,
                   v.trend AS w_vol_trend
            FROM dwd_weekly_quote qw
            JOIN dim_date dd ON qw.trade_date = dd.trade_date AND dd.is_week_end = 1
            LEFT JOIN v_dws_macd_weekly_latest m ...
            WHERE qw.trade_date = ? AND qw.ts_code IN ({ph})
        """
        weekly = con.execute(weekly_sql, [week_end] + ts_codes).df()
        out = daily.merge(weekly, on="ts_code", how="left")
        out["week_end"] = week_end
    else:
        out = daily.copy()
        out["week_end"] = None
    return out
```

`extract_123_b4(con_123, analysis_date, ts_codes)`：读 123 库宽表或导出表，列名经 `MAP_123_*` 重命名后 `normalize_value`。

- [ ] **Step 4: Run test — expect PASS**

- [ ] **Step 5: Commit**

---

### Task 4: `diff_vs_123.py` CLI

**Files:**
- Create: `backend/b4_gate/diff.py`
- Create: `scripts/diff_vs_123.py`
- Test: `tests/test_b4_gate_diff.py`

- [ ] **Step 1: Write failing unit test for diff engine**

```python
import pandas as pd
from backend.b4_gate.diff import diff_b4_frames


def test_diff_b4_reports_mismatch():
    ta = pd.DataFrame({
        "ts_code": ["A.SZ"], "macd_trend": ["up"],
        "w_macd_trend": ["down"],
    })
    ref = ta.copy()
    ref.loc[0, "macd_trend"] = "down"
    mismatches = diff_b4_frames(ta, ref, skip_dde_ts=set())
    assert len(mismatches) == 1
    assert mismatches[0]["field"] == "macd_trend"
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement diff + CLI**

`scripts/diff_vs_123.py` 用法：

```bash
export REF_123_DUCKDB_PATH=/path/to/project123.duckdb
python -m scripts.diff_vs_123 --date 20260605 \
  --sample tests/fixtures/b4_gate/sample_500.csv \
  --out exports/b4_diff_20260605.csv
```

输出：mismatch 行 + 汇总按 field/bucket 计数。BSE DDE 列自动 `skip_dde_compare`。

- [ ] **Step 4: Run unit test — PASS**

- [ ] **Step 5: Commit**

---

### Task 5: `verify_b4_gate.py` + pytest 骨架

**Files:**
- Create: `scripts/verify_b4_gate.py`
- Create: `tests/test_b4_gate_regression.py`
- Create: `tests/fixtures/b4_gate/dates.txt`（占位，B4 通过前可为空或 1 个 smoke 日）

- [ ] **Step 1: Write failing pytest**

```python
"""Golden regression — skipped until golden files exist."""
import pytest
from pathlib import Path

GOLDEN_DIR = Path("tests/fixtures/b4_gate")


@pytest.mark.skipif(
    not (GOLDEN_DIR / "dates.txt").read_text().strip(),
    reason="golden not frozen",
)
def test_b4_golden_matches_db(temp_db):
    from backend.b4_gate.verify import verify_all_dates
    failures = verify_all_dates(temp_db)
    assert failures == []
```

- [ ] **Step 2: Run — SKIP or FAIL**

- [ ] **Step 3: Implement verify**

`scripts/verify_b4_gate.py`：读 `dates.txt` 每个 date + `golden_{date}.csv`，`extract_ta_b4` 对比 14 列；mismatch 打印并 `sys.exit(1)`。

Golden CSV 列：`ts_code,trade_date,week_end` + 7 daily + 7 weekly（`w_*` 前缀与 extract 一致）。

- [ ] **Step 4: pytest 在无 golden 时 SKIP — PASS**

- [ ] **Step 5: Commit**

---

### Task 6: Weekly APPEND≡FULL 等价测试

**Files:**
- Modify: `tests/test_etl/test_append_calc.py`
- Reference: 现有 daily 测试（PP/MA/KPattern/MACD/DDE/Volume）

- [ ] **Step 1: Write failing weekly test（以 PP 为例）**

```python
def test_pp_append_matches_full_on_new_bar_weekly():
  df = _make_df(300)
  calc = PricePositionCalculator(None, "weekly")
  full_df = calc._compute_positions(df.copy())
  last_date = df.iloc[-1]["trade_date"]
  append_df = calc._compute_positions_append(df.copy(), new_bars=[last_date])
  ...
```

- [ ] **Step 2: Run — 若已通过则补其余 5 计算器 weekly 用例**

Run: `pytest tests/test_etl/test_append_calc.py -k weekly -v`

- [ ] **Step 3: 为 MACD/DDE/Volume 补 weekly 合成帧**（DDE 用 `_make_moneyflow_df`）

每个计算器至少 1 个 `test_*_weekly_append_matches_full`；`atol=1e-9`。

- [ ] **Step 4: Full file PASS**

Run: `pytest tests/test_etl/test_append_calc.py -v`

- [ ] **Step 5: Commit**

```bash
git commit -m "test(calc): lock weekly APPEND equivalence for all calculators"
```

---

### Task 7: 墙钟 benchmark 脚本

**Files:**
- Create: `scripts/benchmark_run.py`

- [ ] **Step 1: 实现只读汇总**（不改 `run`）

从 `ods_etl_log` 读最近一次 `run_fetch` / `run_rebuild_dwd` / `calc` / `run_export` 耗时；从 calc 日志或 `ods_etl_log.data_completeness` 解析 daily/weekly APPEND 计数（若无字段，在 Task 10 补 orchestrator 写入）。

```bash
python -m scripts.benchmark_run --date 20260605
```

输出示例：

```
run_fetch: 45.2s
run_rebuild_dwd: 12.1s (skipped=false)
calc_total: 480.0s
  batch_append daily APPEND keys: ...
  batch_append weekly APPEND keys: ...
run_export: 35.0s
TOTAL wall: 572.3s  (SLA target <= 1800s)
```

- [ ] **Step 2: 手动跑一轮 `cli run` 后验证脚本可读**

- [ ] **Step 3: Commit**

---

## Phase 1 — B4 对齐（闸门 1，轨 A）

**人工循环 + 代码修复；未通过不得冻结 golden。**

### Task 8: 选定 5 代表日并首轮 diff

**Files:**
- Modify: `tests/fixtures/b4_gate/dates.txt`（候选，通过后锁定）
- Modify: `backend/b4_gate/enums.py`（按 mismatch 补枚举）

- [ ] **Step 1: 候选日类型**（写入 plan 备注，首次 B4 通过时定稿）

| 类型 | 选取规则 |
|------|----------|
| 最近稳态日 | `MAX(ods_daily.trade_date)` |
| 跨年附近 | 元旦前后 week_end |
| 长假后首日 | 春节后首个交易日 |
| 下跌段 | 指数大跌周中一日 |
| week_end 日 | 分析日 `is_week_end=1` |

- [ ] **Step 2: 对每个候选日跑 diff**

```bash
for d in 20251231 20260102 ...; do
  python -m scripts.diff_vs_123 --date $d --out exports/b4_diff_$d.csv
done
```

- [ ] **Step 3: 按 field × bucket 修算法或 enums**

常见修复点（按历史经验排序）：
- ~~`ma_alignment`~~：**已决策移出硬门禁**（TA 自研 vs 123 `ma_regime` 算法不同）
- `vol_trend` / `w_vol_trend`：量能趋势枚举与回归窗口。**2026-06-10：** 日线/周线 mismatch 根因均为 v2 算法已对齐但 DWS 存 v1 旧 trend（指纹 skip）；`VolumeCalculator.SPEC_VERSION=v2` + 专项重算后 `sample_500@20260609` 日线 `vol_trend` **123→0**、周线 `w_vol_trend` **23→0**（weekly 用 `vol*active_days/5` 反归一化）；总量 **1025→879**
- `macd_alert` / `dde_alert`（B4 硬门禁）：**2026-06-10** 对齐 123 `trend_reversal_signals` — MACD `backend/etl/b4_alerts.compute_macd_hist_turn_alerts`（3 柱拐点，无 flat）；DDE `compute_ddx2_slope_alerts`（DDX2 相邻 5 窗 polyfit 斜率拐点，`eps=0`）。`MACDCalculator`/`DDECalculator` `SPEC_VERSION=v2` + sample_500 专项重算后 `sample_500@20260609` 总量 **879→791**；alert 子集 **543→498**（日线 `macd_alert` **130→57**；剩余多为 123 batch `NO_DATA` vs TA 有 DDX2、或周线 MACD 柱序列微差导致拐点方向相反）
- `dde_alert` / `w_dde_alert`：DDX2 斜率反转 vs 结构法（仍在 B4 列）
- `macd_alert` / `w_macd_alert`：柱线拐头警惕
- `macd_zone` / `w_macd_zone`：区域迟滞与 123 映射
- 周线：确认 `week_end` 锚点与 123 weekly 频率一致

BSE：仅比 6 列（跳过 `dde_trend`/`dde_alert` 日周）。

- [ ] **Step 4: 5 日 × 500 股硬门禁 mismatch 总数 ≈ 0**（不含 `ma_alignment`）

验收命令：

```bash
python -m scripts.diff_vs_123 --dates-file tests/fixtures/b4_gate/dates.txt --summary
```

Expected: `mismatches: 0`（或仅文档允许的 bucket 跳过）

- [ ] **Step 5: Commit 算法修复 + enums**（多次 commit）

---

## Phase 2 — 冻结 G2（闸门 2）

### Task 9: 生成并锁定 golden CSV

**Files:**
- Create: `tests/fixtures/b4_gate/golden_{date}.csv` × 5
- Modify: `tests/fixtures/b4_gate/dates.txt`
- Modify: `tests/test_b4_gate_regression.py`（去掉 skip）

- [ ] **Step 1: 导出 golden**

```bash
python -m scripts.verify_b4_gate --export-golden \
  --dates-file tests/fixtures/b4_gate/dates.txt \
  --sample tests/fixtures/b4_gate/sample_500.csv
```

- [ ] **Step 2: 启用 pytest**

```bash
pytest tests/test_b4_gate_regression.py -v
```

Expected: PASS

- [ ] **Step 3: CI 接入**（`.github/workflows` 或本地 pre-release 脚本）

```bash
pytest tests/test_b4_gate_regression.py tests/test_etl/test_append_calc.py -v
```

- [ ] **Step 4: Commit golden fixtures**

```bash
git add tests/fixtures/b4_gate/
git commit -m "test(b4): freeze G2 golden 5×500 14-column gate"
```

- [ ] **Step 5: 123 标记只读**（运维：不再日常 diff）

---

## Phase 3 — 30min SLA（闸门 3，轨 B，**仅 golden 后**）

### Task 10: Calc 观测 daily/weekly APPEND 占比

**Files:**
- Modify: `backend/etl/orchestrator.py`（`log_etl_end` data_completeness）
- Modify: `scripts/benchmark_run.py`

- [ ] **Step 1: 在 `run_calc` 收尾写入**

```python
data_completeness={
    "append_daily": append_stats_daily,  # per (indicator) counts
    "append_weekly": append_stats_weekly,
    "skip_daily": ...,
    "skip_weekly": ...,
}
```

- [ ] **Step 2: benchmark 脚本解析并打印**

- [ ] **Step 3: 稳态新日 `run` ×3**

```bash
python -m backend.cli run --date YYYYMMDD --skip-export  # 测 fetch+dwd+calc
python -m scripts.benchmark_run --date YYYYMMDD
```

验收：
- 3 次 TOTAL wall ≤ **1800s**
- 至少 1 次 **week_end 日**、1 次 **非 week_end 日**
- weekly APPEND 占比：week_end 日 6 指标应主要为 APPEND；非周末 weekly SKIP 正常

- [ ] **Step 4: 未达标则性能专项**（已有手段，按瓶颈选）

| 瓶颈 | 动作 |
|------|------|
| fetch 拖尾 | 确认 `skip_stale_fetch` on run；G2 stale 子集 |
| calc FULL 风暴 | `backfill-state`；查 weekly state 是否被 repair 清空 |
| batch tail 245→250 | 评估 `SIG_WINDOW=250` 与 PP lookback 对齐（改后重跑 append 测试） |
| export | `--skip-export` 日常；Excel 单独 `export` |

- [ ] **Step 5: Commit 观测字段 + 文档记录 3 次墙钟**

---

### Task 11: 文档修正 `resolve_recalc_bars=291`

**Files:**
- Modify: `CLAUDE.md`（255 → **291**）
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`（若仍写 255）

- [ ] **Step 1: grep 全库 `255` recalc 表述并改为 291**

- [ ] **Step 2: Commit docs only**

---

## Phase 4 — 稳态运维（闸门 4）

### Task 12: `run` 接入 `health_check` 软告警

**Files:**
- Modify: `backend/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing test** — `cmd_run` 完成后调用 health_check 且不因 FAIL 中断 run

```python
def test_cmd_run_invokes_health_check_warning_only(monkeypatch):
    calls = []
    monkeypatch.setattr("scripts.health_check.run", lambda con: calls.append(1) and 2)
    ...
    cli.cmd_run(args)
    assert calls == [1]
```

- [ ] **Step 2: Implement**

```python
def _run_health_check_soft(db_path: str):
    import duckdb
    from scripts.health_check import run as health_run
    con = duckdb.connect(db_path, read_only=True)
    try:
        failures = health_run(con)
    finally:
        con.close()
    if failures:
        logger.warning("health_check: %d FAIL sections (run continues)", failures)
```

在 `cmd_run` 末尾、`logger.info("Done.")` 之前调用。**不** `sys.exit(1)`。

- [ ] **Step 3: 可选 `--verify-b4`** 调用 `verify_b4_gate` 硬阻断

- [ ] **Step 4: pytest PASS**

- [ ] **Step 5: Commit**

---

### Task 13: 运维 runbook + 删 123

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-06-09-daily-screening-product-alignment.md`（状态→已实施）

- [ ] **Step 1: CLAUDE 增加章节**

```bash
# B4（发布/改算法前）
pytest tests/test_b4_gate_regression.py -v
python -m scripts.verify_b4_gate

# 墙钟验收（golden 后）
python -m scripts.benchmark_run --date 20260605

# DWS 快照坍缩（Phase 1 存储策略）
python -m backend.cli prune --keep 1
```

- [ ] **Step 2: 确认 123 项目可归档删除**（人工，仓库外）

- [ ] **Step 3: Commit docs**

---

### Task 14（可选）: DWS 双轨 tail 存储收敛 Phase 2

**仅在 Phase 3 SLA 达标后启动。**

**Files:**
- Create: `backend/db/dws_tail.py`
- Modify: `backend/etl/base.py` `insert_dws_batch` 后 hook
- Test: `tests/test_db/test_dws_tail.py`

- [ ] **Step 1: Write failing test** — 插入 260 行后 tail prune 剩 250

- [ ] **Step 2: Implement `prune_dws_tail(con, freq, max_bars)`**

日线：`DELETE` 每 `(ts_code, calc_date=max)` 组内 `trade_date` 排序超出 250 的行。
周线：仅 `is_week_end=1` 的 `trade_date` 计数 250。

- [ ] **Step 3: 全市场 backfill 一次 + golden 重冻结**

- [ ] **Step 4: Commit**

---

## Self-Review（计划自检）

| Spec § | 任务 |
|--------|------|
| §1.3 B4 12 列硬门禁 | Task 1–5, 8–9 |
| §1.3.2 周线语义 | Task 3 extract week_end; Task 6 weekly tests |
| §1.4 B4 生命周期 | Task 4 diff vs 123; Task 9 freeze; Task 13 删 123 |
| §1.5 G2 500 股 | Task 2 |
| §4.1 health_check | Task 12 |
| §4.2 双轨 tail | Task 14 可选；Phase 1 用 prune --keep 1 |
| §5 P3 顺序 | Phase 0→1→2→3→4 |
| §3.2 weekly append 缺口 | Task 6 |
| §3.2 30min | Task 7, 10 |
| F3 硬/软门禁 | Task 5 verify 硬；Task 12 软 |

**Placeholder scan:** 无 TBD；enum 表首轮 diff 后增量补全（Task 8 明确步骤）。

**类型一致:** extract 周线列统一 `w_` 前缀；golden CSV 与 pytest 同 schema。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-09-daily-screening-impl.md`.

**两种执行方式：**

1. **Subagent-Driven（推荐）** — 每 Task 派生子 agent，Task 间人工 review  
2. **Inline Execution** — 本 session 用 executing-plans 按 Phase 批量执行并设检查点  

**建议起点:** Phase 0 Task 1（B4 列映射）与 Task 6（weekly append 测试）可并行。

**Which approach?**
