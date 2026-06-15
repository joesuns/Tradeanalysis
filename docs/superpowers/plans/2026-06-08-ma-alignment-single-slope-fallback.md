# MA Alignment 单斜率 Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除「一走平一趋势」导致的 `alignment` NULL（导出 `N/A`），在 `_compute_alignment()` 第三层将混合态归入现有 8 类枚举，不新增 DWS 枚举、不改 schema。

**Architecture:** 保留现有三层优先级：`tangle`（间距<2.99% 且 10 日交叉≥2）→ `sideways`（双斜率 |s|<0.08%/日）→ 8 类双明确趋势。在 8 类之后追加 **Layer 3 fallback**：当且仅当「恰好一条走平、另一条有明确趋势（>0.08 或 <-0.08）」时，按交易语义映射到最近邻枚举。历史不足（无 MA 行）仍 NULL → 导出 `N/A`。

**Tech Stack:** Python 3.9+, pandas/numpy, pytest, DuckDB（仅集成测试用）

**背景数据（20260602 实库）：** 305 只成熟股 `alignment IS NULL` 且 MA 可算，100% 为 `one_slope_flat`；136 只历史不足保持 N/A。

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `backend/etl/calc_ma.py` | `_compute_alignment()` 加 Layer 3 fallback |
| `tests/test_etl/test_calc_ma.py` | 新增 6 组 fallback 用例 + 回归用例 |
| `tests/test_etl/test_append_calc.py` | APPEND≡FULL 等价性（已有，须仍 PASS） |
| `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` | §12.53 NULL 语义 + alignment 表补 fallback 说明 |
| `CLAUDE.md` | alignment 10 值说明补 Layer 3 一句 |

**不需改：** `schema.py` CHECK、4 处 ADS `ma_alignment` CASE、`export_wide.py` 着色、API router（无新枚举）

---

## Fallback 映射表（实现依据）

| 位置 (`above`) | 条件 | 结果 |
|----------------|------|------|
| MA5 > MA10 | s5_up + s10_flat | `bull_building` |
| MA5 > MA10 | s5_flat + s10_up | `bull_building` |
| MA5 > MA10 | (s5_dn 或 s10_dn) 且非双明确上行 | `bull_weakening` |
| MA5 < MA10 | s5_dn + s10_flat | `bear_building` |
| MA5 < MA10 | s5_flat + s10_dn | `bear_strong` |
| MA5 < MA10 | (s5_up 或 s10_up) 且非双明确下行 | `bear_weakening` |

硬编码阈值（与现有一致，禁止改动）：
- 走平区：`-0.08 < s < 0.08`（严格不等）
- 趋势区：`s > 0.08` 或 `s < -0.08`
- tangle：`gap < 0.0299` 且 `recent_crosses >= 2`

---

### Task 1: 一走平一趋势 — 失败测试（6 组）

**Files:**
- Modify: `tests/test_etl/test_calc_ma.py`（在 `test_alignment_sideways` 之后插入）

- [ ] **Step 1: 写入失败测试**

