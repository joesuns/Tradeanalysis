# 警惕类指标枚举语义修正 + 中文标签优化

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正 DDE Alert 枚举值颠倒（逻辑 bug）并将统一的「上升拐头/下降拐头」中文标签更名为无歧义的「上升趋势回落/下降趋势反弹」，使 MACD Alert 和 DDE Alert 的枚举语义完全对齐。

**Architecture:** 两根动线修复：(1) `b4_alerts.py` 交换 DDE Alert 两个分支的枚举输出值，对齐「枚举命名描述被反转的旧趋势」设计本意；(2) `export_wide.py` 和 `export-column-comments.yaml` 更新中文标签；(3) `b4_gate/enums.py` 同步翻转 123→TA 映射以维持 B4 Gate diff 一致性。测试用例预期值同步翻转。

**Tech Stack:** Python 3.9+, pytest

**存量数据影响:** DDE 日线+周线 `alert` 列中已有的枚举值与修改后语义不一致，部署后需执行 `cli refresh --indicator dde --date <latest_date>` 重算覆盖存量。

---

### Task 1: 修正 DDE Alert 枚举值（核心逻辑 bug）

**Files:**
- Modify: `backend/etl/b4_alerts.py:69-71,84-87`

- [ ] **Step 1: 交换 `compute_ddx2_slope_alerts` 两个分支的枚举输出值**

编辑 `backend/etl/b4_alerts.py:69-71`，更新 docstring：

```python
    Returns TA enums:
    - downturn_reverse: 下降趋势反转（斜率由负转正，看多）
    - upturn_reverse: 上升趋势反转（斜率由正转负，看空）
```

编辑 `backend/etl/b4_alerts.py:84-87`，交换分支：

```python
        if s_prev < -eps and s_now > eps:
            result[i] = "downturn_reverse"
        elif s_prev > eps and s_now < -eps:
            result[i] = "upturn_reverse"
```

- [ ] **Step 2: 运行 DDE Alert 单元测试确认期望值翻转**

```bash
pytest tests/test_etl/test_b4_alerts.py -v
```

预期：`test_ddx2_slope_inflection_bull_2bar` 和 `test_ddx2_slope_inflection_bear_2bar` **FAIL**（断言值尚未更新，符合预期）。

- [ ] **Step 3: 提交**

```bash
git add backend/etl/b4_alerts.py
git commit -m "fix(alert): swap DDE alert enum values to align with design intent

上下翻转两个枚举值的分支——upturn_reverse（上升趋势回调/看空）对应 s_prev>0 & s_now<0（之前上涨、现在逆转）；downturn_reverse（下降趋势反弹/看多）对应 s_prev<0 & s_now>0（之前下跌、现在逆转）。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 翻转 123→TA 枚举映射（B4 Gate 一致性）

**Files:**
- Modify: `backend/b4_gate/enums.py:80-81`

- [ ] **Step 1: 修改 `DDE_ALERT_123_TO_TA` 映射**

编辑 `backend/b4_gate/enums.py:79-81`：

```python
DDE_ALERT_123_TO_TA: Dict[str, str] = {
    "斜率拐头看多": "downturn_reverse",
    "斜率拐头看空": "upturn_reverse",
    "downturn_reverse": "downturn_reverse",
    "downturn_flat": "downturn_flat",
    "upturn_reverse": "upturn_reverse",
    "upturn_flat": "upturn_flat",
}
```

- [ ] **Step 2: 验证 B4 Gate diff 测试**

```bash
pytest tests/test_b4_gate_diff.py -v
```

预期：`test_diff_b4_skips_dde_alert` — TA 和 123 两侧同步翻转后，diff 行为不变。若测试仍通过，说明映射翻转正确。

- [ ] **Step 3: 提交**

```bash
git add backend/b4_gate/enums.py
git commit -m "fix(b4-gate): flip DDE alert 123→TA enum mapping

123「斜率拐头看多」映射到 downturn_reverse（下降趋势反弹），「斜率拐头看空」映射到 upturn_reverse（上升趋势回落），与 b4_alerts.py 枚举修正对齐。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 更新中文标签（`上升拐头` → `上升趋势回落` / `下降拐头` → `下降趋势反弹`）

