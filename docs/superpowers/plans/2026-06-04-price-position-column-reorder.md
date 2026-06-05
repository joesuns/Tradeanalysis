# 价格滚动分位列重排 + 死代码清理 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Excel 导出中 `price_position_*` 列从视图末尾移至 K线形态列旁边（同属价格指标体系），同时在综合分析 sheet 中展示，并清理 export_wide.py 中全部死代码。

**Architecture:** 修改两张 ADS 视图的 SELECT 列顺序（schema.py），使价格分位列紧接 K线形态之后；export_wide.py 侧添加着色 + 信号过滤 + 清理未使用函数。

**Tech Stack:** DuckDB VIEW DDL, Python/openpyxl

---

### 改动总览

| 文件 | 改动 |
|---|---|
| `backend/db/schema.py` | 两张 VIEW 中将 `price_position_60d/120d/250d` 从末尾移到 `kpattern_strength` 之后 |
| `backend/export_wide.py` | `_SIGNAL_ONLY`、`_SIGNAL_COLS`、`_GROUP_COLORS` 各加 3 个字段；删除 `_reorder_signal_first`、`_COL_GROUPS`、`_get_col_group`、`_GROUP_TINTS`、`_write_sheet` |

---

### Task 1: 重排日线 VIEW 列顺序

**Files:**
- Modify: `backend/db/schema.py:630-632` → 移到 `:574` 之后

- [ ] **Step 1: 在每日线 VIEW 中将 price_position 列移到 kpattern_strength 之后**

在 `backend/db/schema.py` 中，找到 `v_ads_analysis_wide_daily` 的 SELECT 定义（约第 544 行）。

**删除** 原来在 vol_divergence 之后的 3 行（约 630-632）：
```sql
        pp.price_position_60d,
        pp.price_position_120d,
        pp.price_position_250d
```

**插入** 到 `k.strength AS kpattern_strength` 之后（约第 574 行）、`-- Composite volume-price signals` 之前：
```sql
        pp.price_position_60d,
        pp.price_position_120d,
        pp.price_position_250d,
```

即改动后 kpattern 区域变为：
```sql
        k.strength       AS kpattern_strength,

        pp.price_position_60d,
        pp.price_position_120d,
        pp.price_position_250d,

        -- Composite volume-price signals
        CASE
            WHEN pp.price_position_60d > 98 AND v.volume_ratio > 1.5
```

> 注意：`vol_signal` 的 CASE 表达式引用了 `pp.price_position_60d`——价格分位列移到 vol_signal 前面后，pp alias 已经在 JOIN 中可用，CASE 表达式本身无需改动。

- [ ] **Step 2: 验证列顺序**

重启后检查 VIEW 列顺序：

```bash
python3 -c "
import duckdb
from backend.config import DUCKDB_PATH
con = duckdb.connect(DUCKDB_PATH)
cols = con.execute(\"DESCRIBE v_ads_analysis_wide_daily\").fetchall()
for i, (name, _, _, _, _, _) in enumerate(cols, 1):
    if name in ('kpattern', 'kpattern_strength', 'price_position_60d', 'price_position_120d', 'price_position_250d', 'vol_signal'):
        print(f'{i}: {name}')
con.close()
"
```

预期输出：
```
17: kpattern
18: kpattern_strength
19: price_position_60d
20: price_position_120d
21: price_position_250d
22: vol_signal
```

- [ ] **Step 3: Commit**

```bash
git add backend/db/schema.py
git commit -m "feat: reorder price_position columns next to kpattern in daily view

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 重排周线 VIEW 列顺序

**Files:**
- Modify: `backend/db/schema.py:730-732` → 移到 `:674` 之后

- [ ] **Step 1: 在周线 VIEW 中将 price_position 列移到 kpattern_strength 之后**

在 `v_ads_analysis_wide_weekly` 的 SELECT 定义中（约第 644 行）。

**删除** 原来在 vol_divergence 之后的 3 行（约 730-732）：
```sql
        ppw.price_position_60d,
        ppw.price_position_120d,
        ppw.price_position_250d
```

**插入** 到 `kw.strength AS kpattern_strength` 之后（约第 674 行）、vol_signal CASE 之前：
```sql
        ppw.price_position_60d,
        ppw.price_position_120d,
        ppw.price_position_250d,
