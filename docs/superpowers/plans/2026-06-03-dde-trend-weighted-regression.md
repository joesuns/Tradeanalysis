# DDE 趋势指数加权回归 + trend_strength — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 DDE 趋势从等权线性回归改为指数加权回归（对齐 MACD），并新增 `trend_strength` 列使跨股票可排序。

**Architecture:** 两阶段。Phase 1 改 `_compute_trend` 从 `np.polyfit` 等权回归切换为指数加权回归（decay=0.20, window=8），新增 `_compute_trend_strength` 方法。Phase 2 在 DDL 和视图中新增 `trend_strength` 列，更新 `_insert` 写入逻辑。保持 8-bar 窗口和 0.0001 阈值不变。

**Tech Stack:** Python 3.9, NumPy, DuckDB, pytest

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `backend/etl/calc_dde.py` | 改 `_compute_trend`（等权→指数加权）、新增 `_compute_trend_strength`、改 `_insert` |
| `backend/db/schema.py` | DDE DDL 模板增加 `trend_strength REAL` 列、ADS 宽表视图增加 `dde_trend_strength` 列 |
| `tests/test_etl/test_calc_dde.py` | 新增趋势加权 + trend_strength 单元测试 |
| `backend/export_wide.py` | Excel 导出列映射增加 `dde_trend_strength`（可选） |

---

### Task 1: 新增 `_compute_trend_strength` 方法

**Files:**
- Modify: `backend/etl/calc_dde.py:190-211`

- [ ] **Step 1: 在 `_compute_indicators` 中增加对 `_compute_trend_strength` 的调用**

找到 `_compute_indicators` 方法，在已有 `df["trend"] = ...` 之后新增一行：

```python
# 在 calc_dde.py 的 _compute_indicators 方法中，第 180 行之后新增：
df["trend_strength"] = self._compute_trend_strength(df["ddx2"].values.astype(float), window=trend_window)
```

完整上下文（第 178-181 行改为）：

```python
        # Trend: exponentially weighted linear regression on DDX2 (8-bar)
        trend_window = 8
        df["trend"] = self._compute_trend(df["ddx2"].values.astype(float), window=trend_window)
        df["trend_strength"] = self._compute_trend_strength(df["ddx2"].values.astype(float), window=trend_window)
```

- [ ] **Step 2: 新增 `_compute_trend_strength` 方法定义**

在 `_compute_trend` 方法之后（第 211 行空行处）插入新方法：

```python
    def _compute_trend_strength(self, ddx2: np.ndarray, window: int = 8) -> np.ndarray:
        """DDX2 trend strength via exponentially weighted linear regression.

        Formula: weighted_slope / mean(|DDX2_segment|), unitless signed value.
        Positive = bullish capital flow strength, negative = bearish.
        Weighted regression (decay=0.20) gives recent bars ~3x more influence
        than bars 8 days ago.

        Returns NaN where window < window or all DDX2 in segment are zero.
        """
        result = np.full(len(ddx2), np.nan)
        for i in range(window - 1, len(ddx2)):
            segment = ddx2[i - window + 1:i + 1]
            valid = segment[~np.isnan(segment)]
            if len(valid) < window:
                continue
            mean_abs = np.mean(np.abs(valid))
            if mean_abs == 0:
                continue
            n = len(valid)
            x = np.arange(n, dtype=float)
            weights = np.exp(x * 0.20)
            try:
                slope = float(np.polyfit(x, valid, 1, w=weights)[0])
            except (np.linalg.LinAlgError, ValueError, TypeError):
                continue
            if not np.isfinite(slope):
                continue
            result[i] = slope / mean_abs
        return result
```

- [ ] **Step 3: Commit**

```bash
git add backend/etl/calc_dde.py
git commit -m "feat: add _compute_trend_strength for DDE (weighted regression, unitless)"
```

---

### Task 2: 将 `_compute_trend` 改为指数加权回归

**Files:**
- Modify: `backend/etl/calc_dde.py:190-211`

- [ ] **Step 1: 替换 `_compute_trend` 实现**

将当前第 190-211 行的 `_compute_trend` 方法替换为：

