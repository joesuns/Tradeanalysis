# DWS 指纹跳过 — 实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接通 `input_fingerprint` 的跳过逻辑——calc 前比对 DWD 输入数据指纹，未变化则跳过计算。重复跑同一天从 ~107 分钟降到秒级。

**Architecture:** 在 `base.py` 新增 `check_dwd_unchanged()` 共享函数，由 DWD DataFrame 计算指纹并与 DWS 表中的上次指纹比对。6 个 Calculator 的 `calculate()` 各加 2 行调用。零 schema 变更。

**Tech Stack:** Python 3.9, DuckDB, pandas, hashlib (SHA256)

**效率预估：** 指纹比对 ~3ms/股，4890 股 ≈ 15 秒/Calculator；命中则跳过 ~370s 计算。第二次 run 同一天：全指跳过 → 秒级完成。

---

## 文件改动清单

| 文件 | 改动类型 | 职责 |
|------|:----:|------|
| `backend/etl/base.py` | 修改 | +`check_dwd_unchanged()`、+`SkipReason.FINGERPRINT_MATCH`、`compute_fingerprint` 和 `insert_dws_batch` 改签名 |
| `backend/etl/calc_macd.py` | 修改 | `calculate()` +2 行；`_insert()` 传 `input_fingerprint` |
| `backend/etl/calc_ma.py` | 修改 | 同上 |
| `backend/etl/calc_kpattern.py` | 修改 | 同上 |
| `backend/etl/calc_dde.py` | 修改 | 同上 |
| `backend/etl/calc_volume.py` | 修改 | 同上 |
| `backend/etl/calc_price_position.py` | 修改 | 同上 |
| `tests/test_etl/test_fingerprint_skip.py` | **新建** | 指纹跳过测试 |

---

### Task 1: `base.py` 加共享函数 + 改签名

**Files:**
- Modify: `backend/etl/base.py:8-14,126-146,149-181`

#### 1.1 RED — 写测试

- [ ] **Step 1: 创建 `tests/test_etl/test_fingerprint_skip.py`**

```python
"""Tests for DWS fingerprint skip mechanism."""
import duckdb
import pandas as pd
import hashlib
from backend.etl.base import (
    compute_fingerprint,
    check_dwd_unchanged,
    SkipReason,
)


def test_compute_fingerprint_detects_change():
    """Different data → different fingerprint."""
    df1 = pd.DataFrame({"close": [10.0, 11.0, 12.0], "vol": [100, 200, 300]})
    df2 = pd.DataFrame({"close": [10.0, 11.0, 13.0], "vol": [100, 200, 300]})
    assert compute_fingerprint(df1) != compute_fingerprint(df2)


def test_compute_fingerprint_same_data_same_fp():
    """Same data → same fingerprint."""
    df1 = pd.DataFrame({"close": [10.0, 11.0, 12.0]})
    df2 = pd.DataFrame({"close": [10.0, 11.0, 12.0]})
    assert compute_fingerprint(df1) == compute_fingerprint(df2)


def test_compute_fingerprint_ignores_non_numeric():
    """String columns are excluded from fingerprint."""
    df = pd.DataFrame({
        "close": [10.0, 11.0],
        "ts_code": ["A", "B"],
        "trade_date": ["20260101", "20260102"],
    })
    fp = compute_fingerprint(df)
    assert len(fp) == 16  # SHA256 truncated to 16 hex chars


def test_check_dwd_unchanged_same_fingerprint():
    """When fingerprint matches last stored → unchanged."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_test (
            ts_code TEXT, trade_date TEXT, val REAL,
            calc_date TEXT, input_fingerprint TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    df = pd.DataFrame({"trade_date": ["20260101"], "val": [10.0]})
    fp = compute_fingerprint(df)
    con.execute(
        "INSERT INTO dws_test VALUES ('A.SZ', '20260101', 10, '20260604', ?)",
        (fp,),
    )

    assert check_dwd_unchanged(con, "dws_test", "A.SZ", df) is True
    con.close()


def test_check_dwd_unchanged_different_fingerprint():
    """When fingerprint differs → not unchanged."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_test (
            ts_code TEXT, trade_date TEXT, val REAL,
            calc_date TEXT, input_fingerprint TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    con.execute(
        "INSERT INTO dws_test VALUES ('A.SZ', '20260101', 10, '20260604', 'abc123')",
    )
    df = pd.DataFrame({"trade_date": ["20260101"], "val": [99.0]})

    assert check_dwd_unchanged(con, "dws_test", "A.SZ", df) is False
    con.close()


def test_check_dwd_unchanged_no_prior_fingerprint():
    """No prior fingerprint → not unchanged (first calc)."""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_test (
            ts_code TEXT, trade_date TEXT, val REAL,
            calc_date TEXT, input_fingerprint TEXT,
            PRIMARY KEY (ts_code, trade_date, calc_date)
        )
    """)
    df = pd.DataFrame({"trade_date": ["20260101"], "val": [10.0]})

    assert check_dwd_unchanged(con, "dws_test", "A.SZ", df) is False
    con.close()


def test_skip_reason_fingerprint_match_exists():
    """SkipReason.FINGERPRINT_MATCH should be a valid enum value."""
    assert SkipReason.FINGERPRINT_MATCH == "fingerprint_match"
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_etl/test_fingerprint_skip.py -v
# 预期: FAIL — check_dwd_unchanged 不存在 或 SkipReason.FINGERPRINT_MATCH 不存在
```

