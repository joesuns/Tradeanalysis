# 量能指标优化 + Price Position 基础设施 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为量能模块新增 volume_ratio / divergence / trend_strength 三列，新建 dws_price_position 表（价格类指标基础设施），扩展 ADS 视图和 Excel 导出。

**Architecture:** 新建 PricePositionCalculator（独立模块，纯价格特征）作为第 6 个 DWS 计算器；扩展 VolumeCalculator 增加量价交互信号；所有跨指标复合信号留在视图层。不改动 DWD 层、不碰其他 4 个 Calculator。

**Tech Stack:** Python 3.9+, NumPy, pandas, DuckDB ≥1.0, pytest

**分类体系:**
- 量能类指标: volume_ratio, divergence, trend_strength → `dws_volume_*`
- 价格类指标: price_position → `dws_price_position_*`（与 K 线形态 & strength 同属价格类）

---

## 文件改动清单

| 文件 | 改动类型 | 职责 |
|------|:----:|------|
| `backend/etl/calc_price_position.py` | **新建** | PricePositionCalculator，三个窗口的 price_position 计算 |
| `backend/etl/calc_volume.py` | 修改 | SQL 扩展读 close_qfq；+3 新方法；_insert 扩展 |
| `backend/db/schema.py` | 修改 | +2 表 DDL + 索引 + CHECK + latest 视图；修改 volume 表 CHECK；扩展 ADS 视图 |
| `backend/etl/orchestrator.py` | 修改 | 注册 PricePositionCalculator 到 CALCULATORS |
| `backend/export_wide.py` | 修改 | 新增列翻译、着色、列组、信号列注册 |
| `tests/test_etl/test_calc_price_position.py` | **新建** | PricePositionCalculator 单元测试 + 集成测试 |
| `tests/test_etl/test_calc_volume.py` | 修改 | 新增 ratio / divergence / trend_strength 测试 |

---

### Task 1: PricePositionCalculator — 新建模块

**Files:**
- Create: `backend/etl/calc_price_position.py`
- Create: `tests/test_etl/test_calc_price_position.py`

#### 1.1 写测试文件

- [ ] **Step 1: 创建测试文件**

```python
# tests/test_etl/test_calc_price_position.py

import pandas as pd
import numpy as np
from backend.etl.calc_price_position import PricePositionCalculator


def test_price_position_60d():
    """price_position_60d = (close - 60d_low) / (60d_high - 60d_low) * 100"""
    calc = PricePositionCalculator.__new__(PricePositionCalculator)
    calc.freq = "daily"
    calc.src_table = "dwd_daily_quote"
    calc.dws_table = "dws_price_position_daily"

    n = 80
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    # Prices: start at 10, dip to 5, rise to 15
    closes = [10.0] * 30 + list(np.linspace(10, 5, 10)) + [5.0] * 10 + list(np.linspace(5, 15, 30))
    closes = closes[:n]
    df = pd.DataFrame({"trade_date": dates, "close_qfq": closes})
    
    # Call the private _compute method directly (bypassing DB)
    result = calc._compute_positions(df)
    
    # At the dip (index 49, close=5): 60d window should have low near 5
    # price_position_60d should be near 0 (at the very bottom of 60d range)
    pp60 = result["price_position_60d"].iloc[49]
    assert pp60 is not None and pp60 < 10, f"At the dip, price_position_60d should be < 10, got {pp60}"
    
    # At the peak (index 79, close=15): should be near 100
    pp60_end = result["price_position_60d"].iloc[79]
    assert pp60_end is not None and pp60_end > 90, f"At peak, price_position_60d should be > 90, got {pp60_end}"
    
    # Before window fills (index 30): should have value (>=60 data points)
    mid = result["price_position_60d"].iloc[30]
    assert mid is not None, "After 30 data points (<60), should still have NaN or value"


def test_price_position_boundaries():
    """price_position is always in [0, 100]."""
    calc = PricePositionCalculator.__new__(PricePositionCalculator)
    calc.freq = "daily"
    
    n = 100
    dates = [f"202601{i:02d}" for i in range(1, n + 1)]
    np.random.seed(42)
    closes = np.cumsum(np.random.randn(n)) + 100
    df = pd.DataFrame({"trade_date": dates, "close_qfq": closes})
    result = calc._compute_positions(df)
    
    for col in ["price_position_60d", "price_position_120d", "price_position_250d"]:
        valid = result[col].dropna()
        assert (valid >= 0).all(), f"{col} has values < 0"
        assert (valid <= 100).all(), f"{col} has values > 100"


def test_price_position_all_windows():
    """Each window column exists and has values after enough data."""
    calc = PricePositionCalculator.__new__(PricePositionCalculator)
    calc.freq = "daily"
    
    n = 300
    dates = [f"2026{i:03d}" for i in range(1, n + 1)]
    closes = [10.0 + i * 0.02 for i in range(n)]  # steadily rising
    df = pd.DataFrame({"trade_date": dates, "close_qfq": closes})
    result = calc._compute_positions(df)
    
    # After 60 rows: price_position_60d should start having values
    assert result["price_position_60d"].iloc[59] is not None, "60d column should have value at index 59"
    # 120d and 250d should be NaN until enough data
    assert pd.isna(result["price_position_120d"].iloc[59]), "120d should be NaN with only 60 rows"
    # After 120 rows: 120d column fills in
    assert result["price_position_120d"].iloc[119] is not None, "120d column should have value at index 119"
    # After 250 rows: 250d column fills in  
    assert result["price_position_250d"].iloc[249] is not None, "250d column should have value at index 249"


def test_integration_price_position(db_with_schema):
    """Integration: full calculate + INSERT with real DuckDB."""
    con = db_with_schema
    con.execute(
        "INSERT INTO dim_stock (ts_code, stock_code, name) VALUES ('TEST.SZ','TEST','Test')"
    )
    
    n = 150
    for i in range(1, n + 1):
        price = 10.0 + i * 0.1
        con.execute(
            "INSERT INTO dwd_daily_quote (ts_code, trade_date, close_qfq, is_suspended) "
            "VALUES (?,?,?,0)",
            ("TEST.SZ", f"202601{i:02d}", price),
        )
    
    calc = PricePositionCalculator(con, "daily")
    calc.calculate(["TEST.SZ"], "20260201")
    
    rows = con.execute(
        "SELECT trade_date, price_position_60d, price_position_120d, price_position_250d "
        "FROM dws_price_position_daily WHERE ts_code='TEST.SZ' ORDER BY trade_date"
    ).fetchall()
    
    assert len(rows) > 0, "Should have rows inserted"
    # Last row (peak price) should have high position values
    last = rows[-1]
    pp60 = last[1]
    pp120 = last[2]
    assert pp60 is not None and pp60 > 90, f"Last row price_position_60d should be high, got {pp60}"
    if pp120 is not None:
        assert pp120 > 90, f"Last row price_position_120d should be high, got {pp120}"
    # 250d: may be NaN if not enough data
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_etl/test_calc_price_position.py -v
# 预期：全部 FAIL（模块不存在 / PricePositionCalculator 未定义）
```

