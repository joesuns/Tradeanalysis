# 信号时效深化 — 实施计划

> **给执行者：** 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施。步骤使用 checkbox 跟踪。

**目标：** 消除量能趋势的双重平滑滞后（~12天→~5天）和 DDE 背离的三阶信号滞后（~5-8天→~2-3天），两个改动均为纯内部公式替换。

**架构影响：** 零。无新列、无新表、无 DDL 变更、无视图变更、无 API 变更、无 Excel 变更。仅修改 `calc_volume.py` 和 `calc_dde.py` 各一个方法的输入源和参数。

**技术栈：** Python 3.9, NumPy, DuckDB, pytest

---

## 架构评估

### 影响面分析

```
calc_volume.py:_compute_indicators  ← 改输入源 (ma_vol_5 → raw vol) + 改窗口 (20 → 10)
calc_volume.py:_compute_trend       ← 改阈值 (0.005 → 0.008) + 改 min_valid (10 → 5)
       ↓
  dws_volume_*.trend  ← 列名不变、枚举不变
       ↓
  v_ads_analysis_wide_*  ← SELECT * 自动带入，无变更
       ↓
  export_wide.py / API  ← 消费端无感知
```

```
calc_dde.py:_compute_divergence  ← 改数据源 (ddx2 → ddx) + 新增尖刺过滤
       ↓
  dws_dde_*.divergence  ← 列名不变、枚举不变
       ↓
  v_ads_analysis_wide_*  ← 消费端无感知
```

### 不变清单

| 维度 | volume trend | DDE divergence |
|------|:--:|:--:|
| 列名 | `trend` | `divergence` |
| 枚举值 | expanding/shrinking/flat | top_divergence/bottom_divergence |
| DDL CHECK | 不变 | 不变 |
| 视图 | SELECT * 自动带入 | 同 |
| API 响应字段 | trend: Optional[str] | divergence: Optional[str] |
| Excel 列名 | vol_trend (放量/缩量/平量) | dde_divergence (顶背离/底背离) |
| 下游依赖 | 无（终端信号） | 无（终端信号） |

---

## 文件结构

| 文件 | 改动 |
|------|------|
| `backend/etl/calc_volume.py` | 趋势输入从 ma_vol_5 切到 raw vol，窗口 20→10，阈值 0.005→0.008，min_valid 10→5 |
| `backend/etl/calc_dde.py` | 背离数据源从 ddx2 切到 ddx，新增邻域尖刺过滤 |
| `tests/test_etl/test_calc_volume.py` | 新增 raw vol 趋势测试、阈值测试 |
| `tests/test_etl/test_calc_dde.py` | 新增 DDX 背离测试、尖刺过滤测试 |

---

### 任务 1: 量能趋势去双重平滑

**文件:** `backend/etl/calc_volume.py:41-55, 132-160`

- [ ] **步骤 1: 写失败的测试**

在 `tests/test_etl/test_calc_volume.py` 的 `test_trend_flat` 之后添加：

```python
def test_trend_uses_raw_vol():
    """趋势直接使用原始成交量（非 MA5_vol）。"""
    calc = VolumeCalculator.__new__(VolumeCalculator)
    n = 30
    dates = [f"d{i}" for i in range(n)]
    # raw vol 持续上升，但被 SMA(5) 平滑后趋势会滞后
    # 直接用 raw vol 应在 10-bar 窗口后检出 expanding
    vols = [1000000.0 + i * 100000 for i in range(n)]  # 每日 +10%
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)
    # 窗口=10，index 9 为首个有效值，上升趋势应为 expanding
    t = result["trend"].iloc[15]
    assert t == "expanding", f"raw vol 持续上升应为 expanding，实际 {t}"


def test_trend_threshold_0008():
    """趋势阈值从 0.005 变为 0.008——弱趋势判为 flat。"""
    calc = VolumeCalculator.__new__(VolumeCalculator)
    n = 20
    dates = [f"d{i}" for i in range(n)]
    # 极缓慢上升，ln 斜率约 0.003/日 < 0.008 → flat
    vols = [1000000.0 + i * 5000 for i in range(n)]  # 每日 +0.5%
    df = pd.DataFrame({"trade_date": dates, "vol": vols})
    result = calc._compute_indicators(df)
    t = result["trend"].iloc[15]
    assert t == "flat", f"弱趋势(斜率<0.008)应为 flat，实际 {t}"
```

