# 信号时效性优化 — 实施计划

> **给执行者：** 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施。步骤使用 checkbox（`- [ ]`）跟踪。

**目标：** 调整时间窗口、收敛检测方式、市场状态覆盖和趋势上下文过滤，将四项滞后指标信号升级为预判信号。

**架构：** 所有改动均为参数调整或单条件新增，全部在现有 Calculator 类内部完成。无新文件、无新表、无新数据流。每个 Calculator 保持独立可测。收敛检测复用 `base.py` 线性回归函数。

**技术栈：** Python 3.9, NumPy, pandas, DuckDB, pytest

**源方案：** `~/.claude/plans/crispy-questing-pony.md`

---

## 文件结构

| 文件 | 改动 |
|------|------|
| `backend/etl/calc_macd.py` | window=4→8, 3日回归收敛判定 |
| `backend/etl/calc_dde.py` | window=4→8 |
| `backend/etl/calc_ma.py` | 3日回归收敛判定, `sideways` alignment, 新增 import |
| `backend/etl/calc_kpattern.py` | MA10 自计算, 阳/阴克阳趋势过滤 |
| `backend/db/schema.py` | alignment CHECK 增加 `sideways` |
| `tests/test_etl/test_calc_macd.py` | 8-bar 窗口测试, 3日收敛测试 |
| `tests/test_etl/test_calc_ma.py` | sideways 测试, 3日收敛测试 |
| `tests/test_etl/test_calc_kpattern.py` | 阳/阴克阳趋势过滤测试 |

---

### 任务 1: MACD 趋势窗口 4→8

**文件:** `backend/etl/calc_macd.py:46`, `tests/test_etl/test_calc_macd.py`

- [ ] **步骤 1: 写失败的测试**

在 `test_macd_trend_insufficient_window_is_none` 之后添加：

```python
def test_macd_trend_8bar_window():
    """MACD 趋势使用 8-bar 回归窗口（非 4-bar）。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    bar = np.array([0.0, 0.0, 0.0, 0.01, 0.02, 0.03, 0.04,
                    0.05, 0.06, 0.07, 0.08, 0.09])
    result = calc._compute_trend(bar, window=8)
    assert result[11] == "up", f"8-bar 上升趋势应为 up，实际 {result[11]}"
    assert result[6] is None, f"仅 7 根 bar 应为 None，实际 {result[6]}" 
```

- [ ] **步骤 2: 确认失败** `pytest tests/test_etl/test_calc_macd.py::test_macd_trend_8bar_window -v` — 预期 FAIL

- [ ] **步骤 3: 改 window=8**

`calc_macd.py:46`：`window = 4` → `window = 8  # 8-bar regression`

- [ ] **步骤 4: 确认通过** `pytest tests/test_etl/test_calc_macd.py -v` — 预期 20 passed

- [ ] **步骤 5: 提交**
```bash
git add backend/etl/calc_macd.py tests/test_etl/test_calc_macd.py
git commit -m "feat: MACD 趋势窗口 4→8，捕捉两周短期趋势"
```

---

### 任务 2: DDE 趋势窗口 4→8

**文件:** `backend/etl/calc_dde.py:147`, `tests/test_etl/test_calc_dde.py`

- [ ] **步骤 1: 写失败的测试**

```python
def test_dde_trend_8bar_window():
    """DDE 趋势使用 8-bar 回归窗口。"""
    calc = DDECalculator.__new__(DDECalculator)
    ddx2 = np.array([0.001, 0.002, 0.003, 0.004, 0.005,
                     0.006, 0.007, 0.008, 0.009, 0.010])
    result = calc._compute_trend(ddx2, window=8)
    assert result[9] is not None, "8-bar 窗口下第 9 根应有趋势值"
```

- [ ] **步骤 2: 确认失败** `pytest tests/test_etl/test_calc_dde.py::test_dde_trend_8bar_window -v`

- [ ] **步骤 3: 改 window=8** `calc_dde.py:147`：`trend_window = 4` → `trend_window = 8`

- [ ] **步骤 4: 确认通过** `pytest tests/test_etl/test_calc_dde.py -v`

- [ ] **步骤 5: 提交**
```bash
git add backend/etl/calc_dde.py tests/test_etl/test_calc_dde.py
git commit -m "feat: DDE 趋势窗口 4→8，与 MACD 对齐"
```

---

### 任务 3: MACD near_golden/near_dead 改用 3 日回归收敛

**文件:** `backend/etl/calc_macd.py:150-171`, `tests/test_etl/test_calc_macd.py`

- [ ] **步骤 1: 写失败的测试**

```python
def test_macd_near_golden_3day_regression():
    """3 日回归斜率检测收敛，容忍日间波动。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2", "d3"],
        "close_qfq": [10.0, 10.0, 10.0, 10.0],
        "dif":       [0.50, 0.51, 0.52, 0.49],
        "dea":       [0.55, 0.57, 0.56, 0.53],
        "macd_bar":  [-0.10, -0.12, -0.08, -0.08],
    })
    result = calc._compute_turning_points(df)
    # 间距: 0.05, 0.06, 0.04, 0.04
    # Day 3: 3日回归 [0.06, 0.04, 0.04] 斜率负 → 趋势性收缩 → near_golden
    assert result[3] == "near_golden", f"实际 {result[3]}"
```