#### 1.2 实现 PricePositionCalculator

- [ ] **Step 3: 创建 calc_price_position.py**

```python
# backend/etl/calc_price_position.py

import numpy as np
import pandas as pd
from backend.etl.base import to_float_safe


class PricePositionCalculator:
    """Price position (relative strength) calculator.

    Computes price_position for 3 window sizes (60, 120, 250).
    price_position_N = (close - N_day_low) / (N_day_high - N_day_low) * 100

    This is a PURE PRICE feature — no dependency on any other DWS table.
    It serves as infrastructure for MACD divergence high-position checks,
    K-pattern trend context, and volume zone price-aware interpretation.
    Works for both daily and weekly frequencies.
    """

    WINDOWS = [60, 120, 250]

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws_table = f"dws_price_position_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str):
        """Calculate price_position for a batch of stocks. INSERT results into DWS table."""
        for ts_code in ts_codes:
            if self.freq == "weekly":
                df = self.con.execute(f"""
                    SELECT d.trade_date, d.close_qfq FROM {self.src_table} d
                    JOIN dim_date dd ON d.trade_date = dd.trade_date
                    WHERE d.ts_code = ? AND dd.is_week_end = 1
                    ORDER BY d.trade_date
                """, (ts_code,)).df()
            else:
                df = self.con.execute(f"""
                    SELECT trade_date, close_qfq FROM {self.src_table}
                    WHERE ts_code = ? AND is_suspended = 0
                    ORDER BY trade_date
                """, (ts_code,)).df()
            if df.empty or len(df) < 60:
                continue
            df = self._compute_positions(df)
            self._insert(ts_code, df, calc_date)

    def _compute_positions(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute price_position for all window sizes using rolling min/max."""
        c = df["close_qfq"].values.astype(float)

        for window in self.WINDOWS:
            col = f"price_position_{window}d"
            result = np.full(len(c), np.nan)

            # Rolling min/max via pandas for efficiency
            rolling_min = pd.Series(c).rolling(window, min_periods=window).min().values
            rolling_max = pd.Series(c).rolling(window, min_periods=window).max().values

            for i in range(window - 1, len(c)):
                if pd.isna(c[i]):
                    continue
                lo = rolling_min[i]
                hi = rolling_max[i]
                if pd.isna(lo) or pd.isna(hi) or hi == lo:
                    continue
                result[i] = (c[i] - lo) / (hi - lo) * 100.0

            df[col] = result

        return df

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        """Batch insert all rows for one stock via DuckDB register."""
        dws_cols = ["ts_code", "trade_date",
                    "price_position_60d", "price_position_120d",
                    "price_position_250d", "calc_date"]
        data_cols = dws_cols[1:]
        for c in data_cols:
            if c not in df.columns:
                df[c] = None
        batch = df[data_cols].copy()
        batch["ts_code"] = ts_code
        for c in ["price_position_60d", "price_position_120d", "price_position_250d"]:
            batch[c] = batch[c].apply(to_float_safe)
        batch["calc_date"] = batch["calc_date"].astype(str)
        batch = batch[dws_cols]
        self.con.register("_batch", batch)
        cols_sql = ", ".join(dws_cols)
        self.con.execute(
            f"INSERT OR REPLACE INTO {self.dws_table} ({cols_sql}) "
            f"SELECT {cols_sql} FROM _batch"
        )
        self.con.unregister("_batch")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_etl/test_calc_price_position.py -v
# 预期：4 passed（如果 db_with_schema fixture 存在且 dws_price_position_daily 表已建）
# 注意：此时 schema 尚未更新，集成测试会因表不存在而失败——这是预期行为（Task 3 解决）
```

