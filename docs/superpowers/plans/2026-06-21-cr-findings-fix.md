# Code Review 发现修复计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-step. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Code Review 发现的 7 个问题（1 个高 + 1 个中 + 5 个低），不涉及核心逻辑变更。

**Architecture:** 纯小改：改默认值、改注释、补翻译、提取常量、补测试、加注释、跑命令。全部在 `fix/daily-basic-null-and-perf` 分支上执行。

**前置条件：** 上一轮 6 个 commit（`285afd7..29be367`）已完成。

---

### Task 1: 修复 `window` 默认值 + Docstring 用词统一

**Files:**
- Modify: `backend/etl/b4_alerts.py:63`
- Modify: `backend/etl/b4_alerts.py:70-71`

- [ ] **Step 1: 改 `window` 默认值 5→2**

编辑 `backend/etl/b4_alerts.py` line 63：

```python
# 改前
def compute_ddx2_slope_alerts(ddx2, window=5, eps=0.0):

# 改后
def compute_ddx2_slope_alerts(ddx2, window=2, eps=0.0):
```

- [ ] **Step 2: 统一 docstring 「反转」→「反弹/回落」**

编辑 `backend/etl/b4_alerts.py` lines 70-71：

```python
# 改前
    - downturn_reverse: 下降趋势反转（斜率由负转正，看多）
    - upturn_reverse: 上升趋势反转（斜率由正转负，看空）

# 改后
    - downturn_reverse: 下降趋势反弹（斜率由负转正，看多）
    - upturn_reverse: 上升趋势回落（斜率由正转负，看空）
```

- [ ] **Step 3: 运行测试确认**

```bash
pytest tests/test_etl/test_b4_alerts.py tests/test_etl/test_calc_dde.py -v
```

- [ ] **Step 4: 提交**

```bash
git add backend/etl/b4_alerts.py
git commit -m "fix(alert): change DDE alert window default to 2, unify docstring wording

window=2 对齐生产环境唯一值 DDE_ALERT_WINDOW；docstring「反转」统一为用户可见的「反弹/回落」。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 补全 `_flat` 翻译 + 提取共享常量

**Files:**
- Modify: `backend/export_wide.py:188-195`

- [ ] **Step 1: 提取共享标签常量 + 补回 _flat 翻译**

编辑 `backend/export_wide.py`，在 `_ENUM_VALUES` 定义之前插入常量，并修改两条 alert 条目：

```python
# 在 _ENUM_VALUES = { 之前插入：
_ALERT_LABELS = {
    "upturn_reverse": "上升趋势回落",
    "downturn_reverse": "下降趋势反弹",
    "upturn_flat": "上升趋势走平",
    "downturn_flat": "下降趋势走平",
}

# 在 _ENUM_VALUES 内：
    "macd_alert": _ALERT_LABELS,
    ...
    "dde_alert": _ALERT_LABELS,
```

- [ ] **Step 2: 验证导入**

```bash
python -c "from backend.export_wide import _ENUM_VALUES; assert _ENUM_VALUES['macd_alert'] is _ENUM_VALUES['dde_alert']; print('OK')"
```

- [ ] **Step 3: 提交**

```bash
git add backend/export_wide.py
git commit -m "refactor(export): extract shared _ALERT_LABELS + restore _flat translations

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 补注释 + 补测试

**Files:**
- Modify: `backend/b4_gate/columns.py:24`
- Modify: `tests/test_etl/test_b4_alerts.py`

- [ ] **Step 1: B4_SOFT 加注释说明原因**

编辑 `backend/b4_gate/columns.py`，在 `B4_SOFT_DAILY_FIELDS` 上方加一行注释：

```python
# TA-native with different window than 123 — excluded from hard-gate diff
# (dde_alert: TA 2-bar ≠ 123 5-bar; ma_alignment: TA regime ≠ 123)
B4_SOFT_DAILY_FIELDS: List[str] = ["ma_alignment", "dde_alert"]
```

- [ ] **Step 2: 添加跨指标警报枚举等价性测试**

编辑 `tests/test_etl/test_b4_alerts.py`，在文件末尾追加参数化测试：

```python
def test_bullish_alert_enum_equivalence():
    """MACD V-shape and DDE slope-up both produce downturn_reverse."""
    # MACD: V shape → bullish → downturn_reverse
    macd_alerts = compute_macd_hist_turn_alerts(np.array([1.0, -1.0, 0.5]))
    assert macd_alerts[2] == "downturn_reverse"
    # DDE: slope turns positive → bullish → downturn_reverse
    dde_alerts = compute_ddx2_slope_alerts(
        np.array([0.0, 0.0, 1.0, 0.0, 5.0]), window=2, eps=0.0
    )
    assert dde_alerts[-1] == "downturn_reverse"


def test_bearish_alert_enum_equivalence():
    """MACD Λ-shape and DDE slope-down both produce upturn_reverse."""
    # MACD: Λ shape → bearish → upturn_reverse
    macd_alerts = compute_macd_hist_turn_alerts(np.array([1.0, 2.0, 1.0]))
    assert macd_alerts[2] == "upturn_reverse"
    # DDE: slope turns negative → bearish → upturn_reverse
    dde_alerts = compute_ddx2_slope_alerts(
        np.array([0.0, 0.0, -1.0, 0.0, -5.0]), window=2, eps=0.0
    )
    assert dde_alerts[-1] == "upturn_reverse"
```

- [ ] **Step 3: 运行全部受影响测试**

```bash
pytest tests/test_etl/test_b4_alerts.py tests/test_etl/test_calc_dde.py tests/test_b4_gate_*.py -v
```

- [ ] **Step 4: 提交**

```bash
git add backend/b4_gate/columns.py tests/test_etl/test_b4_alerts.py
git commit -m "docs(b4-gate): explain B4_SOFT exclusion + add cross-indicator alert enum equivalence test

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: 存量迁移执行（部署时手动）

**无需代码修改。** 部署后在服务器上执行：

```bash
python -m backend.cli refresh --indicator dde --date <latest_calc_date>
```

覆盖 `dws_dde_daily` 和 `dws_dde_weekly` 的 `alert` 列存量，新枚举值与新翻译表对齐。