**Files:**
- Modify: `backend/export_wide.py:188-197`

- [ ] **Step 1: 修改 Excel 枚举翻译表**

编辑 `backend/export_wide.py:188-197`：

```python
    "macd_alert": {"upturn_reverse": "上升趋势回落", "downturn_reverse": "下降趋势反弹"},
    "ma_turning_point": {"golden_cross": "金叉", "dead_cross": "死叉",
                         "near_golden": "近金叉", "near_dead": "近死叉"},
    "dde_trend": {"up": "上升", "down": "下降", "flat": "走平"},
    "dde_divergence": {"top_divergence": "顶背离", "bottom_divergence": "底背离"},
    "dde_divergence_tradable": {"top_divergence": "顶背离", "bottom_divergence": "底背离"},
    "dde_divergence_reject": {"skip_peak": "隔峰", "tg_lag": "滞后", "zone_mismatch": "区域"},
    "dde_alert": {"upturn_reverse": "上升趋势回落", "downturn_reverse": "下降趋势反弹"},
```

注意：删除 `macd_alert` 和 `dde_alert` 翻译表中从未被代码产出的 `"upturn_flat": "上升走平"`、`"downturn_flat": "下降走平"` 条目（`compute_macd_hist_turn_alerts` 和 `compute_ddx2_slope_alerts` 均不产出 flat 值）。

- [ ] **Step 2: 验证导出模块无语法错误**

```bash
python -c "from backend.export_wide import _ENUM_VALUES; print('OK')"
```

预期：OK

- [ ] **Step 3: 提交**

```bash
git add backend/export_wide.py
git commit -m "refactor(export): rename alert labels to 上升趋势回落/下降趋势反弹

避免「拐头」歧义（可解读为「趋势拐头反转」或「指标拐头向上」）。
同步删除从未产出的 upturn_flat/downturn_flat 翻译条目。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 更新列注释文档

**Files:**
- Modify: `docs/export/export-column-comments.yaml:112-115`
- Modify: `docs/export/export-column-comments.yaml:190-193`

- [ ] **Step 1: 更新 MACD Alert 注释**

编辑 `docs/export/export-column-comments.yaml:112-115`：

```yaml
  macd_alert: |
    MACD 警惕：看连续 3 根 MACD 柱是否形成 V 形或 Λ 形拐点。
    - 下降趋势反弹（V 形）：柱线在下跌途中触底反弹（h[i-1] 为局部最小值），看多预警。
    - 上升趋势回落（Λ 形）：柱线在上涨途中冲顶回落（h[i-1] 为局部最大值），看空预警。
```

- [ ] **Step 2: 更新 DDE Alert 注释**

编辑 `docs/export/export-column-comments.yaml:190-193`：

```yaml
  dde_alert: |
    DDE 警惕：相邻两段 2 根 K 线上对 DDX2 做线性回归，检测斜率方向切换。
    - 下降趋势反弹：DDX2 斜率由负转正，大单资金动向由流出转为流入，看多预警。
    - 上升趋势回落：DDX2 斜率由正转负，大单资金动向由流入转为流出，看空预警。
```

- [ ] **Step 3: 提交**

```bash
git add docs/export/export-column-comments.yaml
git commit -m "docs(export): update alert column comments for new labels

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 更新测试用例

**Files:**
- Modify: `tests/test_etl/test_b4_alerts.py:22-31`
- Modify: `tests/test_etl/test_calc_dde.py:236-250`
- Modify: `tests/test_b4_gate_enums.py`（追加一条 DDE alert 测试）
- Verify: `tests/test_b4_gate_diff.py:32-42`

- [ ] **Step 1: 更新 `test_b4_alerts.py` DDE 测试预期值**

编辑 `tests/test_etl/test_b4_alerts.py:22-31`：

```python
def test_ddx2_slope_inflection_bull_2bar():
    ddx2 = np.array([0.0, 0.0, 1.0, 0.0, 5.0], dtype=float)
    alerts = compute_ddx2_slope_alerts(ddx2, window=2, eps=0.0)
    assert alerts[-1] == "downturn_reverse"


def test_ddx2_slope_inflection_bear_2bar():
    ddx2 = np.array([0.0, 0.0, -1.0, 0.0, -5.0], dtype=float)
    alerts = compute_ddx2_slope_alerts(ddx2, window=2, eps=0.0)
    assert alerts[-1] == "upturn_reverse"
```