- [ ] **Step 5: 提交**

```bash
git add backend/etl/calc_price_position.py tests/test_etl/test_calc_price_position.py
git commit -m "feat: add PricePositionCalculator — 60/120/250d rolling price position"
```

---

### Task 2: VolumeCalculator — 扩展量能模块

**Files:**
- Modify: `backend/etl/calc_volume.py`
- Modify: `tests/test_etl/test_calc_volume.py`

#### 2.1 写 volume_ratio 测试

- [ ] **Step 1: 添加 volume_ratio 测试**

在 `tests/test_etl/test_calc_volume.py` 末尾追加：

```python
def test_volume_ratio():
    """volume_ratio = vol / MA5_vol. 值域以 1.0 为中心。"""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 30
    dates = [f"d{i}" for i in range(n)]
    vols = [1000000.0] * 10 + [3000000.0] * 5 + [1000000.0] * 15
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)

    # 前 10 天：成交量恒定，量比应在 1.0 附近
    ratio_mid = result["volume_ratio"].iloc[9]
    assert ratio_mid is not None and 0.9 < ratio_mid < 1.1, \
        f"Constant volume should give ratio ~1.0, got {ratio_mid}"

    # 爆量日（index 10-14）：vol=3M, MA5 仍受前值影响，ratio 应显著 > 1
    ratio_high = result["volume_ratio"].iloc[10]
    assert ratio_high is not None and ratio_high > 1.5, \
        f"Volume spike should give ratio > 1.5, got {ratio_high}"


def test_volume_divergence_top():
    """价格创 60 日新高 + 成交量未跟随 → 顶背离。"""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 80
    dates = [f"d{i}" for i in range(n)]
    closes = [10.0 + i * 0.2 for i in range(60)]   # steady rise
    closes += [closes[59]] * 20                     # plateau at high
    vols = [1000000.0 - i * 5000 for i in range(60)]  # declining vol during rise
    vols += [vols[59]] * 20

    df = pd.DataFrame({
        "trade_date": dates,
        "vol": vols,
        "close_qfq": closes,
    })
    result = calc._compute_indicators(df)

    # 检查 divergence 列是否存在
    assert "divergence" in result.columns, "divergence column should exist"
    # 顶背离应出现在价格高位 + 量萎缩时
    divs = result["divergence"].dropna()
    # 至少有一个 top_divergence
    assert "top_divergence" in divs.values, \
        f"Expected top_divergence with price high + declining vol, got {divs.unique()}"


def test_volume_divergence_bottom():
    """价格创 60 日新低 + 成交量回升 → 底背离。"""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 80
    dates = [f"d{i}" for i in range(n)]
    closes = [50.0 - i * 0.5 for i in range(50)]  # steady decline
    closes += [closes[49]] * 30                    # bottom plateau
    vols = [500000.0 + i * 5000 for i in range(50)]  # increasing vol during decline
    vols += [vols[49]] * 30

    df = pd.DataFrame({
        "trade_date": dates,
        "vol": vols,
        "close_qfq": closes,
    })
    result = calc._compute_indicators(df)

    divs = result["divergence"].dropna()
    assert "bottom_divergence" in divs.values, \
        f"Expected bottom_divergence with price low + recovering vol, got {divs.unique()}"


def test_volume_trend_strength():
    """trend_strength 是去量纲的连续值，正值上升 / 负值下降。"""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 30
    dates = [f"d{i}" for i in range(n)]
    # Steadily increasing volume
    vols = [1000000.0 + i * 50000 for i in range(n)]
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)

    # 趋势强度列应存在
    assert "trend_strength" in result.columns

    # 持续放量时 trend_strength 应 > 0
    ts = result["trend_strength"].dropna()
    assert len(ts) > 0, "Should have trend_strength values"
    # 至少一半的值应为正
    positive_ratio = (ts > 0).sum() / len(ts)
    assert positive_ratio > 0.5, \
        f"Continuously expanding volume should have mostly positive trend_strength, got {positive_ratio:.1%}"


def test_volume_divergence_dedup():
    """同一背离类型 5 日内不重复标注。"""
    calc = VolumeCalculator.__new__(VolumeCalculator)

    n = 100
    dates = [f"d{i}" for i in range(n)]
    closes = [10.0 + i * 0.1 for i in range(80)] + [closes := 18.0] * 20
    # Fix the syntax: this is list comp embedded in list literal — won't work
    # Let's write cleanly:
    closes_list = []
    for i in range(n):
        if i < 80:
            closes_list.append(10.0 + i * 0.1)
        else:
            closes_list.append(18.0)
    vols = [900000.0 - i * 5000 for i in range(60)] + [500000.0] * 40

    df = pd.DataFrame({
        "trade_date": dates,
        "vol": vols,
        "close_qfq": closes_list,
    })
    result = calc._compute_indicators(df)

    divs = result["divergence"].dropna()
    if len(divs) > 0:
        # 获取所有 top_divergence 的索引
        top_indices = [i for i, v in enumerate(result["divergence"]) if v == "top_divergence"]
        if len(top_indices) >= 2:
            # 任意两个相邻标注之间差距 >= 5 天
            for j in range(1, len(top_indices)):
                gap = top_indices[j] - top_indices[j - 1]
                assert gap >= 5, \
                    f"Dedup failed: top_divergence repeated after {gap} days " \
                    f"(indices {top_indices[j-1]} → {top_indices[j]})"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_etl/test_calc_volume.py::test_volume_ratio -v
pytest tests/test_etl/test_calc_volume.py::test_volume_divergence_top -v
# 预期：FAIL（volume_ratio / divergence 列不存在或方法未定义）
```

