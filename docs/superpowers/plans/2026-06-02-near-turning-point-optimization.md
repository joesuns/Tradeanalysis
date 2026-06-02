# MA/MACD 转折点优化 — 实施计划

> **给执行者：** 使用 superpowers:subagent-driven-development（推荐）逐任务实施。每步 TDD：RED→GREEN→COMMIT。

**目标：** near_golden/near_dead 从「间距 < 15% 且在缩小」改为「预估交叉天数 < 3」，信号含义从空间描述升级为时间预测。

**架构：** 两个 Calculator 各改一个方法的内部判定逻辑。`turning_point` 列不变、枚举值不变、DDL 不变、视图不变、API 不变、Excel 不变。

**技术栈：** Python 3.9, NumPy, DuckDB, pytest

**依据方案：** `~/.claude/plans/crispy-questing-pony.md` (near 判定优化)

---

## 文件结构

| 文件 | 改动 |
|------|------|
| `backend/etl/calc_ma.py:161-186` | near 段替换为 est_days 逻辑 |
| `backend/etl/calc_macd.py:192-228` | near 段替换为 est_days + 零轴兜底 |
| `tests/test_etl/test_calc_ma.py` | 新增 4 个 near 测试 |
| `tests/test_etl/test_calc_macd.py` | 新增 3 个 near 测试 + 适配 1 个已有测试 |

---

### 任务 1: MA near_golden/near_dead 改用 est_days

**文件:**
- 修改: `backend/etl/calc_ma.py:161-186`
- 修改: `tests/test_etl/test_calc_ma.py`

- [ ] **步骤 1: 写失败的测试**

在 `tests/test_etl/test_calc_ma.py` 的 `test_near_dead_ma` 之后、`test_tangle_needs_cross_count` 之前，添加：

```python
def test_near_ma_small_gap_direct():
    """间距 < 0.5% → 直通 near_golden，不看收敛速度。"""
    calc = MACalculator.__new__(MACalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2", "d3"],
        "close_qfq": [10.0, 10.1, 10.2, 10.3],
        "ma_5":      [9.9, 9.95, 9.97, 9.98],
        "ma_10":     [10.0, 10.0, 10.0, 10.0],
        "ma5_slope": [0.5, 0.5, 0.5, 0.5],
        "ma10_slope":[0.5, 0.5, 0.5, 0.5],
    })
    result = calc._compute_turning_points(df)
    # gap/ma10 = 0.02/10.0 = 0.2% < 0.5% → 直通
    assert result[3] == "near_golden", f"小间距应直通 near_golden，实际 {result[3]}"


def test_near_ma_est_2days():
    """gap=2%, 收敛 1.2%/日 → est=1.7天 < 3 → near_golden。"""
    calc = MACalculator.__new__(MACalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2", "d3"],
        "close_qfq": [10.0, 10.1, 10.2, 10.3],
        "ma_5":      [9.5, 9.6, 9.7, 9.8],
        "ma_10":     [10.5, 10.4, 10.2, 10.0],
        "ma5_slope": [0.5, 0.5, 0.5, 0.5],
        "ma10_slope":[0.5, 0.5, 0.5, 0.5],
    })
    result = calc._compute_turning_points(df)
    # gaps: 1.0, 0.8, 0.5, 0.2 → 3日回归斜率负, est<3
    assert result[3] == "near_golden", f"est<3应触发 near_golden，实际 {result[3]}"


def test_near_ma_est_too_slow():
    """gap 大 + 收敛太慢 → est>3 → 不触发。"""
    calc = MACalculator.__new__(MACalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2", "d3"],
        "close_qfq": [10.0, 10.1, 10.2, 10.3],
        "ma_5":      [9.5, 9.52, 9.55, 9.58],
        "ma_10":     [10.5, 10.48, 10.46, 10.44],
        "ma5_slope": [0.5, 0.5, 0.5, 0.5],
        "ma10_slope":[0.5, 0.5, 0.5, 0.5],
    })
    result = calc._compute_turning_points(df)
    # gaps: 1.0, 0.96, 0.91, 0.86 → 收敛太慢
    assert result[3] is None, f"收敛太慢不应触发，实际 {result[3]}"


def test_near_ma_widening():
    """间距扩大 → 不触发。"""
    calc = MACalculator.__new__(MACalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2", "d3"],
        "close_qfq": [10.0, 10.1, 10.2, 10.3],
        "ma_5":      [9.5, 9.4, 9.3, 9.2],
        "ma_10":     [10.0, 10.1, 10.2, 10.3],
        "ma5_slope": [0.5, 0.5, 0.5, 0.5],
        "ma10_slope":[0.5, 0.5, 0.5, 0.5],
    })
    result = calc._compute_turning_points(df)
    # gaps: 0.5, 0.7, 0.9, 1.1 → 扩大中
    assert result[3] is None, f"间距扩大不应触发，实际 {result[3]}"
```