```

- [ ] **Step 2: 验证列顺序**

```bash
python3 -c "
import duckdb
from backend.config import DUCKDB_PATH
con = duckdb.connect(DUCKDB_PATH)
cols = con.execute(\"DESCRIBE v_ads_analysis_wide_weekly\").fetchall()
for i, (name, _, _, _, _, _) in enumerate(cols, 1):
    if name in ('kpattern', 'kpattern_strength', 'price_position_60d', 'price_position_120d', 'price_position_250d', 'vol_signal'):
        print(f'{i}: {name}')
con.close()
"
```

预期输出与日线一致：
```
17: kpattern
18: kpattern_strength
19: price_position_60d
20: price_position_120d
21: price_position_250d
22: vol_signal
```

- [ ] **Step 3: Commit**

```bash
git add backend/db/schema.py
git commit -m "feat: reorder price_position columns next to kpattern in weekly view

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 价格分位加入 _SIGNAL_ONLY 和 _SIGNAL_COLS

**Files:**
- Modify: `backend/export_wide.py:76-78`（`_SIGNAL_COLS`）
- Modify: `backend/export_wide.py:174-179`（`_SIGNAL_ONLY`）

- [ ] **Step 1: 添加 price_position 到 _SIGNAL_COLS**

将 `backend/export_wide.py` 第 76-78 行改为：

```python
_SIGNAL_COLS = {"kpattern", "kpattern_strength", "macd_divergence", "macd_turning_point",
                "macd_alert", "ma_turning_point", "dde_alert", "dde_divergence",
                "vol_divergence", "vol_signal",
                "price_position_60d", "price_position_120d", "price_position_250d"}
```

> 作用：价格分位为 NULL 时（数据不足），Excel 显示 "-" 而非空单元格。

- [ ] **Step 2: 添加 price_position 到 _SIGNAL_ONLY**

将 `backend/export_wide.py` 第 174-179 行改为：

```python
    _SIGNAL_ONLY = {"kpattern", "kpattern_strength",
                    "price_position_60d", "price_position_120d", "price_position_250d",
                    "macd_divergence", "macd_zone", "macd_turning_point", "macd_alert", "macd_trend",
                    "macd_trend_strength",
                    "ma_alignment", "ma_turning_point", "bias_ma5", "bias_ma10",
                    "dde_trend", "dde_trend_strength", "dde_alert", "dde_divergence",
                    "vol_zone", "vol_trend"}
```

> 作用：综合分析 sheet 中出现价格分位列。

- [ ] **Step 3: Commit**

```bash
git add backend/export_wide.py
git commit -m "feat: include price_position in signal-only analysis sheet and NULL→'-' handling

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 添加价格分位列头着色

**Files:**
- Modify: `backend/export_wide.py:489-496`（`_GROUP_COLORS`）

- [ ] **Step 1: 添加 `price_position_` 前缀到 _GROUP_COLORS**

将 `_write_sheet_merged` 函数内的 `_GROUP_COLORS` 字典（约第 489-496 行）改为：

```python
    _GROUP_COLORS = {
        "kpattern": "C0392B",
        "price_position_": "E74C3C",
        "ema_": "8E44AD", "macd_": "8E44AD", "dif": "8E44AD", "dea": "8E44AD",
        "ma_vol_": "27AE60",
        "ma_": "2980B9", "bias_": "2980B9", "ma5_": "2980B9", "ma10_": "2980B9",
        "dde_": "D35400", "ddx": "D35400", "net_mf": "D35400",
        "vol_": "27AE60", "pct_vol": "27AE60",
    }
```

> 注意：`price_position_` 前缀必须放在 `ema_`、`ma_` 等前缀**之前**（在 `_color_for` 的线性扫描中，`price_position_` 的 `p` 不会与已有关键词冲突，但为安全起见放在 kpattern 之后立即插入）。

- [ ] **Step 2: Commit**

```bash
git add backend/export_wide.py
git commit -m "feat: coral-red header tint for price_position columns (#E74C3C)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 清理死代码

**Files:**
- Modify: `backend/export_wide.py`

删除以下 5 段从未被调用的死代码：

- [ ] **Step 1: 删除 `_reorder_signal_first` 函数（约第 246-265 行）**

删除这 20 行：
```python
def _reorder_signal_first(df: "pd.DataFrame") -> "pd.DataFrame":
    """Reorder columns: signal columns follow identity columns, numeric columns behind.
    Drops the 'freq' column (redundant — sheet name already indicates daily/weekly)."""
    df = df.drop(columns=["freq", "周期"], errors="ignore")
    head = [
        "trade_date", "ts_code", "stock_code", "stock_name", "exchange",
        "sector", "industry", "is_st", "close", "pct_chg",
    ]
    signals = [
        "kpattern", "kpattern_strength",
        "price_position_60d", "price_position_120d", "price_position_250d",
        "macd_divergence", "macd_zone", "macd_turning_point", "macd_alert", "macd_trend",
        "ma_alignment", "ma_turning_point", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope",
        "dde_trend", "dde_trend_strength", "dde_alert", "dde_divergence",
        "vol_zone", "vol_trend", "volume_ratio", "vol_trend_strength", "vol_divergence",
        "vol_signal",
    ]
    tail = [c for c in df.columns if c not in head and c not in signals]
    ordered = [c for c in head + signals + tail if c in df.columns]
    return df[ordered]
```

- [ ] **Step 2: 删除 `_COL_GROUPS` 字典及其注释（约第 267-279 行）**

删除这 13 行（含注释行）：
```python
# Column groups for subtle header tinting
_COL_GROUPS = {
    "identity": {"周期", "交易日期", "股票代码", "代码", "股票名称", "交易所", "板块", "行业", "ST"},
    "price":   {"收盘价", "涨跌幅%", "成交量(万手)", "成交额(亿)", "总市值(亿)", "市盈率", "换手率%"},
    "macd":    {"EMA12", "EMA26", "DIF", "DEA", "MACD柱", "MACD背离", "MACD区域", "MACD转折", "MACD警惕", "MACD趋势"},
    "ma":      {"MA5", "MA10", "MA5乖离率", "MA10乖离率", "MA5斜率", "MA10斜率", "均线形态", "均线转折"},
    "dde":     {"主力净流入(万元)", "DDX", "DDX2", "DDE趋势", "DDE警惕", "DDE背离"},
    "volume":  {"5日均量(万手)", "量能百分位", "量能区域", "量能趋势",
                "量比", "量能趋势强度", "量价背离", "量价复合信号"},
    "price_pos": {"60日价格滚动分位(%)", "120日价格滚动分位(%)", "250日价格滚动分位(%)"},
    "kline":   {"K线形态", "形态强度"},
}
```

- [ ] **Step 3: 删除 `_get_col_group` 函数（约第 281-285 行）**

删除这 5 行：
```python
def _get_col_group(col_name: str):
    for group, names in _COL_GROUPS.items():
        if col_name in names:
            return group
    return None
```

- [ ] **Step 4: 删除 `_GROUP_TINTS` 字典及其注释（约第 287-297 行）**

删除这 11 行（含注释行）：
```python
# Apple-style subtle group header tints (very light, low saturation)
_GROUP_TINTS = {
    "identity": "2C3E50",  # dark navy
    "price":    "1A5276",  # deep blue
    "macd":     "6C3483",  # deep purple
    "ma":       "117864",  # dark green
    "dde":      "7D6608",  # dark gold
    "volume":   "935116",  # dark amber
    "price_pos": "1E8449",  # dark green-teal
    "kline":    "922B21",  # dark burgundy
}
```

- [ ] **Step 5: 删除 `_write_sheet` 函数（约第 299-409 行）**

删除从函数定义到其末尾的完整代码块。函数签名在约第 299-300 行：
```python
def _write_sheet(wb: Workbook, sheet_name: str, df: "pd.DataFrame"):
    """Write a DataFrame to a sheet with Apple-style clean header, auto-fit widths,
    row striping, borders, frozen identity columns, and signal color highlights."""
```

删除该函数的所有行直到（含）`text_signal_cols` 对 `量价复合信号` 的处理结束（约第 409 行）。

> 提示：可通过删除第 298 行（`_GROUP_TINTS` 后的空行）至第 410 行空行之间的所有内容来一次性清理。确认删除后 5 段死代码之间无其他有效代码残留。

- [ ] **Step 6: 验证文件无语法错误**

```bash
python3 -c "import py_compile; py_compile.compile('backend/export_wide.py', doraise=True)"
```

预期：无输出（编译成功）。

- [ ] **Step 7: 确认无残留引用**

```bash
grep -n '_reorder_signal_first\|_COL_GROUPS\|_get_col_group\|_GROUP_TINTS\|_write_sheet\b' backend/export_wide.py
```

预期：无输出。

- [ ] **Step 8: Commit**

```bash
git add backend/export_wide.py
git commit -m "chore: remove dead code — _reorder_signal_first, _COL_GROUPS, _get_col_group, _GROUP_TINTS, _write_sheet

All five symbols were never called (replaced by _write_sheet_merged with
_group_COLORS prefix-matching). Price-position ordering is now handled at
the VIEW layer in schema.py.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: 端到端验证

- [ ] **Step 1: 运行完整导出流程**

```bash
python3 fetch_stocks.py --no-export 2>&1 | tail -5
```

确认 ETL 正常完成。

```bash
python3 fetch_stocks.py --export-only 2>&1 | tail -5
```

确认导出成功，输出类似：
```
已导出 N 行 → exports/analysis_20260604_gen*.xlsx
```

- [ ] **Step 2: 检查 Excel 列顺序**

打开导出的 Excel 文件，在"个股分析"sheet 中确认：
- **日线指标**区域：K线形态 → 形态强度 → 60日价格滚动分位(%) → 120日价格滚动分位(%) → 250日价格滚动分位(%) → 量价信号 → MACD...
- **周线指标**区域：同样的顺序

- [ ] **Step 3: 检查列头颜色**

确认：
- K线形态、形态强度 → 深红 (`#C0392B`)
- 60/120/250日价格滚动分位 → 珊瑚红 (`#E74C3C`)
- 其他指标颜色不变

- [ ] **Step 4: 检查综合分析 sheet**

打开"综合分析"sheet，确认日线和周线区域均出现价格滚动分位三列。

- [ ] **Step 5: 运行现有测试确保无回归**

```bash
pytest tests/ -v --tb=short 2>&1 | tail -30
```

全部测试应通过。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: end-to-end verification of price_position reorder + dead code removal

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