#### 2.2 修改 VolumeCalculator

- [ ] **Step 3: 修改 calc_volume.py——扩展 SQL 查询，新增三个私有方法，扩展 _insert**

**3a: 修改 calculate() 的 SQL 查询**（第 24-36 行区域）

将 `SELECT trade_date, vol` 改为 `SELECT trade_date, vol, close_qfq`：

```python
# 日线查询（第 32-36 行）
df = self.con.execute(f"""
    SELECT trade_date, vol, close_qfq FROM {self.src_table}
    WHERE ts_code = ? AND is_suspended = 0
    ORDER BY trade_date
""", (ts_code,)).df()

# 周线查询（第 25-30 行）
df = self.con.execute(f"""
    SELECT d.trade_date, d.vol, d.close_qfq FROM {self.src_table} d
    JOIN dim_date dd ON d.trade_date = dd.trade_date
    WHERE d.ts_code = ? AND dd.is_week_end = 1
    ORDER BY d.trade_date
""", (ts_code,)).df()
```

**3b: 修改 _compute_indicators()**（第 42-57 行），增加三个新列的计算：

```python
def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
    v = df["vol"].values.astype(float)

    # MA5 volume
    df["ma_vol_5"] = sma(v, 5)

    # Volume ratio: vol / MA5_vol
    df["volume_ratio"] = self._compute_volume_ratio(df)

    # Percentile rank of MA5_vol within last 120 days
    df["pct_vol_rank"] = self._compute_pct_rank(df["ma_vol_5"].values, 120)

    # Zone: explosive / low_volume / normal
    df["zone"] = self._compute_zone(df)

    # Trend: linear regression slope on ln(raw_vol) over 10 days
    df["trend"] = self._compute_trend(df["vol"].values, 10)

    # Trend strength: de-unitized slope
    window = 10
    df["trend_strength"] = self._compute_trend_strength(df["vol"].values, window=window)

    # Divergence: vol vs close over 60-day window
    df["divergence"] = self._compute_divergence(df)

    return df
```

**3c: 新增 _compute_volume_ratio() 方法**（在 _compute_indicators 之后）：

```python
def _compute_volume_ratio(self, df: pd.DataFrame) -> np.ndarray:
    """volume_ratio = vol / MA5_vol. NaN where MA5 not available."""
    vol = df["vol"].values.astype(float)
    ma5 = df["ma_vol_5"].values.astype(float)
    result = np.full(len(vol), np.nan)
    for i in range(len(vol)):
        if pd.isna(ma5[i]) or ma5[i] <= 0:
            continue
        result[i] = vol[i] / ma5[i]
    return result
```

**3d: 新增 _compute_trend_strength() 方法**（复用 DDE 的加权回归 + 去量纲模式）：