#### 1.2 GREEN — 实现

- [ ] **Step 3: 添加 `SkipReason.FINGERPRINT_MATCH`**

在 `backend/etl/base.py` 第 14 行（`DELISTED` 之后）插入：

```python
    FINGERPRINT_MATCH = "fingerprint_match"    # DWD input unchanged since last calc
```

- [ ] **Step 4: 修改 `compute_fingerprint` 的 `float_cols` 为可选**

```python
def compute_fingerprint(df: "pd.DataFrame", float_cols: list[str] = None) -> str:
    """Compute content fingerprint (SHA256 truncated) for a DataFrame.

    When float_cols is None, auto-detects all numeric columns.
    Returns 16-char hex string.
    """
    import pandas as pd
    if float_cols is None:
        float_cols = [c for c in df.columns
                      if c not in ("ts_code", "trade_date")
                      and pd.api.types.is_numeric_dtype(df[c])]
    parts = []
    for col in sorted(float_cols):
        if col not in df.columns:
            continue
        series = df[col].dropna()
        if len(series) == 0:
            parts.append(f"{col}:empty")
        else:
            parts.append(
                f"{col}:{series.min():.6f}:{series.max():.6f}:"
                f"{series.mean():.6f}:{len(series)}"
            )
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

- [ ] **Step 5: 新增 `check_dwd_unchanged`**

在 `compute_fingerprint` 之后插入：

```python
def check_dwd_unchanged(con, dws_table: str, ts_code: str,
                        df: "pd.DataFrame") -> bool:
    """Check if DWD input data is unchanged since last calculation.

    Computes fingerprint of df, queries the DWS table for the most
    recent input_fingerprint for this stock, and compares them.

    Returns True if unchanged (calculation can be skipped).
    """
    new_fp = compute_fingerprint(df)
    row = con.execute(f"""
        SELECT input_fingerprint FROM {dws_table}
        WHERE ts_code = ? AND input_fingerprint IS NOT NULL
        ORDER BY calc_date DESC LIMIT 1
    """, (ts_code,)).fetchone()
    return row is not None and row[0] == new_fp
```

- [ ] **Step 6: 修改 `insert_dws_batch` 接受可选 `input_fingerprint`**

将签名和指纹行改为：

```python
def insert_dws_batch(con, table: str, df: "pd.DataFrame", ts_code: str,
                     calc_date: str, dws_cols: list[str],
                     float_cols: list[str],
                     spec_version: str = "v1",
                     input_fingerprint: str = None):
    ...
    batch["input_fingerprint"] = input_fingerprint or compute_fingerprint(df, float_cols)