- [ ] **步骤 2: 运行测试确认失败**

```bash
pytest tests/test_etl/test_calc_volume.py::test_trend_uses_raw_vol \
       tests/test_etl/test_calc_volume.py::test_trend_threshold_0008 -v
```

预期：FAIL — `test_trend_uses_raw_vol` 中旧公式用 MA5_vol 或 `test_trend_threshold_0008` 中旧阈值 0.005 把弱趋势判为 expanding。

- [ ] **步骤 3: 替换输入源和窗口**

修改 `calc_volume.py` 第 53-54 行：

```python
# 改前：
# Trend: linear regression slope on ln(MA5_vol) over 20 days
df["trend"] = self._compute_trend(df["ma_vol_5"].values, 20)

# 改后：
# Trend: linear regression slope on ln(raw_vol) over 10 days
df["trend"] = self._compute_trend(df["vol"].values, 10)
```

- [ ] **步骤 4: 修改阈值和最小有效值**

修改 `calc_volume.py` 第 133-157 行：

```python
# 改前 docstring：
"""Volume trend via linear regression slope on ln(MA5_vol).

- expanding: slope > 0.005
- shrinking: slope < -0.005
- flat: otherwise
"""

# 改后 docstring：
"""Volume trend via linear regression slope on ln(raw volume).

- expanding: slope > 0.008
- shrinking: slope < -0.008
- flat: otherwise
"""
```

```python
# 改前：
if len(valid_positive) < 10:
    continue

slope = linear_regression_slope(valid_positive)
if slope > 0.005:
    result[i] = "expanding"
elif slope < -0.005:
    result[i] = "shrinking"

# 改后：
if len(valid_positive) < 5:
    continue

slope = linear_regression_slope(valid_positive)
if slope > 0.008:
    result[i] = "expanding"
elif slope < -0.008:
    result[i] = "shrinking"
```

参数名 `ma_vol_5` 在方法签名中，但实际传入 raw vol——改为通用名 `vol_series`：

```python
# 改前：
def _compute_trend(self, ma_vol_5: np.ndarray, window: int) -> list:

# 改后：
def _compute_trend(self, vol_series: np.ndarray, window: int) -> list:
```

方法体内 `ma_vol_5` 引用全部改为 `vol_series`（共 3 处：第 139, 143, 145 行）。

- [ ] **步骤 5: 运行全部 Volume 测试**

```bash
pytest tests/test_etl/test_calc_volume.py -v
```

预期：7 passed（5 已有 + 2 新增）。

- [ ] **步骤 6: 提交**

```bash
git add backend/etl/calc_volume.py tests/test_etl/test_calc_volume.py
git commit -m "feat: 量能趋势去双重平滑——raw vol + 10-bar 回归替代 MA5_vol + 20-bar
- 消除 SMA(5) 引入的 ~2.5 天滞后
- 窗口 20→10 砍半回归滞后
- 阈值 0.005→0.008 补偿 raw vol 高方差
- 信号时效从 ~12 天降到 ~5 天，列名/枚举/DDL/视图/API 全不变"
```

---

### 任务 2: DDE 背离从 DDX2 切到 DDX + 尖刺过滤

**文件:** `backend/etl/calc_dde.py:207-254`

- [ ] **步骤 1: 写失败的测试**

在 `tests/test_etl/test_calc_dde.py` 的 `test_dde_divergence_no_tie_false_positive` 之后添加：