```python
def _compute_trend_strength(self, vol_series: np.ndarray, window: int = 10) -> np.ndarray:
    """Volume trend strength via exponentially weighted linear regression.

    Formula: weighted_slope(ln(vol)) / mean(|ln(vol)|), unitless.
    Positive = volume expanding, negative = shrinking.
    Weighted regression (decay=0.20) gives recent bars ~3x more influence.
    """
    n = len(vol_series)
    result = np.full(n, np.nan)
    for i in range(window - 1, n):
        segment = vol_series[i - window + 1:i + 1]
        valid = segment[~np.isnan(segment)]
        valid_pos = valid[valid > 0]
        if len(valid_pos) < 5:
            continue
        log_segment = np.log(valid_pos)
        m = len(log_segment)
        x = np.arange(m, dtype=float)
        weights = np.exp(x * 0.20)
        try:
            slope = float(np.polyfit(x, log_segment, 1, w=weights)[0])
        except (np.linalg.LinAlgError, ValueError, TypeError):
            continue
        if not np.isfinite(slope):
            continue
        scale = np.mean(np.abs(log_segment))
        if scale < 1e-6:
            result[i] = 0.0
        else:
            result[i] = float(slope) / scale
    return result
```

**3e: 新增 _compute_divergence() 方法**（基于 MACD 模式，vol 替代 DIF）：

```python
def _compute_divergence(self, df: pd.DataFrame) -> list:
    """Top/bottom volume-price divergence using 60-day window.

    Confirmation day + 5-day dedup, same pattern as MACD/DDE divergence.
    - Top divergence: price near 60d high + vol has fallen from 60d peak
    - Bottom divergence: price near 60d low + vol has recovered from 60d valley
    """
    result = [None] * len(df)
    w = 59  # 60-bar window
    for i in range(w, len(df)):
        window_close = df["close_qfq"].iloc[i - w:i + 1]
        window_vol = df["vol"].iloc[i - w:i + 1]

        if window_vol.isna().any() or window_close.isna().any():
            continue

        c_hi = window_close.max()
        c_lo = window_close.min()
        v_hi = window_vol.max()
        v_lo = window_vol.min()
        cur_c = df["close_qfq"].iloc[i]
        cur_v = df["vol"].iloc[i]

        if pd.isna(cur_c) or pd.isna(cur_v):
            continue

        # Top divergence: price at 60d high, vol has fallen from 60d peak
        vol_peak_iloc = np.argmax(window_vol.values)
        vol_fallen = v_hi != 0 and cur_v < window_vol.values[vol_peak_iloc]
        price_near_peak = cur_c >= c_hi * 0.98

        if vol_peak_iloc < w and vol_fallen and price_near_peak:
            recent = any(
                result[j] == "top_divergence" for j in range(max(0, i - 5), i)
            )
            if not recent:
                result[i] = "top_divergence"

        # Bottom divergence: price at 60d low, vol has recovered from 60d valley
        vol_valley_iloc = np.argmin(window_vol.values)
        vol_recovered = v_lo != 0 and cur_v > window_vol.values[vol_valley_iloc]
        # Recovery must exceed 10% of valley absolute value
        vol_recovery_pct = (cur_v - v_lo) / abs(v_lo) if v_lo != 0 else 0
        vol_confirmed = vol_recovery_pct > 0.1
        # Price stopped falling: low >= 3 bars ago
        c_lo_iloc = np.argmin(window_close.values)
        price_stopped = (w - c_lo_iloc) >= 3
        price_near_bottom = cur_c <= c_lo * 1.02

        if (vol_valley_iloc < w and vol_recovered and vol_confirmed
                and price_stopped and price_near_bottom):
            recent = any(
                result[j] == "bottom_divergence" for j in range(max(0, i - 5), i)
            )
            if not recent:
                result[i] = "bottom_divergence"

    return result
```

**3f: 修改 _insert()**——扩展 dws_cols 和类型转换：

```python
def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
    """Batch insert all rows for one stock via DuckDB register."""
    dws_cols = ["ts_code", "trade_date", "ma_vol_5", "pct_vol_rank",
                "zone", "trend", "volume_ratio", "trend_strength",
                "divergence", "calc_date"]
    data_cols = dws_cols[1:]
    for c in data_cols:
        if c not in df.columns:
            df[c] = None
    batch = df[data_cols].copy()
    batch["ts_code"] = ts_code
    for c in ["ma_vol_5", "pct_vol_rank", "volume_ratio", "trend_strength"]:
        batch[c] = batch[c].apply(to_float_safe)
    batch["calc_date"] = batch["calc_date"].astype(str)
    batch = batch[dws_cols]
    self.con.register("_batch", batch)
    cols_sql = ", ".join(dws_cols)
    self.con.execute(
        f"INSERT OR REPLACE INTO {self.dws_table} ({cols_sql}) "
        f"SELECT {cols_sql} FROM _batch"
    )
    self.con.unregister("_batch")
```

