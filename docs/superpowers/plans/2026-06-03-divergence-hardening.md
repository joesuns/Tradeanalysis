# 背离信号实战加固 — 实施计划

> **给执行者：** 使用 superpowers:subagent-driven-development（推荐）逐任务实施。每步 TDD：RED→GREEN→COMMIT。

**目标：** DDE 底背离修复（0→正常触发），MACD 底背离提质（78% 续跌率→<50%），所有背离 idx→iloc 架构加固。顶背离判定逻辑不变。

**架构：** 两个 Calculator 各改 `_compute_divergence` 方法。无新列、无 DDL 变更、无视图变更。

**技术栈：** Python 3.9, NumPy, DuckDB, pytest

---

## 文件结构

| 文件 | 改动 |
|------|------|
| `backend/etl/calc_macd.py:118-163` | idx→iloc + 底背离加回升确认 + 止跌确认 |
| `backend/etl/calc_dde.py:220-277` | idx→iloc + 底背离删尖刺过滤 + 回升确认 + 止跌确认 |
| `tests/test_etl/test_calc_macd.py` | +3 底背离测试 |
| `tests/test_etl/test_calc_dde.py` | +3 底背离测试 + 适配顶背离尖刺测试 |

---

### 任务 1: idx→iloc 架构加固（纯重构，行为不变）

**文件:**
- 修改: `backend/etl/calc_macd.py:118-163`
- 修改: `backend/etl/calc_dde.py:220-277`

- [ ] **步骤 1: 确认现有测试通过**

```bash
pytest tests/test_etl/test_calc_macd.py::test_macd_divergence_confirmation_day \
       tests/test_etl/test_calc_macd.py::test_macd_divergence_no_duplicate_within_5_days \
       tests/test_etl/test_calc_dde.py::test_dde_divergence_window_60 \
       tests/test_etl/test_calc_dde.py::test_dde_divergence_no_tie_false_positive \
       tests/test_etl/test_calc_dde.py::test_dde_divergence_uses_ddx \
       tests/test_etl/test_calc_dde.py::test_dde_divergence_spike_filtered -v
```

预期：6 passed。这些测试覆盖了所有背离判定路径，重构后必须全部保持通过。

- [ ] **步骤 2: MACD 重构 idx→iloc**

将 `backend/etl/calc_macd.py` 第 118-163 行的 `_compute_divergence` 中所有 `idxmax()/idxmin()` 替换为 iloc 位置运算：

```python
def _compute_divergence(self, df: pd.DataFrame) -> list:
    """Top/bottom divergence using 60-day window. Marked on confirmation day.

    Confirmation day = DIF has clearly rolled over from its 60d peak
    but price is still near its 60d high (within 2%).
    Deduplication: same type of divergence does not repeat within 5 bars.
    """
    result = [None] * len(df)
    w = 59  # 60-bar window: iloc[i-59 : i+1] = 60 elements
    for i in range(w, len(df)):
        window_close = df["close_qfq"].iloc[i - w : i + 1]
        window_dif = df["dif"].iloc[i - w : i + 1]
        c_hi = window_close.max()
        c_lo = window_close.min()
        d_hi = window_dif.max()
        d_lo = window_dif.min()
        cur_c = df["close_qfq"].iloc[i]
        cur_d = df["dif"].iloc[i]

        if pd.isna(cur_c) or pd.isna(cur_d):
            continue

        # Top divergence: DIF peaked in past, DIF has fallen from peak,
        #                price still near 60d high (within 2%).
        dif_peak_iloc = np.argmax(window_dif.values)
        dif_has_fallen = d_hi != 0 and cur_d < d_hi
        price_near_peak = cur_c >= c_hi * 0.98

        if dif_peak_iloc < w and dif_has_fallen and price_near_peak:
            recent = any(result[j] == "top_divergence" for j in range(max(0, i - 5), i))
            if not recent:
                result[i] = "top_divergence"

        # Bottom divergence: DIF valley in past, DIF has recovered from valley,
        #                   price still near 60d low (within 2%).
        dif_valley_iloc = np.argmin(window_dif.values)
        dif_has_recovered = d_lo != 0 and cur_d > d_lo
        price_near_bottom = cur_c <= c_lo * 1.02

        if dif_valley_iloc < w and dif_has_recovered and price_near_bottom:
            recent = any(result[j] == "bottom_divergence" for j in range(max(0, i - 5), i))
            if not recent:
                result[i] = "bottom_divergence"

    return result
```