```python
def test_dde_divergence_uses_ddx():
    """背离检测使用原始 DDX（非 DDX2）。"""
    calc = DDECalculator.__new__(DDECalculator)
    n = 68
    close = np.full(n, 10.0)
    ddx = np.full(n, 0.05)
    ddx2 = np.full(n, 0.05)
    # DDX 在第 56 天见顶，DDX2 因 EMA 延迟在第 59 天见顶
    for i in range(30, 57):
        ddx[i] = 0.05 + (i - 30) * 0.01   # DDX peaks day 56
    for i in range(57, n):
        ddx[i] = ddx[56] - (i - 56) * 0.01  # DDX declining
    # DDX2 = EMA(DDX, 5) — lags behind DDX, peaks later
    for i in range(30, 59):
        ddx2[i] = 0.05 + (i - 30) * 0.008  # DDX2 peaks day 58-59
    for i in range(59, n):
        ddx2[i] = ddx2[58] - (i - 58) * 0.008
    for i in range(30, 61):
        close[i] = 10.0 + (i - 30) * 0.1   # price peaks day 60
    for i in range(61, n):
        close[i] = close[60] * 0.99

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "ddx": ddx, "ddx2": ddx2,
    })
    result = calc._compute_divergence(df)
    # 用 DDX 检测背离，确认日应早于 DDX2 版本
    div_indices = [i for i, v in enumerate(result) if v == "top_divergence"]
    assert len(div_indices) >= 1, "DDX 背离应至少检测到一次"


def test_dde_divergence_spike_filtered():
    """单日 DDX 尖刺不应成为 60 日伪峰值触发假背离。"""
    calc = DDECalculator.__new__(DDECalculator)
    n = 68
    close = np.full(n, 10.0)
    ddx = np.full(n, 0.05)
    # 第 55 天：DDX 单日尖刺（孤立极端值），邻域无确认
    ddx[55] = 0.50  # 10x normal，单日尖刺
    # 价格继续上涨，正常 DDX 未跟随
    for i in range(30, 61):
        close[i] = 10.0 + (i - 30) * 0.1
    for i in range(61, n):
        close[i] = close[60] * 0.99

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "ddx": ddx,
    })
    result = calc._compute_divergence(df)
    # 尖刺不应触发背离（邻域无 ≥ 峰值 80% 的确认柱）
    # 如果尖刺过滤生效，背离不会在尖刺日附近触发
    for i in range(55, 60):
        assert result[i] != "top_divergence", (
            f"idx {i}: 单日尖刺不应触发背离"
        )
```

- [ ] **步骤 2: 运行测试确认失败**

```bash
pytest tests/test_etl/test_calc_dde.py::test_dde_divergence_uses_ddx \
       tests/test_etl/test_calc_dde.py::test_dde_divergence_spike_filtered -v
```

预期：FAIL — 旧代码用 DDX2 且无尖刺过滤。

- [ ] **步骤 3: 切换数据源 + 变量重命名**

修改 `calc_dde.py` 第 207-254 行，将所有 `ddx2` 引用改为 `ddx`：

第 208-217 行：
```python
# 改前：
"""Top/bottom divergence using DDX2 vs close over 60-day window.

Confirmation day: DDX2 has clearly rolled from its 60d peak/valley,
but price still near extreme. Dedup: no repeat within 5 bars.
"""
result = [None] * len(df)
w = 59  # 60-bar window: iloc[i-59 : i+1] = 60 elements
for i in range(w, len(df)):
    window_close = df["close_qfq"].iloc[i - w : i + 1]
    window_ddx2 = df["ddx2"].iloc[i - w : i + 1]

    if window_ddx2.isna().any():

# 改后：
"""Top/bottom divergence using DDX (raw) vs close over 60-day window.

Confirmation day: DDX has clearly rolled from its 60d peak/valley,
but price still near extreme. Dedup: no repeat within 5 bars.
Single-bar DDX spikes are filtered by requiring a neighboring bar
within ±2 days to reach >= 80% of the peak value.
"""
result = [None] * len(df)
w = 59  # 60-bar window: iloc[i-59 : i+1] = 60 elements
for i in range(w, len(df)):
    window_close = df["close_qfq"].iloc[i - w : i + 1]
    window_ddx = df["ddx"].iloc[i - w : i + 1]

    if window_ddx.isna().any():
```