- [ ] **Step 4: 运行单元测试确认扩展通过**（schema 更新前，仅测纯计算逻辑）

```bash
pytest tests/test_etl/test_calc_volume.py::test_volume_ratio \
  tests/test_etl/test_calc_volume.py::test_volume_divergence_top \
  tests/test_etl/test_calc_volume.py::test_volume_divergence_bottom \
  tests/test_etl/test_calc_volume.py::test_volume_divergence_dedup \
  tests/test_etl/test_calc_volume.py::test_volume_trend_strength -v
# 预期：5 passed
```

- [ ] **Step 5: 提交**

```bash
git add backend/etl/calc_volume.py tests/test_etl/test_calc_volume.py
git commit -m "feat: add volume_ratio, divergence, trend_strength to VolumeCalculator"
```

---

### Task 3: Schema — 新增 dws_price_position 表 + 扩展 volume 表 + ADS 视图

**Files:**
- Modify: `backend/db/schema.py`

#### 3.1 新增 dws_price_position DDL

- [ ] **Step 1: 在 _DWS_DDL dict 中新增 price_position 条目**

在 `backend/db/schema.py` 的 `_DWS_DDL` dict 中 `"volume"` 条目之后添加：

```python
    # 6.6 Price Position
    "price_position": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code              TEXT,
        trade_date           TEXT,
        price_position_60d   REAL,
        price_position_120d  REAL,
        price_position_250d  REAL,
        calc_date            TEXT,
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (price_position_60d >= 0 AND price_position_60d <= 100),
        CHECK (price_position_120d >= 0 AND price_position_120d <= 100),
        CHECK (price_position_250d >= 0 AND price_position_250d <= 100)
    )""",
```

#### 3.2 修改 volume 表的 CHECK 约束

- [ ] **Step 2: 修改 volume DDL**——在现有 CHECK 约束后添加新列的约束

将 `"volume"` 条目替换为：

```python
    "volume": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code        TEXT,
        trade_date     TEXT,
        ma_vol_5       REAL,
        pct_vol_rank   REAL,
        zone           TEXT,
        trend          TEXT,
        volume_ratio   REAL,
        trend_strength REAL,
        divergence     TEXT,
        calc_date      TEXT,
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (pct_vol_rank >= 0 AND pct_vol_rank <= 100),
        CHECK (zone IN ('explosive', 'low_volume', 'normal')),
        CHECK (trend IN ('expanding', 'shrinking', 'flat')),
        CHECK (divergence IN ('top_divergence', 'bottom_divergence') OR divergence IS NULL)
    )""",
```

#### 3.3 更新索引生成循环

- [ ] **Step 3: 将 "price_position" 加入索引和视图生成循环**

找到第 365 行的 `for _indicator in ["kpattern", "macd", "ma", "dde", "volume"]`，改为：

```python
for _indicator in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]:
```

找到第 403 行的 `for _indicator in ["kpattern", "macd", "ma", "dde", "volume"]`，改为：

```python
for _indicator in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]:
```

#### 3.4 扩展 ADS 视图

- [ ] **Step 4: 扩展 v_ads_analysis_wide_daily**

在 `v_ads_analysis_wide_daily` 中：

**4a:** 在 `FROM` 子句之前（`v.ma_vol_5, v.pct_vol_rank` 行之后），添加 price_position JOIN 和新量能列：

```sql
        v.ma_vol_5, v.pct_vol_rank,
        v.zone           AS vol_zone,
        v.trend          AS vol_trend,
        v.volume_ratio   AS vol_ratio,
        v.trend_strength AS vol_trend_strength,
        v.divergence     AS vol_divergence,

        pp.price_position_60d,
        pp.price_position_120d,
        pp.price_position_250d,
```

**4b:** 在 `FROM` 子句中添加 price_position JOIN：

```sql
    LEFT JOIN v_dws_volume_daily_latest       v ON c.ts_code = v.ts_code AND c.trade_date = v.trade_date
    LEFT JOIN v_dws_price_position_daily_latest pp ON c.ts_code = pp.ts_code AND c.trade_date = pp.trade_date
    LEFT JOIN dwd_daily_quote                 q ON c.ts_code = q.ts_code AND c.trade_date = q.trade_date
```

**4c:** 在 `CASE ... AS kpattern` 之前添加复合信号：