关键变化：
- `window_dif.idxmax()` → `np.argmax(window_dif.values)` 得到 iloc 位置 (0~w)
- `window_dif.idxmin()` → `np.argmin(window_dif.values)` 得到 iloc 位置 (0~w)
- `dif_peak_idx < df.index[i]` → `dif_peak_iloc < w`（峰值不在窗口最后一根 = 不在今天）
- `dif_valley_idx < df.index[i]` → `dif_valley_iloc < w`（谷值不在今天）

- [ ] **步骤 3: DDE 重构 idx→iloc**

将 `backend/etl/calc_dde.py` 第 220-277 行做同样的 idx→iloc 替换。

顶背离段——`ddx_peak_iloc = np.argmax(window_ddx.values)`，后续 `peak_iloc` 直接用这个数值，不再需要 `window_ddx.index.get_loc()`：

```python
            # Top divergence: DDX peaked in past, has fallen from peak,
            #                price still near 60d high (within 2%).
            ddx_peak_iloc = np.argmax(window_ddx.values)
            ddx_peak_val = window_ddx.max()
            ddx_fallen = d_hi != 0 and cur_d < d_hi
            price_near_peak = cur_c >= c_hi * 0.98

            # 邻域确认：峰值不是孤立的单日尖刺
            neighbors = window_ddx.values[
                max(0, ddx_peak_iloc - 2):min(len(window_ddx), ddx_peak_iloc + 3)
            ]
            is_spike = (neighbors >= ddx_peak_val * 0.8).sum() < 2

            if ddx_peak_iloc < w and ddx_fallen and not is_spike and price_near_peak:
                ...
```

底背离段——同样用 `np.argmin(window_ddx.values)`，valley_spike 判定保留（Task 3 会删除）：

```python
            # Bottom divergence: DDX valley in past, has recovered from valley,
            #                   price still near 60d low (within 2%).
            ddx_valley_iloc = np.argmin(window_ddx.values)
            ddx_valley_val = window_ddx.min()
            ddx_recovered = d_lo != 0 and cur_d > d_lo
            price_near_bottom = cur_c <= c_lo * 1.02

            # 邻域确认：谷值不是孤立的单日尖刺
            v_neighbors = window_ddx.values[
                max(0, ddx_valley_iloc - 2):min(len(window_ddx), ddx_valley_iloc + 3)
            ]
            is_valley_spike = (v_neighbors <= ddx_valley_val * 1.2).sum() < 2

            if ddx_valley_iloc < w and ddx_recovered and not is_valley_spike and price_near_bottom:
                ...
```

- [ ] **步骤 4: 确认重构后测试全绿**

```bash
pytest tests/test_etl/test_calc_macd.py tests/test_etl/test_calc_dde.py -v
```

预期：全部通过（MACD 26 + DDE 13 = 39 passed），行为不变。

- [ ] **步骤 5: 提交**

```bash
git add backend/etl/calc_macd.py backend/etl/calc_dde.py
git commit -m "refactor: 背离 idxmax/idxmin → iloc 位置，消除 index 连续性假设"
```

---

### 任务 2: MACD 底背离加 DIF 回升确认 + 价格止跌确认

**文件:**
- 修改: `backend/etl/calc_macd.py:_compute_divergence` 底背离段
- 修改: `tests/test_etl/test_calc_macd.py`

- [ ] **步骤 1: 写失败的测试**