```

- [ ] **Step 7: 运行测试确认通过**

```bash
pytest tests/test_etl/test_fingerprint_skip.py -v
# 预期: 7 passed
```

- [ ] **Step 8: 提交**

```bash
git add backend/etl/base.py tests/test_etl/test_fingerprint_skip.py
git commit -m "feat: add check_dwd_unchanged + SkipReason.FINGERPRINT_MATCH

compute_fingerprint: float_cols now optional (auto-detect numeric cols).
insert_dws_batch: accepts optional input_fingerprint override.
check_dwd_unchanged: compares DWD fingerprint with last stored in DWS.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 6 个 Calculator 接通指纹跳过

**Files:**
- Modify: `backend/etl/calc_macd.py`, `calc_ma.py`, `calc_kpattern.py`, `calc_dde.py`, `calc_volume.py`, `calc_price_position.py`

#### 2.1 RED — 写集成测试

- [ ] **Step 1: 追加集成测试验证 Calculator 跳过行为**

```python
def test_volume_calculator_skips_on_fingerprint_match():
    """VolumeCalculator should skip stock when DWD fingerprint matches."""
    import duckdb
    from backend.etl.calc_volume import VolumeCalculator
    from backend.etl.base import CalcResult, SkipReason

    con = duckdb.connect(":memory:")
    # Setup dwd_daily_quote
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT,
            open_qfq REAL, high_qfq REAL, low_qfq REAL, close_qfq REAL,
            vol REAL, amount REAL, pct_chg REAL,
            total_mv REAL, pe_ttm REAL, turnover_rate REAL, volume_ratio REAL,
            is_suspended INTEGER
        )
    """)
    for i in range(30):
        con.execute(
            "INSERT INTO dwd_daily_quote VALUES ('TEST.SZ', ?, 10,11,9,10,100,1000,0,100,15,0.5,1,0)",
            (f"202601{i:02d}",),
        )

    calc = VolumeCalculator(con, "daily")

    # First calc — should compute
    result1 = calc.calculate(["TEST.SZ"], "20260604")
    assert result1.calculated == 1
    assert result1.total_skipped == 0

    # Second calc — same DWD data → should skip
    result2 = calc.calculate(["TEST.SZ"], "20260604")
    assert result2.calculated == 0
    assert result2.total_skipped == 1
    assert SkipReason.FINGERPRINT_MATCH in result2.skipped

    con.close()


def test_volume_calculator_recalculates_when_dwd_changes():
    """VolumeCalculator should recalculate when DWD data changes."""
    import duckdb
    from backend.etl.calc_volume import VolumeCalculator

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code TEXT, trade_date TEXT,
            open_qfq REAL, high_qfq REAL, low_qfq REAL, close_qfq REAL,
            vol REAL, amount REAL, pct_chg REAL,
            total_mv REAL, pe_ttm REAL, turnover_rate REAL, volume_ratio REAL,
            is_suspended INTEGER
        )
    """)
    for i in range(30):
        con.execute(
            "INSERT INTO dwd_daily_quote VALUES ('TEST.SZ', ?, 10,11,9,10,100,1000,0,100,15,0.5,1,0)",
            (f"202601{i:02d}",),
        )

    calc = VolumeCalculator(con, "daily")

    # First calc
    calc.calculate(["TEST.SZ"], "20260604")

    # Add new DWD data
    con.execute(
        "INSERT INTO dwd_daily_quote VALUES ('TEST.SZ', '20260131', 15,16,14,15,200,2000,0,100,15,0.5,1,0)",
    )

    # Second calc — DWD changed → should recalculate
    result2 = calc.calculate(["TEST.SZ"], "20260604")
    assert result2.calculated == 1
    assert result2.total_skipped == 0

    con.close()
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_etl/test_fingerprint_skip.py -v -k "volume_calculator"
# 预期: FAIL — 第二次 calc 没有跳过
```