```python
def _alignment_df(ma5, ma10, s5, s10, n=15):
    """Helper: build minimal DataFrame for _compute_alignment tests."""
    calc = MACalculator.__new__(MACalculator)
    n = len(ma5) if hasattr(ma5, "__len__") else n
    if not hasattr(ma5, "__len__"):
        ma5 = np.full(n, ma5)
        ma10 = np.full(n, ma10)
        s5 = np.full(n, s5)
        s10 = np.full(n, s10)
    df = pd.DataFrame({
        "trade_date": [f"d{i}" for i in range(n)],
        "close_qfq": ma5,
        "ma_5": ma5,
        "ma_10": ma10,
        "ma5_slope": s5,
        "ma10_slope": s10,
    })
    return calc, df


def test_alignment_fallback_bull_building_s5_up_s10_flat():
    """一走平一趋势：MA5>MA10, s5上行, s10走平 → bull_building（平安银行类）。"""
    calc, df = _alignment_df(10.9, 10.8, 0.40, -0.01)
    result = calc._compute_alignment(df)
    assert result[-1] == "bull_building", f"期望 bull_building，实际 {result[-1]}"


def test_alignment_fallback_bull_strong_s5_flat_s10_up():
    """一走平一趋势：MA5>MA10, s5走平, s10上行 → bull_strong。"""
    calc, df = _alignment_df(10.9, 10.8, 0.02, 0.25)
    result = calc._compute_alignment(df)
    assert result[-1] == "bull_strong", f"期望 bull_strong，实际 {result[-1]}"


def test_alignment_fallback_bull_weakening_s5_dn_s10_flat():
    """一走平一趋势：MA5>MA10, s5下行, s10走平 → bull_weakening。"""
    calc, df = _alignment_df(10.9, 10.8, -0.15, 0.03)
    result = calc._compute_alignment(df)
    assert result[-1] == "bull_weakening", f"期望 bull_weakening，实际 {result[-1]}"


def test_alignment_fallback_bear_building_s5_dn_s10_flat():
    """一走平一趋势：MA5<MA10, s5下行, s10走平 → bear_building。"""
    calc, df = _alignment_df(10.7, 10.9, -0.20, -0.02)
    result = calc._compute_alignment(df)
    assert result[-1] == "bear_building", f"期望 bear_building，实际 {result[-1]}"


def test_alignment_fallback_bear_strong_s5_flat_s10_dn():
    """一走平一趋势：MA5<MA10, s5走平, s10下行 → bear_strong。"""
    calc, df = _alignment_df(10.7, 10.9, 0.01, -0.30)
    result = calc._compute_alignment(df)
    assert result[-1] == "bear_strong", f"期望 bear_strong，实际 {result[-1]}"


def test_alignment_fallback_bear_weakening_s5_up_s10_flat():
    """一走平一趋势：MA5<MA10, s5上行, s10走平 → bear_weakening。"""
    calc, df = _alignment_df(10.7, 10.9, 0.18, 0.04)
    result = calc._compute_alignment(df)
    assert result[-1] == "bear_weakening", f"期望 bear_weakening，实际 {result[-1]}"


def test_alignment_fallback_does_not_override_existing_bull_strong():
    """回归：双明确上行仍 bull_strong，fallback 不得覆盖。"""
    calc = MACalculator.__new__(MACalculator)
    dates = [f"202601{i:02d}" for i in range(1, 41)]
    prices = [10.0 + i * 0.2 for i in range(40)]
    df = pd.DataFrame({"trade_date": dates, "close_qfq": prices})
    result = calc._compute_indicators(df)
    valid = result["alignment"].dropna()
    assert (valid == "bull_strong").sum() > 0, f"双上行应仍有 bull_strong，实际 {valid.unique()}"
```

- [ ] **Step 2: 运行测试确认 FAIL**

```bash
pytest tests/test_etl/test_calc_ma.py::test_alignment_fallback_bull_building_s5_up_s10_flat \
  tests/test_etl/test_calc_ma.py::test_alignment_fallback_bull_strong_s5_flat_s10_up \
  tests/test_etl/test_calc_ma.py::test_alignment_fallback_bull_weakening_s5_dn_s10_flat \
  tests/test_etl/test_calc_ma.py::test_alignment_fallback_bear_building_s5_dn_s10_flat \
  tests/test_etl/test_calc_ma.py::test_alignment_fallback_bear_strong_s5_flat_s10_dn \
  tests/test_etl/test_calc_ma.py::test_alignment_fallback_bear_weakening_s5_up_s10_flat -v
```

Expected: FAIL — `result[-1]` 为 `None`

- [ ] **Step 3: Commit 测试**

```bash
git add tests/test_etl/test_calc_ma.py
git commit -m "test: add failing cases for MA alignment single-slope fallback"
```

---

### Task 2: 实现 Layer 3 Fallback

**Files:**
- Modify: `backend/etl/calc_ma.py:105-169`

- [ ] **Step 1: 更新 docstring**

将 `_compute_alignment` docstring 首行改为：

```python
        """11-value alignment: 8 directional + tangle + sideways + single-slope fallback.

        Layer 1 tangle: gap < 3% of MA10 AND >= 2 crosses in last 10 days.
        Layer 2 sideways: both |slope| < 0.08%/day.
        Layer 3 (fallback): exactly one slope flat, map to nearest of 8 directional codes.
        """
```

- [ ] **Step 2: 在 8 类 elif 链之后、`return result` 之前插入 fallback**

在 `elif not above and s5_up and s10_up: result[i] = "bear_rolling"` 之后添加：

```python
            # Layer 3: single-slope transitional (exactly one flat, one trending)
            if s5_flat != s10_flat:
                if above:
                    if s5_up or s10_up:
                        result[i] = "bull_building" if s5_up else "bull_strong"
                    elif s5_dn or s10_dn:
                        result[i] = "bull_weakening"
                else:
                    if s5_dn or s10_dn:
                        result[i] = "bear_building" if s5_dn else "bear_strong"
                    elif s5_up or s10_up:
                        result[i] = "bear_weakening"
```

- [ ] **Step 3: 运行 Task 1 全部 fallback 测试**