在 `test_macd_divergence_no_duplicate_within_5_days` 之后添加：

```python
def test_macd_bottom_div_recovery_strong_triggers():
    """DIF 回升 > 10% + 价格低点 3 天前 → 底背离触发。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    n = 68
    close = np.full(n, 10.0)
    dif = np.full(n, 0.5)
    # 价格跌到 day 57，DIF 谷值在 day 55，然后 DIF 回升 > 10%
    for i in range(30, 58):
        close[i] = 10.0 - (i - 30) * 0.1    # 持续下跌
    for i in range(30, 56):
        dif[i] = 0.5 - (i - 30) * 0.05      # DIF 跟随下跌
    dif[55] = -0.75   # DIF 谷值
    dif[56] = -0.60   # DIF 回升 20%
    dif[57] = -0.50
    # day 58-67: 价格在低点附近横盘，DIF 持续回升
    for i in range(58, n):
        close[i] = close[57] * 1.005         # 价格微涨，距低点 2% 内
        dif[i] = dif[i-1] + 0.05             # DIF 持续回升

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "dif": dif,
    })
    result = calc._compute_divergence(df)
    # day 62: 低点已过去 5 天, DIF 回升 > 10%
    assert result[62] == "bottom_divergence", (
        f"DIF 回升 > 10% + 价格止跌应触发底背离，实际 {result[62]}"
    )


def test_macd_bottom_div_recovery_weak_not_triggers():
    """DIF 回升 < 10% → 底背离不触发。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    n = 68
    close = np.full(n, 10.0)
    dif = np.full(n, 0.5)
    for i in range(30, 58):
        close[i] = 10.0 - (i - 30) * 0.1
    for i in range(30, 56):
        dif[i] = 0.5 - (i - 30) * 0.05
    dif[55] = -0.75   # DIF 谷值
    dif[56] = -0.74   # DIF 仅回升 1.3%
    for i in range(57, n):
        close[i] = close[56] * 1.005
        dif[i] = dif[i-1] + 0.002

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "dif": dif,
    })
    result = calc._compute_divergence(df)
    # DIF 回升不足 10%，不触发
    for i in range(60, n):
        assert result[i] != "bottom_divergence", (
            f"DIF 回升 < 10% 不应触发，idx {i} 实际 {result[i]}"
        )


def test_macd_bottom_div_price_still_falling_not_triggers():
    """价格仍在创新低 → 底背离不触发。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    n = 68
    close = np.full(n, 10.0)
    dif = np.full(n, 0.5)
    for i in range(30, n):
        close[i] = 10.0 - (i - 30) * 0.1    # 持续下跌不反弹
    for i in range(30, 56):
        dif[i] = 0.5 - (i - 30) * 0.05
    dif[55] = -0.75   # DIF 谷值
    for i in range(56, n):
        dif[i] = dif[i-1] + 0.03             # DIF 在回升

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "dif": dif,
    })
    result = calc._compute_divergence(df)
    # 价格还在创新低，即使 DIF 回升也不触发
    for i in range(60, n):
        assert result[i] != "bottom_divergence", (
            f"价格仍在创新低不应触发，idx {i} 实际 {result[i]}"
        )
```

- [ ] **步骤 2: 确认失败**

```bash
pytest tests/test_etl/test_calc_macd.py::test_macd_bottom_div_recovery_strong_triggers \
       tests/test_etl/test_calc_macd.py::test_macd_bottom_div_recovery_weak_not_triggers \
       tests/test_etl/test_calc_macd.py::test_macd_bottom_div_price_still_falling_not_triggers -v
```

预期：`test_macd_bottom_div_recovery_strong_triggers` PASS（旧逻辑不加回升条件也能触发），`test_macd_bottom_div_recovery_weak_not_triggers` FAIL（旧逻辑不检查回升幅度），`test_macd_bottom_div_price_still_falling_not_triggers` FAIL（旧逻辑不检查止跌）。

- [ ] **步骤 3: 修改 MACD 底背离段**