```python
    def _compute_trend(self, ddx2: np.ndarray, window: int = 8) -> list:
        """DDX2 trend via exponentially weighted linear regression.

        Weighted regression (decay=0.20) makes recent bars ~3x more influential.
        - up: weighted_slope > 0.0001
        - down: weighted_slope < -0.0001
        - flat: otherwise
        """
        result = [None] * len(ddx2)
        for i in range(len(ddx2)):
            if i < window - 1:
                continue
            segment = ddx2[i - window + 1:i + 1]
            valid = segment[~np.isnan(segment)]
            if len(valid) < window:
                continue
            n = len(valid)
            x = np.arange(n, dtype=float)
            weights = np.exp(x * 0.20)
            try:
                slope = float(np.polyfit(x, valid, 1, w=weights)[0])
            except (np.linalg.LinAlgError, ValueError, TypeError):
                continue
            if not np.isfinite(slope):
                continue
            if slope > 0.0001:
                result[i] = "up"
            elif slope < -0.0001:
                result[i] = "down"
            else:
                result[i] = "flat"
        return result
```

- [ ] **Step 2: 运行现有趋势测试确保兼容**

```bash
pytest tests/test_etl/test_calc_dde.py::test_dde_trend_8bar_window -v
```

预期: PASS（趋势方向判定逻辑不变，但斜率值因加权会变化，测试只检查非 None）

- [ ] **Step 3: 运行全部 DDE 单元测试**

```bash
pytest tests/test_etl/test_calc_dde.py -v
```

预期: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add backend/etl/calc_dde.py
git commit -m "refactor: switch DDE trend from equal-weight to exponentially weighted regression (decay=0.20)"
```

---

### Task 3: DDL — DDE 表增加 `trend_strength` 列

**Files:**
- Modify: `backend/db/schema.py:312-327`

- [ ] **Step 1: 在 DDE DDL 模板中增加 `trend_strength REAL` 列**

找到 schema.py 第 313-327 行的 `"dde"` 模板，在 `trend TEXT` 之后增加一行：

```python
    "dde": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code        TEXT,
        trade_date     TEXT,
        net_mf_amount  REAL,
        ddx            REAL,
        ddx2           REAL,
        trend          TEXT,
        trend_strength REAL,
        alert          TEXT,
        divergence     TEXT,
        calc_date      TEXT,
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (trend IN ('up', 'down', 'flat')),
        CHECK (alert IN ('upturn_reverse', 'downturn_reverse', 'upturn_flat', 'downturn_flat') OR alert IS NULL),
        CHECK (divergence IN ('top_divergence', 'bottom_divergence') OR divergence IS NULL)
    )""",
```

注意：`trend_strength REAL` 不加 CHECK 约束（允许 NULL，历史数据无此列时填 NULL）。

- [ ] **Step 2: 在 ADS 日线宽表视图中增加 `dde_trend_strength` 列**

找到 schema.py 第 460-463 行的日线宽表 DDE 段：

```python
        d.net_mf_amount, d.ddx, d.ddx2,
        d.trend          AS dde_trend,
        d.alert          AS dde_alert,
        d.divergence     AS dde_divergence,
```

改为：

```python
        d.net_mf_amount, d.ddx, d.ddx2,
        d.trend          AS dde_trend,
        d.trend_strength AS dde_trend_strength,
        d.alert          AS dde_alert,
        d.divergence     AS dde_divergence,
```

- [ ] **Step 3: 在 ADS 周线宽表视图中增加 `dde_trend_strength` 列**

找到 schema.py 第 535-536 行的周线宽表 DDE 段：

```python
        dw.net_mf_amount, dw.ddx, dw.ddx2,
        dw.trend         AS dde_trend,
```

改为：

```python
        dw.net_mf_amount, dw.ddx, dw.ddx2,
        dw.trend          AS dde_trend,
        dw.trend_strength AS dde_trend_strength,
```

- [ ] **Step 4: 验证 DDL 语法**

```bash
python3 -c "
import duckdb
con = duckdb.connect(':memory:')
# Simulate table creation with new column
con.execute('''
    CREATE TABLE dws_dde_daily (
        ts_code TEXT, trade_date TEXT, net_mf_amount REAL,
        ddx REAL, ddx2 REAL, trend TEXT, trend_strength REAL,
        alert TEXT, divergence TEXT, calc_date TEXT,
        PRIMARY KEY (ts_code, trade_date, calc_date)
    )
''')
con.execute("INSERT INTO dws_dde_daily VALUES ('TEST.SZ','20260101',100,0.3,0.25,'up',0.15,NULL,NULL,'20260102')")
print('DDL OK: row count =', con.execute('SELECT COUNT(*) FROM dws_dde_daily').fetchone()[0])
con.close()
"
```

预期: `DDL OK: row count = 1`

- [ ] **Step 5: Commit**

```bash
git add backend/db/schema.py
git commit -m "feat: add trend_strength column to DDE tables and ADS wide views"
```

---

### Task 4: 更新 `_insert` 写入 `trend_strength`

**Files:**
- Modify: `backend/etl/calc_dde.py:309-326`

- [ ] **Step 1: 在 `_insert` 方法中增加 `trend_strength` 列的写入**

找到 `_insert` 方法（第 309 行），修改 `dws_cols` 列表和 `to_float_safe` 转换列：

```python
    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        """Batch insert all rows for one stock via DuckDB register."""
        dws_cols = ["ts_code", "trade_date", "net_mf_amount", "ddx", "ddx2",
                    "trend", "trend_strength", "alert", "divergence", "calc_date"]
        data_cols = dws_cols[1:]
        for c in data_cols:
            if c not in df.columns:
                df[c] = None
        batch = df[data_cols].copy()
        batch["ts_code"] = ts_code
        for c in ["net_mf_amount", "ddx", "ddx2", "trend_strength"]:
            batch[c] = batch[c].apply(to_float_safe)
        batch["calc_date"] = batch["calc_date"].astype(str)
        batch = batch[dws_cols]
        self.con.register("_batch", batch)
        cols_sql = ", ".join(dws_cols)
        self.con.execute(f"INSERT OR REPLACE INTO {self.dws_table} ({cols_sql}) SELECT {cols_sql} FROM _batch")
        self.con.unregister("_batch")
```

变更点：
- 第 311 行 `dws_cols` 增加 `"trend_strength"`
- 第 319 行 `to_float_safe` 转换列增加 `"trend_strength"`

- [ ] **Step 2: Commit**

```bash
git add backend/etl/calc_dde.py
git commit -m "feat: write trend_strength in DDE _insert"
```

---

### Task 5: 新增单元测试

**Files:**
- Modify: `tests/test_etl/test_calc_dde.py`

- [ ] **Step 1: 新增测试 — 指数加权回归趋势判定**

在文件末尾追加：

```python
def test_dde_trend_weighted_regression():
    """指数加权回归：近期 bar 权重更大，上升趋势应被捕捉。"""
    calc = DDECalculator.__new__(DDECalculator)

    # 前 4 天下降，后 4 天快速上升 → 加权回归应判 up
    ddx2 = np.array([0.010, 0.008, 0.006, 0.004, 0.003,
                     0.005, 0.008, 0.012, 0.016, 0.020])
    result = calc._compute_trend(ddx2, window=8)
    # 第 9 根（index 9, 0-indexed 第 10 个）：后 4 天上升势头强
    assert result[9] == "up", (
        f"加权回归应捕捉近期上升势头，实际 {result[9]}"
    )


def test_dde_trend_weighted_flat():
    """指数加权回归：无明显方向时应判 flat。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.array([0.010, 0.011, 0.009, 0.010, 0.011,
                     0.009, 0.010, 0.010, 0.011, 0.009])
    result = calc._compute_trend(ddx2, window=8)
    assert result[9] == "flat", (
        f"无明显趋势应判 flat，实际 {result[9]}"
    )