- [ ] **步骤 2: 确认失败**

- [ ] **步骤 3: 替换收敛判定逻辑**

将 `calc_macd.py` 第 150-171 行替换为：

```python
            gap = abs(dif[i] - dea[i])

            # 收敛：优先用 3 日回归（容忍日间波动），兜底 3 日绝对值缩小
            narrowing = False
            if i >= 2:
                if not pd.isna(dif[i-2]) and not pd.isna(dea[i-2]):
                    gap_seq = np.array([
                        abs(dif[i-2]-dea[i-2]), abs(dif[i-1]-dea[i-1]), gap
                    ])
                    gap_slope = linear_regression_slope(gap_seq, use_log=False)
                    narrowing = gap_slope < 0 or gap < abs(dif[i-2]-dea[i-2])
            else:
                narrowing = gap < abs(dif[i-1]-dea[i-1])

            if not narrowing:
                continue

            if abs(dea[i]) < close[i] * 0.001:
                near = gap < close[i] * 0.0001
            else:
                near = gap / abs(dea[i]) < 0.15

            if near:
                if dif[i] < dea[i]:
                    result[i] = "near_golden"
                else:
                    result[i] = "near_dead"
```

- [ ] **步骤 4: 确认通过** `pytest tests/test_etl/test_calc_macd.py -v`

- [ ] **步骤 5: 提交**

---

### 任务 4: MA near_golden/near_dead 改用 3 日回归收敛

**文件:** `backend/etl/calc_ma.py:3,137-145`, `tests/test_etl/test_calc_ma.py`

- [ ] **步骤 1: 新增 import** `calc_ma.py:3` 加 `linear_regression_slope`

```python
from backend.etl.base import sma, to_float_safe, linear_regression_slope
```

- [ ] **步骤 2: 写测试** — MA 含噪收敛场景，3 日回归应检出

- [ ] **步骤 3: 替换收敛判定** — 与任务 3 同模式，操作 `ma5/ma10` 间距

- [ ] **步骤 4: 确认通过** `pytest tests/test_etl/test_calc_ma.py -v`

- [ ] **步骤 5: 提交**

---

### 任务 5: 新增 `sideways`（横盘）均线状态

**文件:** `backend/etl/calc_ma.py:84-86`, `backend/db/schema.py:287-289`, `tests/test_etl/test_calc_ma.py`

- [ ] **步骤 1: 写失败的测试** — 双斜率在平区且非 tangle → 期望 `"sideways"`，当前得 `None`

- [ ] **步骤 2: 新增 sideways 判定**

在 tangle 块 `continue` 后、`above = ma5[i] > ma10[i]` 前插入：

```python
            s5_flat = s5[i] > -0.3 and s5[i] < 0.3
            s10_flat = s10[i] > -0.3 and s10[i] < 0.3
            if s5_flat and s10_flat:
                result[i] = "sideways"
                continue
```

- [ ] **步骤 3: 更新 schema** — CHECK 约束末尾加 `'sideways'`

- [ ] **步骤 4: 确认通过** `pytest tests/test_etl/test_calc_ma.py -v`

- [ ] **步骤 5: 提交**

---

### 任务 6: 阳克阴 / 阴克阳 增加 MA10 趋势上下文

**文件:** `backend/etl/calc_kpattern.py:62,122-124,185-187`, `tests/test_etl/test_calc_kpattern.py`

- [ ] **步骤 1: 新增 MA10** — `calc_kpattern.py:62` 后加 `ma_10 = sma(c, 10)`

- [ ] **步骤 2: 阳克阴加趋势条件** — 末尾加 `and not pd.isna(ma_10[i]) and c[i] > ma_10[i]`

- [ ] **步骤 3: 阴克阳加趋势条件** — 末尾加 `and not pd.isna(ma_10[i]) and c[i] < ma_10[i]`

- [ ] **步骤 4: 写测试** — 下跌趋势中阳克阴被过滤、上升趋势中阴克阳被过滤

- [ ] **步骤 5: 确认通过** `pytest tests/test_etl/test_calc_kpattern.py -v`

- [ ] **步骤 6: 提交**

---

### 任务 7: 端到端验证

- [ ] **运行全部测试:** `pytest tests/test_etl/test_calc_*.py -v` — 期望 ~65 passed

- [ ] **单股流水线:** `python3 -m backend.cli etl --step build-all --ts-code 000001.SZ --start 20260101`

- [ ] **趋势覆盖度:** 查 `v_dws_macd_daily_latest` — flat% 在 15-40%

- [ ] **near 信号量:** 查 `v_dws_macd_daily_latest` — near_golden/near_dead 显著增加

- [ ] **sideways 覆盖:** 查 `v_dws_ma_daily_latest` — sideways 占非 NULL 的 20-40%

---

### 任务 8 (P2): ETL 后趋势分布日志

**文件:** `backend/etl/orchestrator.py`

MACD ETL 后加趋势分布统计，flat% 超出 [15%, 40%] 时 warn。

---

## 自检

- ✅ 4 项优化各有独立任务（T1-T6）
- ✅ 端到端验证覆盖全部指标（T7）
- ✅ 无 TBD/TODO/占位符
- ✅ 所有 import、函数签名、常量名一致