- [ ] **Step 2: 更新 `test_calc_dde.py` DDE Alert 测试预期值**

编辑 `tests/test_etl/test_calc_dde.py:236-250`：

```python
def test_dde_slope_inflection_bull_alert():
    """2-bar adjacent-window slope flip → downturn_reverse."""
    calc = DDECalculator.__new__(DDECalculator)
    ddx2 = np.array([0.0, 0.0, 1.0, 0.0, 5.0], dtype=float)
    df = pd.DataFrame({"trade_date": [f"d{i}" for i in range(len(ddx2))], "ddx2": ddx2})
    result = calc._compute_alerts(df)
    assert result[-1] == "downturn_reverse"


def test_dde_slope_inflection_bear_alert():
    calc = DDECalculator.__new__(DDECalculator)
    ddx2 = np.array([0.0, 0.0, -1.0, 0.0, -5.0], dtype=float)
    df = pd.DataFrame({"trade_date": [f"d{i}" for i in range(len(ddx2))], "ddx2": ddx2})
    result = calc._compute_alerts(df)
    assert result[-1] == "upturn_reverse"
```

- [ ] **Step 3: 追加 `test_b4_gate_enums.py` DDE alert 映射测试**

编辑 `tests/test_b4_gate_enums.py`，在文件末尾追加：

```python
def test_dde_alert_hist_turn():
    assert normalize_value("dde_alert", "斜率拐头看多", "123") == "downturn_reverse"
    assert normalize_value("dde_alert", "斜率拐头看空", "123") == "upturn_reverse"
```

- [ ] **Step 4: 运行全部受影响的测试**

```bash
pytest tests/test_etl/test_b4_alerts.py tests/test_etl/test_calc_dde.py tests/test_b4_gate_enums.py tests/test_b4_gate_diff.py -v
```

预期：全部 PASS

- [ ] **Step 5: 运行 B4 Gate 全量回归测试**

```bash
pytest tests/test_b4_gate_*.py -v
```

预期：全部 PASS

- [ ] **Step 6: 提交**

```bash
git add tests/
git commit -m "test(alert): flip DDE alert test assertions + add B4 gate mapping test

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 验证端点（可选，仅在有运行中 API 时执行）

**Files:**
- Verify: 导出 Excel 中 macd_alert / dde_alert 列标签

- [ ] **Step 1: 运行一次导出确认中文标签**

```bash
python -m backend.cli export --date <latest_trade_date> --ts-code 000001.SZ
```

打开 `exports/analysis_*.xlsx`，确认「综合分析」sheet 中 MACD 警惕和 DDE 警惕列的表头及值显示为「上升趋势回落」「下降趋势反弹」。

- [ ] **Step 2: 验证列注释悬停显示**

在 Excel 中将鼠标悬停在 MACD 警惕 / DDE 警惕列的表头上，确认注释与 `export-column-comments.yaml` 新内容一致。

---

### Task 7: 存量数据迁移提示

**无需代码修改，记录运维步骤：**

部署后，`dws_dde_daily` 和 `dws_dde_weekly` 的 `alert` 列中，修改前的枚举值与修改后语义相反（旧 `upturn_reverse` 表示看多，新 `upturn_reverse` 表示看空）。需执行：

```bash
# 重算 DDE 日线+周线（最新分析日），覆盖存量
python -m backend.cli refresh --indicator dde --date <latest_calc_date>
```

`refresh` 对 DDE 全量 stale 走 FULL 重算，自然产出修正后的枚举值。

---

## 自检清单

- [x] **Spec coverage** — 覆盖两个问题：DDE 枚举颠倒（Task 1-2） + 中文标签歧义（Task 3-4）；测试同步（Task 5）；存量迁移提醒（Task 7）
- [x] **Placeholder scan** — 无 TBD/TODO/占位符；所有步骤含完整代码
- [x] **Type consistency** — 所有枚举值 `downturn_reverse` / `upturn_reverse` 跨文件一致；翻译表 key 与代码产出值一致