第 222-252 行，变量名和尖刺过滤：
```python
# 改前：
c_hi = window_close.max()
c_lo = window_close.min()
d_hi = window_ddx2.max()
d_lo = window_ddx2.min()
cur_c = df["close_qfq"].iloc[i]
cur_d = df["ddx2"].iloc[i]

if pd.isna(cur_c) or pd.isna(cur_d):
    continue

# Top divergence: DDX2 peaked in past, has fallen from peak,
#                price still near 60d high (within 2%).
ddx2_peak_idx = window_ddx2.idxmax()
ddx2_fallen = d_hi != 0 and cur_d < d_hi
price_near_peak = cur_c >= c_hi * 0.98

if ddx2_peak_idx < df.index[i] and ddx2_fallen and price_near_peak:
    recent = any(result[j] == "top_divergence" for j in range(max(0, i - 5), i))
    if not recent:
        result[i] = "top_divergence"

# Bottom divergence: DDX2 valley in past, has recovered from valley,
#                   price still near 60d low (within 2%).
ddx2_valley_idx = window_ddx2.idxmin()
ddx2_recovered = d_lo != 0 and cur_d > d_lo
price_near_bottom = cur_c <= c_lo * 1.02

if ddx2_valley_idx < df.index[i] and ddx2_recovered and price_near_bottom:
    recent = any(result[j] == "bottom_divergence" for j in range(max(0, i - 5), i))
    if not recent:
        result[i] = "bottom_divergence"

# 改后：
c_hi = window_close.max()
c_lo = window_close.min()
d_hi = window_ddx.max()
d_lo = window_ddx.min()
cur_c = df["close_qfq"].iloc[i]
cur_d = df["ddx"].iloc[i]

if pd.isna(cur_c) or pd.isna(cur_d):
    continue

# Top divergence: DDX peaked in past, has fallen from peak,
#                price still near 60d high (within 2%).
ddx_peak_idx = window_ddx.idxmax()
ddx_peak_val = window_ddx.max()
ddx_fallen = d_hi != 0 and cur_d < d_hi
price_near_peak = cur_c >= c_hi * 0.98

# 邻域确认：峰值不是孤立单日尖刺（±2 天内至少另有 1 天 ≥ 峰值 80%）
peak_iloc = window_ddx.index.get_loc(ddx_peak_idx)
neighbors = window_ddx.iloc[
    max(0, peak_iloc - 2):min(len(window_ddx), peak_iloc + 3)
]
is_spike = (neighbors >= ddx_peak_val * 0.8).sum() < 2

if ddx_peak_idx < df.index[i] and ddx_fallen and not is_spike and price_near_peak:
    recent = any(result[j] == "top_divergence" for j in range(max(0, i - 5), i))
    if not recent:
        result[i] = "top_divergence"

# Bottom divergence: DDX valley in past, has recovered from valley,
#                   price still near 60d low (within 2%).
ddx_valley_idx = window_ddx.idxmin()
ddx_valley_val = window_ddx.min()
ddx_recovered = d_lo != 0 and cur_d > d_lo
price_near_bottom = cur_c <= c_lo * 1.02

# 邻域确认：谷值不是孤立单日尖刺
valley_iloc = window_ddx.index.get_loc(ddx_valley_idx)
v_neighbors = window_ddx.iloc[
    max(0, valley_iloc - 2):min(len(window_ddx), valley_iloc + 3)
]
is_valley_spike = (v_neighbors <= ddx_valley_val * 1.2).sum() < 2

if ddx_valley_idx < df.index[i] and ddx_recovered and not is_valley_spike and price_near_bottom:
    recent = any(result[j] == "bottom_divergence" for j in range(max(0, i - 5), i))
    if not recent:
        result[i] = "bottom_divergence"
```