```sql
        -- Composite volume-price signals
        CASE
            WHEN q.close_qfq > (
                SELECT MAX(close_qfq) FROM dwd_daily_quote q2
                WHERE q2.ts_code = c.ts_code
                  AND q2.trade_date < c.trade_date
                  AND q2.trade_date >= (
                    SELECT MIN(trade_date) FROM dwd_daily_quote
                    WHERE ts_code = c.ts_code AND trade_date >= c.trade_date
                    LIMIT 1 OFFSET 59
                  )
            ) AND v.volume_ratio > 1.5
                THEN 'breakout_confirmed'
            WHEN pp.price_position_60d > 85 AND v.zone = 'explosive'
                 AND q.pct_chg BETWEEN -2 AND 2
                THEN 'volume_climax'
            WHEN pp.price_position_60d < 15 AND v.zone = 'low_volume'
                THEN 'volume_dry_up'
            WHEN c.turning_point = 'golden_cross' AND v.divergence = 'top_divergence'
                THEN 'golden_cross_weakened'
            WHEN c.turning_point = 'dead_cross' AND v.divergence = 'bottom_divergence'
                THEN 'dead_cross_weakened'
            ELSE NULL
        END              AS vol_signal,
```

- [ ] **Step 5: 同样扩展 v_ads_analysis_wide_weekly**——按 4a/4b/4c 模式改写周线视图

- [ ] **Step 6: 扩展 v_ads_index_wide 和 v_ads_index_wide_weekly**——添加新量能列和 price_position 列

- [ ] **Step 7: 运行 schema 创建测试**

```bash
python3 -c "
from backend.db.schema import create_all_tables
from backend.db.connection import get_connection
con = get_connection()
create_all_tables(con)
# 验证新表存在
tables = con.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%price_position%'\").fetchall()
print('price_position tables:', tables)
# 验证所有 12 张 DWS 表存在
dws_tables = con.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'dws_%'\").fetchall()
print(f'DWS tables ({len(dws_tables)}):', [t[0] for t in dws_tables])
con.close()
"
# 预期：输出包含 dws_price_position_daily, dws_price_position_weekly
# 预期：DWS 表共 12 张（原来的 10 + 新增 2）
```

- [ ] **Step 8: 提交**

```bash
git add backend/db/schema.py
git commit -m "feat: add dws_price_position tables, extend volume schema, add vol_signal to ADS views"
```

---

### Task 4: Orchestrator — 注册 PricePositionCalculator

**Files:**
- Modify: `backend/etl/orchestrator.py`

- [ ] **Step 1: 添加 import**

在第 29 行 `from backend.etl.calc_volume import VolumeCalculator` 之后：

```python
from backend.etl.calc_price_position import PricePositionCalculator
```

- [ ] **Step 2: 注册到 CALCULATORS**

将第 31-32 行的：

```python
CALCULATORS = [MACDCalculator, MACalculator, KPatternCalculator,
               DDECalculator, VolumeCalculator]
```

改为：

```python
CALCULATORS = [MACDCalculator, MACalculator, KPatternCalculator,
               DDECalculator, VolumeCalculator, PricePositionCalculator]
```

> **排序理由：** PricePositionCalculator 放在最后——它是其他模块的依赖项，但所有 DWS 模块互不依赖（各自独立读 DWD），所以顺序无关紧要。放在最后只影响 ETL 进度日志的输出顺序。

- [ ] **Step 3: 验证 orchestrator import 不报错**

```bash
python3 -c "from backend.etl.orchestrator import CALCULATORS; print([c.__name__ for c in CALCULATORS])"
# 预期：['MACDCalculator', 'MACalculator', 'KPatternCalculator', 'DDECalculator', 'VolumeCalculator', 'PricePositionCalculator']
```

- [ ] **Step 4: 提交**

```bash
git add backend/etl/orchestrator.py
git commit -m "feat: register PricePositionCalculator in ETL orchestrator"
```

---

### Task 5: Excel 导出 — 适配新列

**Files:**
- Modify: `backend/export_wide.py`

- [ ] **Step 1: 扩展 _COL_NAMES**——在 `"vol_zone"` / `"vol_trend"` 条目之后添加：

```python
    "volume_ratio": "量比", "vol_trend_strength": "量能趋势强度",
    "vol_divergence": "量价背离",
    "price_position_60d": "价格位置60日", "price_position_120d": "价格位置120日",
    "price_position_250d": "价格位置250日",
    "vol_signal": "量价复合信号",
```

- [ ] **Step 2: 扩展 _ENUM_VALUES**——添加新的枚举翻译：

```python
    "vol_divergence": {"top_divergence": "顶背离", "bottom_divergence": "底背离"},
    "vol_trend": {"expanding": "放量", "shrinking": "缩量", "flat": "平量"},
    "vol_signal": {
        "breakout_confirmed": "突破确认", "volume_climax": "放量滞涨",
        "volume_dry_up": "缩量止跌",
        "golden_cross_weakened": "金叉量弱", "dead_cross_weakened": "死叉量弱",
    },
```

- [ ] **Step 3: 扩展 _SIGNAL_COLS**——新列为信号列（NULL 显示为 "-"）：

```python
_SIGNAL_COLS = {"kpattern", "kpattern_strength", "macd_divergence", "macd_turning_point",
                "macd_alert", "ma_turning_point", "dde_alert", "dde_divergence",
                "vol_divergence", "vol_signal"}
```