#### 2.2 GREEN — 实现

- [ ] **Step 3: 修改 6 个 Calculator 的 `calculate()` 和 `_insert()`**

每个 Calculator 做相同模式的改动。以 Volume 为例：

`calculate()` 中，在已有数据验证后、计算前插入：

```python
        # Check fingerprint: skip if DWD input hasn't changed
        if check_dwd_unchanged(self.con, self.dws_table, ts_code, df):
            result.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                            "DWD fingerprint match")
            continue
```

`_insert()` 调用处，传入指纹：

```python
    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str,
                input_fingerprint: str = None):
        ...
        insert_dws_batch(self.con, self.dws_table, df, ts_code, calc_date,
                         dws_cols, float_cols,
                         input_fingerprint=input_fingerprint or compute_fingerprint(df, float_cols))
```

**具体到每个文件：**

`calc_macd.py` — [第 19 行 `calculate()` 方法](backend/etl/calc_macd.py#L19-L52) 的 DWD 查询和验证之后、`_compute_indicators` 之前加指纹检查。`_insert` 加 `input_fingerprint` 参数。

`calc_ma.py`、`calc_kpattern.py`、`calc_dde.py`、`calc_volume.py`、`calc_price_position.py` — 同上模式。

引入 import：

```python
from backend.etl.base import (..., check_dwd_unchanged)
```

- [ ] **Step 4: 运行集成测试确认通过**

```bash
pytest tests/test_etl/test_fingerprint_skip.py -v
# 预期: 9 passed
```

- [ ] **Step 5: 提交**

```bash
git add backend/etl/calc_*.py tests/test_etl/test_fingerprint_skip.py
git commit -m "feat: connect DWS fingerprint skip in all 6 Calculators

Each calculate() checks check_dwd_unchanged() before computing:
if DWD input fingerprint matches last stored → skip the stock.
_insert() passes computed fingerprint to insert_dws_batch().

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 全量测试回归 + CLAUDE.md 更新

- [ ] **Step 1: 运行全量测试**

```bash
pytest tests/ -v --tb=short
# 预期: 新增测试 PASS，既有 4 个失败不受影响
```

- [ ] **Step 2: 更新 CLAUDE.md**

在 "关键技术细节" 或 "已知问题" 区块插入：

```markdown
- **DWS 指纹跳过 (v2):** `check_dwd_unchanged()` 在 calc 前比对 DWD 输入数据 SHA256 指纹。
  相同 → 跳过计算；不同 → 重算。重复跑同一天从 ~107min 降到秒级。
  `input_fingerprint` 存储于每张 DWS 表的每行中，由 `insert_dws_batch()` 写入。
```

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: document DWS fingerprint skip mechanism

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 端到端验证

- [ ] **Step 1: 第一次 run**

```bash
python -m backend.cli run
# 全量计算，预计 ~107min，写入指纹
```

- [ ] **Step 2: 第二次 run（同日期）**

```bash
python -m backend.cli run
# 指纹匹配 → 全部跳过 → 秒级完成
```

核心验证日志：
```
calc VolumeCalculator daily: N calculated, M skipped (fingerprint_match)
```

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "chore: end-to-end verification — fingerprint skip"
```

---

## 自检

- ✅ 零 schema 变更 —— `input_fingerprint` 已存在于所有 12 张 DWS 表
- ✅ `compute_fingerprint` 向下兼容 —— `float_cols=None` 自动检测数值列
- ✅ `insert_dws_batch` 向下兼容 —— `input_fingerprint=None` 时行为不变
- ✅ `check_dwd_unchanged` 无历史指纹返回 False（首次必算）
- ✅ 每个 Task 有 RED→GREEN TDD 循环
- ✅ 所有 Calculator 使用相同的 2 行模式
- ✅ 不影响 `export_wide.py`、视图、API