- [ ] **步骤 2: 运行测试确认失败**

```bash
pytest tests/test_etl/test_calc_ma.py::test_near_ma_small_gap_direct \
       tests/test_etl/test_calc_ma.py::test_near_ma_est_2days \
       tests/test_etl/test_calc_ma.py::test_near_ma_est_too_slow \
       tests/test_etl/test_calc_ma.py::test_near_ma_widening -v
```

`test_near_ma_small_gap_direct` 在当前逻辑下 gap=0.2% < 15%，且收敛，旧代码也会触发。其他三个测试有不同期望——至少 `test_near_ma_est_too_slow` 会 FAIL（旧代码 gap 在缩小且 < 15%，会触发 near）。

- [ ] **步骤 3: 实现新逻辑**

替换 `backend/etl/calc_ma.py` 第 161-186 行：

```python
            # Near golden / near dead: 预估交叉天数 < 3
            gap = abs(ma5[i] - ma10[i])
            gap_pct = gap / ma10[i]

            # 小间距直通: gap < 0.5% of MA10
            if gap_pct < 0.005:
                if ma5[i] < ma10[i]:
                    result[i] = "near_golden"
                else:
                    result[i] = "near_dead"
                continue

            # 速度判定: 3 日回归 est_days = gap / convergence_speed
            if i >= 2:
                gap_seq = np.array([
                    abs(ma5[i - 2] - ma10[i - 2]),
                    abs(ma5[i - 1] - ma10[i - 1]),
                    gap,
                ])
                gap_slope = linear_regression_slope(gap_seq, use_log=False)
                if gap_slope < 0:
                    conv_speed = -gap_slope
                    if conv_speed > 1e-9 and gap / conv_speed < 3:
                        if ma5[i] < ma10[i]:
                            result[i] = "near_golden"
                        else:
                            result[i] = "near_dead"
```

- [ ] **步骤 4: 运行全部 MA 测试**

```bash
pytest tests/test_etl/test_calc_ma.py -v
```

预期：~17 passed（原 13 + 新增 4）。需检查 `test_near_golden_ma` 和 `test_near_golden_ma_3day_regression` 是否与新逻辑兼容——如果旧测试数据在新逻辑下不触发，需适配测试数据。

- [ ] **步骤 5: 提交**

```bash
git add backend/etl/calc_ma.py tests/test_etl/test_calc_ma.py
git commit -m "feat: MA near_golden/near_dead 改为预估交叉天数<3判定"
```

---

### 任务 2: MACD near_golden/near_dead 改用 est_days

**文件:**
- 修改: `backend/etl/calc_macd.py:192-228`
- 修改: `tests/test_etl/test_calc_macd.py`

- [ ] **步骤 1: 写失败的测试**

在 `tests/test_etl/test_calc_macd.py` 的 `test_macd_near_zero_axis_absolute_threshold` 之后添加：

```python
def test_macd_near_small_gap_direct():
    """DIF-DEA 间距 < 0.005 → 直通 near_golden。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2", "d3"],
        "close_qfq": [10.0, 10.0, 10.0, 10.0],
        "dif":       [0.30, 0.30, 0.30, 0.30],
        "dea":       [0.30, 0.30, 0.30, 0.304],
        "macd_bar":  [0.0, 0.0, 0.0, -0.008],
    })
    result = calc._compute_turning_points(df)
    # gap = |0.30-0.304| = 0.004 < 0.005 → 直通
    assert result[3] == "near_golden", f"小间距应直通 near_golden，实际 {result[3]}"


def test_macd_near_est_2days():
    """est_days < 3 + 零轴常规判定 → near_golden。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2", "d3"],
        "close_qfq": [10.0, 10.0, 10.0, 10.0],
        "dif":       [0.50, 0.51, 0.52, 0.49],
        "dea":       [0.55, 0.57, 0.56, 0.53],
        "macd_bar":  [-0.10, -0.12, -0.08, -0.08],
    })
    result = calc._compute_turning_points(df)
    # gaps: 0.05, 0.06, 0.04, 0.04 → 3日回归负, est≈2.2天
    # gap/|DEA|=0.04/0.53=7.5% < 15% → near_golden
    assert result[3] == "near_golden", f"est<3 + 常规通过应触发，实际 {result[3]}"


def test_macd_near_est_too_slow():
    """收敛太慢 → est>3 → 不触发。"""
    calc = MACDCalculator.__new__(MACDCalculator)
    df = pd.DataFrame({
        "trade_date": ["d0", "d1", "d2", "d3"],
        "close_qfq": [10.0, 10.0, 10.0, 10.0],
        "dif":       [0.50, 0.51, 0.515, 0.52],
        "dea":       [0.60, 0.60, 0.60, 0.60],
        "macd_bar":  [-0.20, -0.18, -0.17, -0.16],
    })
    result = calc._compute_turning_points(df)
    # gaps: 0.10, 0.09, 0.085, 0.08 → 收敛太慢 est≈6天
    assert result[3] is None, f"est>3 不应触发，实际 {result[3]}"
```