在 Task 1 重构后的代码基础上，修改底背离段（约第 155-165 行）：

```python
        # Bottom divergence: DIF valley in past, DIF has recovered >10%,
        #                   price stopped falling (low >= 3 bars ago).
        dif_valley_iloc = np.argmin(window_dif.values)
        dif_valley_val = window_dif.min()
        dif_has_recovered = d_lo != 0 and cur_d > d_lo
        # 回升确认：DIF 回升幅度 > 谷值绝对值的 10%
        dif_recovery_pct = (cur_d - d_lo) / abs(d_lo) if d_lo != 0 else 0
        dif_confirmed = dif_recovery_pct > 0.1

        # 价格止跌确认：60日低点距今 >= 3 根 bar
        c_lo_iloc = np.argmin(window_close.values)
        price_stopped = (w - c_lo_iloc) >= 3

        price_near_bottom = cur_c <= c_lo * 1.02

        if (dif_valley_iloc < w and dif_has_recovered and dif_confirmed
                and price_stopped and price_near_bottom):
            recent = any(result[j] == "bottom_divergence" for j in range(max(0, i - 5), i))
            if not recent:
                result[i] = "bottom_divergence"
```

顶背离段不变。

- [ ] **步骤 4: 确认通过**

```bash
pytest tests/test_etl/test_calc_macd.py -v
```

预期：29 passed（原 26 + 新增 3）。

- [ ] **步骤 5: 提交**

```bash
git add backend/etl/calc_macd.py tests/test_etl/test_calc_macd.py
git commit -m "feat: MACD 底背离加 DIF 回升>10% + 价格止跌>=3天确认"
```

---

### 任务 3: DDE 底背离修复 + 确认

**文件:**
- 修改: `backend/etl/calc_dde.py:_compute_divergence` 底背离段
- 修改: `tests/test_etl/test_calc_dde.py`

- [ ] **步骤 1: 写失败的测试**

在 `test_dde_divergence_spike_filtered` 之后添加：

```python
def test_dde_bottom_div_triggers():
    """DDE 底背离：DDX 谷值回升 > 10% + 价格止跌 → 触发。"""
    calc = DDECalculator.__new__(DDECalculator)
    n = 68
    close = np.full(n, 10.0)
    ddx = np.full(n, 0.05)
    for i in range(30, 58):
        close[i] = 10.0 - (i - 30) * 0.1
    for i in range(30, 56):
        ddx[i] = 0.05 - (i - 30) * 0.01      # DDX 下跌
    ddx[55] = -0.20   # DDX 谷值
    for i in range(56, n):
        ddx[i] = ddx[i-1] + 0.04              # DDX 快速回升
        close[i] = close[57] * 1.005          # 价格在低点附近

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "ddx": ddx,
    })
    result = calc._compute_divergence(df)
    any_div = any(r == "bottom_divergence" for r in result[60:])
    assert any_div, "DDX 谷值回升 + 价格止跌应触发底背离"


def test_dde_top_div_spike_still_filtered():
    """顶背离尖刺过滤仍然有效。"""
    calc = DDECalculator.__new__(DDECalculator)
    n = 68
    close = np.full(n, 10.0)
    ddx = np.full(n, 0.05)
    ddx[55] = 0.50  # 单日尖刺
    for i in range(30, 61):
        close[i] = 10.0 + (i - 30) * 0.1
    for i in range(61, n):
        close[i] = close[60] * 0.99

    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": close, "ddx": ddx,
    })
    result = calc._compute_divergence(df)
    for i in range(55, 60):
        assert result[i] != "top_divergence", (
            f"顶背离尖刺过滤应仍然有效，idx {i} 实际 {result[i]}"
        )
```

- [ ] **步骤 2: 确认失败**

```bash
pytest tests/test_etl/test_calc_dde.py::test_dde_bottom_div_triggers -v
```

预期：FAIL — 当前底背离 0 触发。