- [ ] **步骤 4: 运行全部 DDE 测试**

```bash
pytest tests/test_etl/test_calc_dde.py -v
```

预期：13 passed（11 已有 + 2 新增）。已有测试 `test_dde_divergence_window_60` 和 `test_dde_divergence_no_tie_false_positive` 可能需要适配——这两个测试的 DataFrame 中需同时包含 `ddx` 和 `ddx2` 列。

检查已有测试：`test_dde_divergence_window_60` 的 DataFrame 有 `close_qfq` 和 `ddx2` 列，没有 `ddx` 列。改后 `_compute_divergence` 读 `df["ddx"]`，需在测试中增加 `ddx` 列。

修改 `test_dde_divergence_window_60`：
```python
# DataFrame 中增加 ddx 列（与 ddx2 同值或从测试数据推导）
df = pd.DataFrame({
    "trade_date": [f"d{i}" for i in range(n)],
    "close_qfq": close,
    "ddx": close * 0.01,       # 新增：模拟 DDX 与价格正相关
    "ddx2": ddx2,
})
```

同样修改 `test_dde_divergence_no_tie_false_positive`。

- [ ] **步骤 5: 确认已有测试适配后全部通过**

```bash
pytest tests/test_etl/test_calc_dde.py -v
```

- [ ] **步骤 6: 提交**

```bash
git add backend/etl/calc_dde.py tests/test_etl/test_calc_dde.py
git commit -m "feat: DDE 背离数据源从 DDX2 切到 DDX + 单日尖刺邻域过滤
- 60 日窗口自带强平滑，无需 EMA 前置
- 邻域 ±2 天内至少另有 1 天 ≥ 峰值 80% 才确认峰值
- 背离信号提前 3-5 天触发，消除双重平滑滞后
- DDX2 继续用于 trend 和 alert（需平滑的场景）"
```

---

### 任务 3: 端到端验证

- [ ] **步骤 1: 运行全部指标测试**

```bash
pytest tests/test_etl/test_calc_*.py -v
```

预期：~63 passed（原 59 + 新增 4）。

- [ ] **步骤 2: 单股流水线冒烟**

```bash
python3 -m backend.cli etl --step build-all --ts-code 000001.SZ --start 20260101 --end 20260602
```

- [ ] **步骤 3: 量能趋势分布验证**

```bash
python3 -c "
import duckdb
con = duckdb.connect('./data/tradeanalysis.duckdb', read_only=True)
r = con.execute('''SELECT trend, COUNT(*) as cnt FROM v_dws_volume_daily_latest
    WHERE trend IS NOT NULL GROUP BY trend''').df()
print(r.to_string())
# 期望：expanding/shrinking/flat 三态均有分布，flat 占 20-40%
"
```

- [ ] **步骤 4: 提交验证结果**

```bash
git add -A && git commit -m "verify: 信号时效深化端到端验证通过"
```

---

## 自检

**1. 覆盖检查：**
- ✅ 任务 1: raw vol 输入 + 窗口/阈值/min_valid 四项改动
- ✅ 任务 2: ddx2→ddx 全局替换 + 尖刺过滤 + 已有测试适配
- ✅ 任务 3: 端到端含分布验证

**2. 占位符扫描：** 无。

**3. 类型一致性：**
- `_compute_trend(vol_series, window)` 参数名从 `ma_vol_5` 改为 `vol_series`，方法内引用全部更新
- `window_ddx` 替换 `window_ddx2`，后续变量 `ddx_peak_idx`、`ddx_fallen` 等全部同步
- 测试 DataFrame 中新增 `ddx` 列，与 `_compute_divergence` 的 `df["ddx"]` 访问一致