def test_dde_trend_strength_positive():
    """trend_strength: 上升趋势返回正值。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.array([0.001, 0.002, 0.003, 0.004, 0.005,
                     0.006, 0.007, 0.008, 0.009, 0.010])
    result = calc._compute_trend_strength(ddx2, window=8)
    assert result[9] is not None, "trend_strength 不应为 None"
    assert not np.isnan(result[9]), "trend_strength 不应为 NaN"
    assert result[9] > 0, (
        f"单调上升应返回正强度，实际 {result[9]}"
    )


def test_dde_trend_strength_negative():
    """trend_strength: 下降趋势返回负值。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.array([0.010, 0.009, 0.008, 0.007, 0.006,
                     0.005, 0.004, 0.003, 0.002, 0.001])
    result = calc._compute_trend_strength(ddx2, window=8)
    assert result[9] is not None, "trend_strength 不应为 None"
    assert not np.isnan(result[9]), "trend_strength 不应为 NaN"
    assert result[9] < 0, (
        f"单调下降应返回负强度，实际 {result[9]}"
    )


def test_dde_trend_strength_window_insufficient():
    """窗口不足时 trend_strength 应返回 NaN。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.array([0.001, 0.002, 0.003])  # 只有 3 根，不足 window=8
    result = calc._compute_trend_strength(ddx2, window=8)
    for i in range(len(ddx2)):
        assert np.isnan(result[i]), (
            f"窗口不足 index {i} 应返回 NaN，实际 {result[i]}"
        )


def test_dde_trend_strength_zero_mean():
    """DDX2 全为零时 trend_strength 应返回 NaN（除零保护）。"""
    calc = DDECalculator.__new__(DDECalculator)

    ddx2 = np.zeros(10)
    result = calc._compute_trend_strength(ddx2, window=8)
    # 全零时 mean_abs=0，应 continue 跳过
    assert np.isnan(result[9]), (
        f"全零 DDX2 应返回 NaN，实际 {result[9]}"
    )
```

- [ ] **Step 2: 运行新测试验证失败（trend_strength 尚未定义）**

```bash
pytest tests/test_etl/test_calc_dde.py::test_dde_trend_weighted_regression \
  tests/test_etl/test_calc_dde.py::test_dde_trend_weighted_flat \
  tests/test_etl/test_calc_dde.py::test_dde_trend_strength_positive \
  tests/test_etl/test_calc_dde.py::test_dde_trend_strength_negative \
  tests/test_etl/test_calc_dde.py::test_dde_trend_strength_window_insufficient \
  tests/test_etl/test_calc_dde.py::test_dde_trend_strength_zero_mean -v
```

- [ ] **Step 3: 运行全部 DDE 测试确认通过**

```bash
pytest tests/test_etl/test_calc_dde.py -v
```

预期: 16 个测试全部 PASS（10 旧 + 6 新）

- [ ] **Step 4: Commit**

```bash
git add tests/test_etl/test_calc_dde.py
git commit -m "test: add weighted regression and trend_strength tests for DDE"
```

---

### Task 6: 集成验证

**Files:**
- 无新建/修改

- [ ] **Step 1: 单只股票全流水线验证**

```bash
python3 -m backend.cli etl --step build-all --ts-code 000001.SZ --start 20260501 --end 20260602
```

预期: ETL 成功完成，无报错

- [ ] **Step 2: 查询 DDE trend_strength 列**

```bash
python3 -c "
import duckdb
con = duckdb.connect('data/tradeanalysis.duckdb', read_only=True)
rows = con.execute('''
    SELECT ts_code, trade_date, ddx2, trend, trend_strength
    FROM v_dws_dde_daily_latest
    WHERE ts_code = '000001.SZ'
    ORDER BY trade_date DESC
    LIMIT 10
''').fetchall()
for r in rows:
    print(r)
con.close()
"
```

预期: 最近 10 个交易日每行有 `trend` 和 `trend_strength` 值，trend_strength 为数字或 NULL（历史数据）

- [ ] **Step 3: 验证 ADS 宽表视图包含新列**

```bash
python3 -c "
import duckdb
con = duckdb.connect('data/tradeanalysis.duckdb', read_only=True)
# 检查列是否存在
cols = con.execute(\"SELECT column_name FROM information_schema.columns WHERE table_name = 'v_ads_analysis_wide_daily' AND column_name LIKE '%dde%'\").fetchall()
for c in cols:
    print(c[0])
con.close()
"
```

预期输出包含:
```
dde_trend
dde_trend_strength
dde_alert
dde_divergence
```

- [ ] **Step 4: 运行全部测试**

```bash
pytest tests/ -v
```

预期: 全部 PASS（含新增 DDE 测试）

---

## 设计决策记录

| 决策 | 取值 | 理由 |
|------|------|------|
| 加权方式 | 指数加权 `np.exp(x * decay)` | 对齐 MACD 趋势的 `np.polyfit(x, y, 1, w=weights)` 模式 |
| decay 系数 | **0.20** | MACD 用 0.15；DDE 设 0.20 稍灵敏——资金数据噪音大于价格，需更强调近期 |
| 窗口 | **8-bar**（不变） | 已确认，8 日覆盖约 1.5 个交易周的中线资金趋势 |
| 阈值 | **0.0001**（不变） | DDX2 典型范围 ±0.02~0.05，8-bar 斜率 0.0001 合理 |
| trend_strength 公式 | `weighted_slope / mean(abs(DDX2_segment))` | 对齐 MACD `slope / mean(abs(bar))` 公式，unitless 可跨股票比较 |
| 零值保护 | `mean_abs == 0 → continue（NaN）` | DDX2 全零时无法归一化，返回 NaN |
| 历史兼容 | trend_strength 列无 NOT NULL 约束 | 旧数据重算前为 NULL，向前兼容 |

## 回滚方案

如果指数加权回归导致趋势信号过于敏感（false positive 增多），两个 knob 可调：

1. **降低 decay**：从 0.20 降到 0.15（对齐 MACD），减弱近期权重
2. **收窄阈值**：从 0.0001 提到 0.00015，提高触发门槛

修改位置均在 `calc_dde.py` 的 `_compute_trend` 方法中，无需改 DDL。