- [ ] **步骤 3: 修改 DDE 底背离段**

在 Task 1 重构后的代码基础上，修改底背离段：

```python
        # Bottom divergence: DDX valley in past, has recovered >10%,
        #                   price stopped falling (low >= 3 bars ago).
        ddx_valley_iloc = np.argmin(window_ddx.values)
        ddx_valley_val = window_ddx.min()
        ddx_recovered = d_lo != 0 and cur_d > d_lo
        # 回升确认：DDX 回升幅度 > 谷值绝对值的 10%
        ddx_recovery_pct = (cur_d - d_lo) / abs(d_lo) if d_lo != 0 else 0
        ddx_confirmed = ddx_recovery_pct > 0.1

        # 价格止跌确认：60日低点距今 >= 3 根 bar
        c_lo_iloc = np.argmin(window_close.values)
        price_stopped = (w - c_lo_iloc) >= 3

        price_near_bottom = cur_c <= c_lo * 1.02

        if (ddx_valley_iloc < w and ddx_recovered and ddx_confirmed
                and price_stopped and price_near_bottom):
            recent = any(result[j] == "bottom_divergence" for j in range(max(0, i - 5), i))
            if not recent:
                result[i] = "bottom_divergence"
```

关键变化：
- 删除 `is_valley_spike` 尖刺过滤（六行：valley_iloc 计算、v_neighbors 切片、is_valley_spike 判定、not is_valley_spike 条件）
- 新增 `ddx_recovery_pct > 0.1` 回升确认
- 新增 `price_stopped >= 3` 止跌确认

顶背离段（含 `is_spike`）完全不变。

- [ ] **步骤 4: 确认通过**

```bash
pytest tests/test_etl/test_calc_dde.py -v
```

预期：15 passed（原 13 + 新增 2）。

- [ ] **步骤 5: 提交**

```bash
git add backend/etl/calc_dde.py tests/test_etl/test_calc_dde.py
git commit -m "feat: DDE 底背离删尖刺过滤 + 加 DDX回升>10% + 价格止跌确认"
```

---

### 任务 4: 端到端验证

- [ ] **步骤 1: 全量指标测试**

```bash
pytest tests/test_etl/test_calc_*.py -v
```

预期：~76 passed（原 71 + 新增 5）。

- [ ] **步骤 2: 背离信号分布检查**

```bash
python3 -c "
import duckdb
con = duckdb.connect('./data/tradeanalysis.duckdb', read_only=True)
r = con.execute('''
    SELECT divergence, COUNT(*) FROM v_dws_macd_daily_latest
    WHERE divergence IS NOT NULL GROUP BY 1
''').df()
print('MACD:', r.to_string())
r2 = con.execute('''
    SELECT divergence, COUNT(*) FROM v_dws_dde_daily_latest
    WHERE divergence IS NOT NULL GROUP BY 1
''').df()
print('DDE:', r2.to_string())
# 期望：DDE 底背离从 0 变为 >0
"
```

- [ ] **步骤 3: 提交验证**

```bash
git add -A && git commit -m "verify: 背离实战加固端到端验证通过"
```

---

## 自检

**1. 覆盖检查：**
- ✅ 任务 1: idx→iloc 重构，行为不变
- ✅ 任务 2: MACD 底背离 + DIF 回升 10% + 价格止跌 3 天
- ✅ 任务 3: DDE 底背离 - 尖刺过滤 + DDX 回升 10% + 价格止跌 3 天
- ✅ 任务 4: 端到端验证
- ✅ 顶背离逻辑完全不变

**2. 占位符扫描：** 无。

**3. 类型一致性：**
- `np.argmax/np.argmin` 返回 int（iloc 位置），比较 `iloc < w`（w=59）正确
- `dif_recovery_pct > 0.1` 即 10%，当 d_lo=0 时 recovery_pct=0，不触发（正确）
- `(w - c_lo_iloc)` 给出低点距窗口末尾的 bar 数，≥3 表示至少 3 天前