- [ ] **Step 4: 扩展 _ROUND_2DP**——数值列保留 2 位小数：

```python
_ROUND_2DP = {"close", "pct_chg", "pe_ttm", "turnover_rate", "net_mf_amount",
              "volume_ratio", "vol_trend_strength",
              "price_position_60d", "price_position_120d", "price_position_250d"}
```

- [ ] **Step 5: 扩展信号排序**——在 `_reorder_signal_first()` 的 `signals` 列表中追加新列：

```python
    signals = [
        "kpattern", "kpattern_strength",
        "macd_divergence", "macd_zone", "macd_turning_point", "macd_alert", "macd_trend",
        "ma_alignment", "ma_turning_point", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope",
        "dde_trend", "dde_trend_strength", "dde_alert", "dde_divergence",
        "vol_zone", "vol_trend", "volume_ratio", "vol_trend_strength", "vol_divergence",
        "vol_signal",
        "price_position_60d", "price_position_120d", "price_position_250d",
    ]
```

- [ ] **Step 6: 扩展 _COL_GROUPS**——添加新列到各自组：

```python
    "volume":  {"5日均量(万手)", "量能百分位", "量能区域", "量能趋势",
                "量比", "量能趋势强度", "量价背离", "量价复合信号"},
    "price_pos": {"价格位置60日", "价格位置120日", "价格位置250日"},
```

- [ ] **Step 7: 添加 price_pos 组着色**——在 `_GROUP_TINTS` 中添加：

```python
    "price_pos": "1E8449",  # dark green-teal
```

- [ ] **Step 8: 运行导出验证**

```bash
python3 -c "
from backend.export_wide import export_wide_to_excel
n = export_wide_to_excel('data/tradeanalysis.duckdb', '20260530', 'exports/test_volume.xlsx')
print(f'Exported {n} rows')
"
# 预期：成功导出，不报 KeyError
```

- [ ] **Step 9: 提交**

```bash
git add backend/export_wide.py
git commit -m "feat: add volume ratio/divergence/trend_strength + price_position columns to Excel export"
```

---

### Task 6: 端到端验证

- [ ] **Step 1: 运行全部测试**

```bash
pytest tests/test_etl/test_calc_price_position.py tests/test_etl/test_calc_volume.py -v
# 预期：~14 passed（4 price_position + 10 volume）
```

- [ ] **Step 2: 运行已有测试确保无回归**

```bash
pytest tests/ -v
# 预期：全部 passing，无回归
```

- [ ] **Step 3: 单股完整流水线**

```bash
python3 -m backend.cli etl --step build-all --ts-code 000001.SZ --start 20260101
# 预期：PricePositionCalculator 出现在 calc_dws 日志中，无报错
```

- [ ] **Step 4: 验证新数据存在**

```bash
python3 -c "
import duckdb
con = duckdb.connect('data/tradeanalysis.duckdb')
# 验证 price_position 表有数据
n = con.execute(\"SELECT COUNT(*) FROM dws_price_position_daily WHERE ts_code='000001.SZ'\").fetchone()[0]
print(f'price_position rows: {n}')
assert n > 0, 'No price_position data'
# 验证 volume 新列有数据
cols = con.execute(\"SELECT column_name FROM information_schema.columns WHERE table_name='dws_volume_daily' AND column_name IN ('volume_ratio','divergence','trend_strength')\").fetchall()
print(f'New volume columns: {[c[0] for c in cols]}')
assert len(cols) == 3, 'Missing new volume columns'
# 验证 ADS 视图可查询
r = con.execute(\"SELECT vol_ratio, vol_divergence, price_position_60d FROM v_ads_analysis_wide_daily WHERE ts_code='000001.SZ' LIMIT 1\").fetchone()
print(f'ADS row: vol_ratio={r[0]}, vol_divergence={r[1]}, price_position_60d={r[2]}')
con.close()
print('ALL CHECKS PASSED')
"
```

- [ ] **Step 5: 提交最终验证**

```bash
git add -A
git commit -m "chore: end-to-end validation of volume optimization + price position infrastructure"
```

---

## 自检

- ✅ 6 个 Task，每个 2-15 个 step
- ✅ 每个代码 step 有完整代码（无 TBD / TODO）
- ✅ 每个测试 step 有完整测试代码
- ✅ 每个命令 step 有预期输出
- ✅ PricePositionCalculator 独立模块，不读任何其他 DWS 表
- ✅ VolumeCalculator 扩展只读 DWD 层，不跨 DWS 模块
- ✅ 复合信号（vol_signal）全部在 ADS 视图层，不进 Calculator
- ✅ Schema 改动包含 DDL + CHECK + 索引 + 视图的完整修改
- ✅ Excel 导出适配包含翻译、着色、列组、信号列注册