```bash
pytest tests/test_etl/test_calc_ma.py -k "alignment_fallback or alignment_sideways or alignment_bull_strong or tangle" -v
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/etl/calc_ma.py
git commit -m "feat: MA alignment Layer 3 fallback for single-slope transitional states"
```

---

### Task 3: APPEND 等价性回归

**Files:**
- Test: `tests/test_etl/test_append_calc.py`（只读，不修改）

- [ ] **Step 1: 跑 MA append 等价性**

```bash
pytest tests/test_etl/test_append_calc.py -k "ma" -v
```

Expected: PASS（`append_calculate` 调用 `_compute_indicators` → fallback 自动一致）

- [ ] **Step 2: 跑全量 append 套件**

```bash
pytest tests/test_etl/test_append_calc.py -v
```

Expected: PASS，`alignment` 列 `atol=1e-9` 等价

---

### Task 4: 全量测试 + 实库分布抽检（可选）

**Files:** 无代码变更

- [ ] **Step 1: 全量 pytest**

```bash
pytest tests/ -v
```

Expected: PASS

- [ ] **Step 2: （可选）实库分布验证**

若本地有 `data/tradeanalysis.duckdb` 且可写，重算后抽检：

```bash
python -m backend.cli calc --date 20260602 --ts-code 000001.SZ --force
```

```python
# 一次性脚本
import duckdb
from backend.config import DUCKDB_PATH
con = duckdb.connect(DUCKDB_PATH, read_only=True)
row = con.execute("""
  SELECT alignment FROM v_dws_ma_daily_latest
  WHERE ts_code='000001.SZ' AND trade_date='20260602'
""").fetchone()
print(row)  # 期望 ('bull_building',)
null_cnt = con.execute("""
  SELECT COUNT(*) FROM v_dws_ma_daily_latest m
  JOIN dwd_daily_quote q ON m.ts_code=q.ts_code AND m.trade_date=q.trade_date
  WHERE q.trade_date='20260602' AND q.is_suspended=0 AND m.alignment IS NULL
    AND m.ma_5 IS NOT NULL
""").fetchone()[0]
print("null with ma:", null_cnt)  # 期望 0
con.close()
```

---

### Task 5: 文档更新

**Files:**
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: 更新 spec §12.53 与 alignment 表**

在 `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`：

1. alignment 表（约 L602）`NULL` 行改为：

```markdown
| NULL | — | — | — | MA5/MA10/斜率不可用（IPO <10 日、停牌窗口不足）；**不走 fallback** |
```

2. 在 `tangle` 行后新增说明行：

```markdown
| *(fallback)* | > 或 < | 一走平一走 | — | Layer 3：单斜率过渡态归入最近 8 类（如 s5↑+s10平+多头位→bull_building） |
```

3. §12.53 替换为：

```markdown
### 12.53 dws_ma.alignment NULL 语义明确

> alignment 取 NULL 的唯一场景：MA5、MA10 或斜率不可用（IPO 上市不足 10 日、停牌复牌后窗口内有效交易日不足、斜率 NaN）。「一走平一趋势」过渡态由 Layer 3 fallback 归入现有 8 类，不再 NULL。
```

- [ ] **Step 2: 更新 CLAUDE.md MA 信号节**

在 `- **alignment：** 10 值（8 方向 + tangle + sideways）` 改为：

```markdown
- **alignment：** 10 值 DWS 枚举（8 方向 + tangle + sideways）；一走平一趋势过渡态 Layer 3 fallback 归入 8 类，不扩枚举
```

- [ ] **Step 3: Commit 文档**

```bash
git add docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md CLAUDE.md
git commit -m "docs: clarify MA alignment NULL vs single-slope fallback semantics"
```

---

### Task 6: 收尾核对（Engineering Protocol §⑤）

- [ ] 305 只 `one_slope_flat` 成熟股 fallback 后有值（实库重算后验证）
- [ ] 136 只历史不足仍 N/A（warmup 未动）
- [ ] `pytest tests/ -v` 全绿
- [ ] schema / export_wide / API 无改动（无新枚举）
- [ ] `test_append_calc.py` 等价性仍 `atol=1e-9`

**运维提示（写入 PR 描述，非代码）：** 已入库 DWS 快照的 `alignment` 不会自动更新；需对受影响日期执行 `calc --force` 或下一交易日自然重算。

---

## Self-Review

| 检查项 | 结果 |
|--------|------|
| Spec 6 组映射均有测试 | Task 1 覆盖 |
| 无 placeholder / TBD | ✓ |
| 不改 schema CHECK | ✓ |
| append 等价性 Task 3 验证 | ✓ |
| 历史不足 N/A 不变 | fallback 仅在 ma/slope 可算后触发 |