- [ ] **步骤 2: 运行测试确认失败**

```bash
pytest tests/test_etl/test_calc_macd.py::test_macd_near_small_gap_direct \
       tests/test_etl/test_calc_macd.py::test_macd_near_est_2days \
       tests/test_etl/test_calc_macd.py::test_macd_near_est_too_slow -v
```

预期：至少 `test_macd_near_est_too_slow` FAIL——旧逻辑 gap 在缩小且 < 15%，会触发 near。

- [ ] **步骤 3: 实现新逻辑**

替换 `backend/etl/calc_macd.py` 第 192-228 行：

```python
            # Near golden / near dead: 预估交叉天数 < 3
            if pd.isna(dif[i]) or pd.isna(dea[i]) or dea[i] == 0:
                continue
            if pd.isna(dif[i - 1]) or pd.isna(dea[i - 1]):
                continue

            gap = abs(dif[i] - dea[i])

            # 小间距直通: DIF-DEA 几乎合并
            if gap < 0.005:
                if dif[i] < dea[i]:
                    result[i] = "near_golden"
                else:
                    result[i] = "near_dead"
                continue

            # 速度判定: 3 日回归 est_days = gap / convergence_speed
            if i >= 2:
                if not pd.isna(dif[i - 2]) and not pd.isna(dea[i - 2]):
                    gap_seq = np.array([
                        abs(dif[i - 2] - dea[i - 2]),
                        abs(dif[i - 1] - dea[i - 1]),
                        gap,
                    ])
                    gap_slope = linear_regression_slope(gap_seq, use_log=False)
                    if gap_slope < 0:
                        conv_speed = -gap_slope
                        if conv_speed > 1e-9 and gap / conv_speed < 3:
                            # 零轴兜底（保留不变）
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

- [ ] **步骤 4: 适配已有测试**

检查 `test_macd_near_golden`、`test_macd_near_dead`、`test_macd_near_zero_axis_absolute_threshold`、`test_macd_near_golden_3day_regression`。其测试数据的 est_days 值需 < 3，否则 near 不会触发。必要时微调测试数据使 est_days 落在 3 以内。

- [ ] **步骤 5: 运行全部 MACD 测试**

```bash
pytest tests/test_etl/test_calc_macd.py -v
```

预期：~27 passed（原 24 + 新增 3）。

- [ ] **步骤 6: 提交**

```bash
git add backend/etl/calc_macd.py tests/test_etl/test_calc_macd.py
git commit -m "feat: MACD near_golden/near_dead 改为 est_days<3 + 零轴兜底不变"
```

---

### 任务 3: 端到端验证

- [ ] **步骤 1: 全量指标测试**

```bash
pytest tests/test_etl/test_calc_*.py -v
```

预期：~73 passed（原 66 + 新增 7）。

- [ ] **步骤 2: near 信号分布检查**

```bash
python3 -c "
import duckdb
con = duckdb.connect('./data/tradeanalysis.duckdb', read_only=True)
r = con.execute('''
    SELECT turning_point, COUNT(*) as cnt
    FROM v_dws_ma_daily_latest WHERE turning_point IS NOT NULL
    GROUP BY turning_point ORDER BY cnt DESC
''').df()
print(r.to_string())
print()
r2 = con.execute('''
    SELECT turning_point, COUNT(*) as cnt
    FROM v_dws_macd_daily_latest WHERE turning_point IS NOT NULL
    GROUP BY turning_point ORDER BY cnt DESC
''').df()
print(r2.to_string())
# 期望: near_golden+near_dead 数量 < golden_cross+dead_cross 的 2.5 倍
"
```

- [ ] **步骤 3: 提交验证**

```bash
git add -A && git commit -m "verify: near est_days<3 端到端验证通过"
```

---

## 自检

**1. 覆盖检查：**
- ✅ 任务 1: MA near 改为 est_days（小间距直通 + 速度判定）
- ✅ 任务 2: MACD near 改为 est_days（同上 + 零轴兜底保留）
- ✅ 任务 3: 端到端验证含 near 分布检查

**2. 占位符扫描：** 无。

**3. 类型一致性：**
- `gap / conv_speed < 3` 中 gap 和 conv_speed 同单位（raw MA 值），est_days 单位是 bar（交易日/周）
- `gap_pct < 0.005` 即 0.5%，无量纲
- `gap < 0.005` 是 MACD 的绝对阈值，单位与 DIF/DEA 一致
- 零轴兜底阈值 `close[i] * 0.001` 和 `close[i] * 0.0001` 不变
