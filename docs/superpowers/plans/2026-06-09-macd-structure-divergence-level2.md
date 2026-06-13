# MACD/DDE 结构背离 Level 2（隔峰+柱背）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 MACD、DDE 的 `divergence` 从 60 日 rolling 极值法改为通达信「MACD 顶底结构」Level 2（直接+隔峰线背 ∧ 柱背，结构形成日 TG 标注），对齐国内终端画线语义；Volume 量价背离保持原 rolling 法不变。

**Architecture:** 新建 `backend/etl/divergence_structure.py` 承载结构背离（逐 bar 状态机，语义对齐 Tongdaxin BARSLAST+HHV+MDIF）；`calc_macd.py` / `calc_dde.py` 改调新函数；`base.compute_price_signal_divergence` 仅保留给 Volume。DWS 列名/枚举不变（`top_divergence`/`bottom_divergence`/NULL），标注日 = **结构形成 TG**（非钝化 T）。RecalcSpec lookback 60→250 以覆盖 3 个金/死叉周期。

**Tech Stack:** Python 3.9, NumPy, pandas, DuckDB, pytest

**已批准决策：**
- **D1** 标注日 = 结构形成 TG
- **D2** 钝化 T = 线背(直接∨隔峰) ∧ 柱背；仅 TG 写入 DWS
- **D3** DDE 同 sprint（锚点 CROSS(DDX,DDX2)，KPI 分开验收）

**验收 KPI：**
- MACD：golden 集日期一致率 ≥85%（±1 交易日容差）
- DDE：golden 集 ≥70%（tushare DDX 代理限制）
- `pytest tests/ -v` 全绿；APPEND 末 bar divergence == FULL

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `backend/etl/divergence_structure.py` | **新建** MDIF 归一、cross 检测、MACD/DDE 结构背离主逻辑 |
| `backend/etl/base.py` | 保留 `compute_price_signal_divergence`（Volume 专用），docstring 注明 |
| `backend/etl/calc_macd.py` | `_compute_divergence` 改调结构函数；`RECALC_SPEC lookback=250, event_tail=10` |
| `backend/etl/calc_dde.py` | `_compute_divergence` 改调 DDE 结构函数；`RECALC_SPEC lookback=250, event_tail=10` |
| `backend/etl/calc_volume.py` | **不改** |
| `tests/test_etl/test_divergence_structure.py` | **新建** 单元测试 + golden 对标 |
| `tests/fixtures/tdx_macd_structure_golden.csv` | **新建** 通达信人工标注 |
| `tests/fixtures/tdx_dde_structure_golden.csv` | **新建** DDE 标注（可选子集） |
| `tests/test_etl/test_calc_macd.py` | 删除/替换旧 60 窗背离测试 |
| `tests/test_etl/test_calc_dde.py` | 删除/替换旧 60 窗背离测试 |
| `tests/test_etl/test_incremental_calc.py` | 删除 MACD/DDE divergence oracle 或改指向新模块 |
| `tests/test_etl/test_append_calc.py` | tail 窗 80→300 |
| `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` | §6.2 / §12.20 / §12.29 / §12.60 重写 |
| `CLAUDE.md` | 背离架构段落更新 |

**不变：** DDL、视图、`export_wide.py`、API 字段、`divergence` CHECK 约束。

---

## 通达信 Level 2 算法规格（实现契约）

### 顶背离（红柱区，锚点=金叉 CROSS(DIF,DEA)）

逐 bar `i` 计算：

```
M1  = BARSLAST(金叉)                    # 距最近金叉 bar 数；金叉当日 M1=0
CH1 = HHV(CLOSE, M1+1)                  # close[i-M1 : i] 含当日
DIFH1 = HHV(DIF, M1+1)
MACDH1 = HHV(MACD柱, M1+1)              # 仅 macd_bar>0 的段内峰值
CH2 = REF(CH1, M1+1)                    # i-(M1+1) 位置的 CH1
DIFH2 = REF(DIFH1, M1+1)
MACDH2 = REF(MACDH1, M1+1)
CH3 = REF(CH2, M1+1); DIFH3 = REF(DIFH2, M1+1); MACDH3 = REF(MACDH2, M1+1)

MDIFH2 = MDIF(DIFH2); MDIFT2 = MDIF(DIF, scale=DIFH2)
MDIFH3 = MDIF(DIFH3); MDIFT3 = MDIF(DIF, scale=DIFH3)

直接顶钝化 T1 = (CH1>CH2) ∧ (MDIFT2<MDIFH2) ∧ (MACD>0 ∧ REF(MACD,1)>0) ∧ (MDIFT2≥REF(MDIFT2,1))
隔峰顶钝化 T2 = (CH1>CH3 ∧ CH3>CH2) ∧ (MDIFT3<MDIFH3) ∧ (MACD>0 ∧ REF(MACD,1)>0) ∧ (MDIFT3≥REF(MDIFT3,1))
柱背顶 B  = (MACDH1 < MACDH2) [直接] 或隔峰时 MACDH1 < MACDH3
T = (T1 ∨ T2) ∧ B

结构形成 TG = REF(T,1) ∧ (MDIFT_used < REF(MDIFT_used,1))  # used=2 if T1 else 3
顶背离消失 = (REF(T1,1) ∧ DIFH1≥DIFH2) ∨ (REF(T2,1) ∧ DIFH1≥DIFH3)
```

**写入 DWS：** `TG ∧ ¬消失` 且 `FILTER(dedup=10)` → `top_divergence`

### 底背离（绿柱区，锚点=死叉 CROSS(DEA,DIF)）

对称：`N1=BARSLAST(死叉)`，`CL1=LLV(C,...)`，`MDIFB` 用 `DIFL2/DIFL3` 作 scale，钝化要求 `MDIFB≤REF(MDIFB,1)`，柱背 `MACDL1 > MACDL2`（绿柱区取 macd_bar 最小即最负），TG 当 `MDIFB > REF(MDIFB,1)`。

### MDIF 归一（Tongdaxin INTPART）

```python
def mdif_part(value: float, ref_peak: float) -> int:
    """INTPART(value / 10^PDIFH); PDIFH=INTPART(LOG(|ref|))-1 when |ref|>=10 else 0."""
    if not np.isfinite(value) or not np.isfinite(ref_peak) or ref_peak == 0:
        return 0
    abs_ref = abs(ref_peak)
    pdifh = int(np.floor(np.log10(abs_ref))) - 1 if abs_ref >= 10.0 else 0
    scale = 10.0 ** pdifh
    return int(value / scale)  # truncate toward zero
```

### DDE 同构

- `fast=DDX`, `slow=DDX2`, `bar=DDX`（无 MACD 柱时用 `DDX` 段内 peak 作柱背代理，或 `|DDX|` 段内 HHV）
- 顶背离段内 DDX 峰仍走 **尖刺过滤**（邻域 `[peak-2,peak+3)` 内 ≥0.8×峰值 的 bar <2 → 剔除该峰）
- 窗内任一 NaN → 该 bar 不判（`require_finite=True`）

---

## Task 0: Golden 对标集

**Files:**
- Create: `tests/fixtures/tdx_macd_structure_golden.csv`
- Create: `tests/fixtures/tdx_dde_structure_golden.csv`
- Create: `tests/test_etl/test_divergence_structure.py`（golden 测试骨架）

- [ ] **Step 1: 创建 MACD golden CSV 模板**

```csv
ts_code,trade_date,freq,divergence,note
000001.SZ,20240315,daily,top_divergence,tdx_structure_TG
600519.SH,20240122,daily,bottom_divergence,tdx_structure_TG
```

选 **25 股**（主板/创业板/科创/ST 各若干），通达信加载「MACD顶底结构」指标，记录 **结构形成** 日（非钝化日）。DDE 子集 **10 股** 即可。

- [ ] **Step 2: 写 golden 测试（初始 skip）**

```python
# tests/test_etl/test_divergence_structure.py
import csv
from pathlib import Path
import pytest
import pandas as pd
from backend.etl.calc_macd import MACDCalculator

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "tdx_macd_structure_golden.csv"

def _load_golden():
    if not GOLDEN.exists():
        return []
    with open(GOLDEN, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

@pytest.mark.parametrize("row", _load_golden(), ids=lambda r: f"{r['ts_code']}_{r['trade_date']}")
def test_macd_structure_matches_tdx_golden(row, con_with_stock_history):
    """结构形成日与通达信 golden 对齐（±1 bar 容差）。"""
    ts_code = row["ts_code"]
    expect_date = row["trade_date"]
    expect = row["divergence"]
    calc = MACDCalculator(con_with_stock_history, "daily")
    df = _load_dwd_daily(con_with_stock_history, ts_code)  # 实现见 Step 3
    out = calc._compute_indicators(df)
    hits = out.loc[out["divergence"] == expect, "trade_date"].tolist()
    assert _date_within_tolerance(hits, expect_date, tol=1), (
        f"{ts_code}: expected {expect} near {expect_date}, got {hits}"
    )
```

- [ ] **Step 3: 在 `tests/conftest.py` 增加 `_load_dwd_daily` helper 或复用现有 seed fixture**

若 golden 股无 DWD 数据，用 `tests/conftest.py` 已有 quote seed 模式插入最小 OHLCV 序列。**Task 0 可在 golden CSV 填好前 `@pytest.mark.skip(reason="golden pending")`。**

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/tdx_macd_structure_golden.csv tests/test_etl/test_divergence_structure.py
git commit -m "test: add TDX MACD structure divergence golden scaffold"
```

---

## Task 1: MDIF 与 Cross 辅助函数

**Files:**
- Create: `backend/etl/divergence_structure.py`
- Test: `tests/test_etl/test_divergence_structure.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_etl/test_divergence_structure.py
import numpy as np
from backend.etl.divergence_structure import mdif_part, cross_up, cross_down

def test_mdif_part_matches_tongdaxin():
    assert mdif_part(0.123, ref_peak=0.456) == int(0.123 / 0.1)
    assert mdif_part(-0.05, ref_peak=-0.08) == int(-0.05 / 0.01)

def test_cross_up_detects_golden_cross():
    fast = np.array([0.0, 0.1, 0.2, 0.15])
    slow = np.array([0.05, 0.08, 0.18, 0.16])
    assert cross_up(fast, slow, 1) is True
    assert cross_up(fast, slow, 0) is False
```

- [ ] **Step 2: 运行确认 FAIL**

```bash
pytest tests/test_etl/test_divergence_structure.py::test_mdif_part_matches_tongdaxin -v
```
Expected: `ModuleNotFoundError`

- [ ] **Step 3: 实现辅助函数**

```python
# backend/etl/divergence_structure.py
"""Tongdaxin-style MACD/DDE structure divergence (Level 2: direct + skip-peak + bar peak)."""
from typing import List, Optional
import numpy as np

def mdif_part(value: float, ref_peak: float) -> int:
    if not np.isfinite(value) or not np.isfinite(ref_peak) or ref_peak == 0:
        return 0
    abs_ref = abs(float(ref_peak))
    pdifh = int(np.floor(np.log10(abs_ref))) - 1 if abs_ref >= 10.0 else 0
    scale = 10.0 ** pdifh
    return int(float(value) / scale)

def cross_up(fast: np.ndarray, slow: np.ndarray, i: int) -> bool:
    if i <= 0:
        return False
    a, b = float(fast[i - 1]), float(fast[i])
    c, d = float(slow[i - 1]), float(slow[i])
    if not all(np.isfinite(x) for x in (a, b, c, d)):
        return False
    return a <= c and b > d

def cross_down(fast: np.ndarray, slow: np.ndarray, i: int) -> bool:
    if i <= 0:
        return False
    a, b = float(fast[i - 1]), float(fast[i])
    c, d = float(slow[i - 1]), float(slow[i])
    if not all(np.isfinite(x) for x in (a, b, c, d)):
        return False
    return a >= c and b < d
```

- [ ] **Step 4: 运行 PASS**

```bash
pytest tests/test_etl/test_divergence_structure.py::test_mdif_part_matches_tongdaxin \
       tests/test_etl/test_divergence_structure.py::test_cross_up_detects_golden_cross -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/etl/divergence_structure.py tests/test_etl/test_divergence_structure.py
git commit -m "feat: add MDIF and cross helpers for structure divergence"
```

---

## Task 2: MACD 结构背离核心

**Files:**
- Modify: `backend/etl/divergence_structure.py`
- Test: `tests/test_etl/test_divergence_structure.py`

- [ ] **Step 1: 写合成场景失败测试（直接顶 + 柱背 + TG）**

```python
def test_macd_direct_top_structure_forms_on_tg_day():
    from backend.etl.divergence_structure import compute_macd_structure_divergence
    n = 120
    close = np.linspace(10, 12, n)
    dif = np.zeros(n)
    dea = np.zeros(n)
    macd = np.zeros(n)
    # 手工构造两波红柱：第二波价更高、DIF/MACD柱更低（详见 plan 注释）
    # ... 在测试中逐 bar 赋值 ...
    result = compute_macd_structure_divergence(close, dif, dea, macd, dedup=10)
    tg_days = [i for i, v in enumerate(result) if v == "top_divergence"]
    assert len(tg_days) >= 1
    # 钝化日 T 不应写入（result 在 T 日仍为 None）
```

测试数据构造要点：至少 **2 次金叉**、第二波 `CH1>CH2`、`MDIFT2<MDIFH2`、`MACDH1<MACDH2`，再在 DIF 转向日出现 TG。

- [ ] **Step 2: 实现 `compute_macd_structure_divergence`**

```python
def compute_macd_structure_divergence(
    close, dif, dea, macd_bar, dedup: int = 10,
) -> List[Optional[str]]:
    close = np.asarray(close, dtype=float)
    dif = np.asarray(dif, dtype=float)
    dea = np.asarray(dea, dtype=float)
    macd_bar = np.asarray(macd_bar, dtype=float)
    n = len(close)
    result: List[Optional[str]] = [None] * n
    if n < 3:
        return result

    CH1 = np.full(n, np.nan)
    DIFH1 = np.full(n, np.nan)
    MACDH1 = np.full(n, np.nan)

    T = np.zeros(n, dtype=bool)
    T1 = np.zeros(n, dtype=bool)
    T2 = np.zeros(n, dtype=bool)
    mdift2 = np.full(n, np.nan)
    mdift3 = np.full(n, np.nan)

    for i in range(n):
        if not cross_up(dif, dea, i):
            pass  # gc tracked implicitly via M1 scan
        # M1 = bars since last golden cross at i
        gc = _last_cross_index(dif, dea, i, golden=True)
        if gc is None:
            continue
        m1 = i - gc
        seg = slice(gc, i + 1)
        c_slice = close[seg]
        d_slice = dif[seg]
        m_slice = macd_bar[seg]
        if not np.all(np.isfinite(c_slice)):
            continue
        CH1[i] = np.nanmax(c_slice)
        DIFH1[i] = np.nanmax(d_slice)
        pos = m_slice > 0
        MACDH1[i] = np.nanmax(m_slice[pos]) if pos.any() else np.nan

        ref_i = i - (m1 + 1)
        if ref_i < 0 or np.isnan(CH1[ref_i]):
            continue
        ch2, difh2, macdh2 = CH1[ref_i], DIFH1[ref_i], MACDH1[ref_i]
        ref_i3 = ref_i - (i - ref_i)  # CH3 chain: REF(CH2,M1+1) → use CH1[ref_i - (m1+1)] when valid
        ch3 = CH1[ref_i - (m1 + 1)] if ref_i - (m1 + 1) >= 0 else np.nan
        difh3 = DIFH1[ref_i - (m1 + 1)] if ref_i - (m1 + 1) >= 0 else np.nan
        macdh3 = MACDH1[ref_i - (m1 + 1)] if ref_i - (m1 + 1) >= 0 else np.nan

        m2_val = mdif_part(dif[i], difh2)
        m3_val = mdif_part(dif[i], difh3) if np.isfinite(difh3) else 0
        mdift2[i] = m2_val
        mdift3[i] = m3_val
        mh2 = mdif_part(difh2, difh2)
        mh3 = mdif_part(difh3, difh3) if np.isfinite(difh3) else 0

        red = macd_bar[i] > 0 and (i == 0 or macd_bar[i - 1] > 0)
        bar_ok = np.isfinite(macdh2) and MACDH1[i] < macdh2

        t1 = (CH1[i] > ch2 and m2_val < mh2 and red
              and (i == 0 or mdift2[i] >= mdift2[i - 1]))
        t2 = (np.isfinite(ch3) and CH1[i] > ch3 > ch2 and m3_val < mh3 and red
              and (i == 0 or mdift3[i] >= mdift3[i - 1]))
        bar2 = np.isfinite(macdh3) and MACDH1[i] < macdh3
        T1[i] = t1 and bar_ok
        T2[i] = t2 and bar2
        T[i] = T1[i] or T2[i]

        if i > 0 and T[i - 1]:
            used = mdift2 if T1[i - 1] else mdift3
            used_prev = mdift2[i - 1] if T1[i - 1] else mdift3[i - 1]
            if np.isfinite(used) and np.isfinite(used_prev) and used < used_prev:
                if not _recent(result, i, "top_divergence", dedup):
                    if not (T1[i - 1] and DIFH1[i] >= difh2) and not (T2[i - 1] and np.isfinite(difh3) and DIFH1[i] >= difh3):
                        result[i] = "top_divergence"

    # 底背离：对称 loop，锚点 cross_down(dif, dea)，CL/MDIFB/绿柱柱背
    _compute_macd_bottom_structure(close, dif, dea, macd_bar, result, dedup)
    return result


def _last_cross_index(fast, slow, i, golden: bool) -> Optional[int]:
    for j in range(i, -1, -1):
        if golden and cross_up(fast, slow, j):
            return j
        if not golden and cross_down(fast, slow, j):
            return j
    return None

def _recent(result, i, label, dedup):
    return any(result[j] == label for j in range(max(0, i - dedup), i))
```

**执行者注意：** 上式为骨架；`_compute_macd_bottom_structure` 必须完整实现底背离对称逻辑。实现完成后用 Task 1 合成数据调通，再对照通达信截图微调 `REF(CH3,...)` 索引。

- [ ] **Step 3: 补充隔峰、消失、底背离测试各 1 个**

```bash
pytest tests/test_etl/test_divergence_structure.py -v -k "macd"
```

- [ ] **Step 4: Commit**

```bash
git add backend/etl/divergence_structure.py tests/test_etl/test_divergence_structure.py
git commit -m "feat: MACD structure divergence Level 2 (direct+skip+bar+TG)"
```

---

## Task 3: 接入 calc_macd + RecalcSpec

**Files:**
- Modify: `backend/etl/calc_macd.py`
- Modify: `tests/test_etl/test_calc_macd.py`

- [ ] **Step 1: 更新 RECALC_SPEC**

```python
# calc_macd.py
RECALC_SPEC_DAILY = RecalcSpec(lookback=250, seed=26, event_tail=10, min_rows=27)
RECALC_SPEC_WEEKLY = RecalcSpec(lookback=250, seed=26, event_tail=10, min_rows=27)
```

- [ ] **Step 2: 改 `_compute_divergence`**

```python
from backend.etl.divergence_structure import compute_macd_structure_divergence

def _compute_divergence(self, df: pd.DataFrame) -> list:
    return compute_macd_structure_divergence(
        df["close_qfq"].values,
        df["dif"].values,
        df["dea"].values,
        df["macd_bar"].values,
        dedup=10,
    )
```

- [ ] **Step 3: 删除旧测试** `test_macd_divergence_confirmation_day`、`test_macd_divergence_no_duplicate_within_5_days`、`test_macd_bottom_div_*`（60 窗专用），改为：

```python
def test_macd_divergence_uses_structure_pipeline():
    calc = MACDCalculator.__new__(MACDCalculator)
    # 使用 test_divergence_structure 中已验证的合成 df
    ...
```

- [ ] **Step 4: 运行**

```bash
pytest tests/test_etl/test_calc_macd.py tests/test_etl/test_divergence_structure.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_macd.py tests/test_etl/test_calc_macd.py
git commit -m "feat: wire MACD calculator to structure divergence"
```

---

## Task 4: DDE 结构背离

**Files:**
- Modify: `backend/etl/divergence_structure.py`
- Modify: `backend/etl/calc_dde.py`
- Modify: `tests/test_etl/test_calc_dde.py`

- [ ] **Step 1: 实现 `compute_dde_structure_divergence(close, ddx, ddx2, dedup=10, spike_filter_top=True)`**

逻辑同 MACD，但 `fast=ddx`, `slow=ddx2`, `macd_bar=ddx`（段内柱背取 HHV(DDX) 在红柱区 ddx>0）；段内 DDX 峰应用尖刺过滤：

```python
def _is_ddx_spike(seg_ddx: np.ndarray, peak_idx: int, peak_val: float) -> bool:
    lo = max(0, peak_idx - 2)
    hi = min(len(seg_ddx), peak_idx + 3)
    neighbors = seg_ddx[lo:hi]
    return (neighbors >= peak_val * 0.8).sum() < 2
```

窗内 `ddx` 含 NaN → 该 bar 跳过。

- [ ] **Step 2: calc_dde 接入**

```python
from backend.etl.divergence_structure import compute_dde_structure_divergence

def _compute_divergence(self, df: pd.DataFrame) -> list:
    return compute_dde_structure_divergence(
        df["close_qfq"].values,
        df["ddx"].values,
        df["ddx2"].values,
        dedup=10,
        spike_filter_top=True,
        require_finite=True,
    )
```

- [ ] **Step 3: 替换 DDE 旧 60 窗测试**

删除 `test_dde_divergence_window_60`、`test_dde_divergence_no_tie_false_positive` 等；新增 `test_dde_structure_spike_filtered`。

- [ ] **Step 4: Commit**

```bash
pytest tests/test_etl/test_calc_dde.py tests/test_etl/test_divergence_structure.py -v -k dde
git add backend/etl/divergence_structure.py backend/etl/calc_dde.py tests/test_etl/test_calc_dde.py
git commit -m "feat: DDE structure divergence aligned with DDX/DDX2 crosses"
```

---

## Task 5: Append 与 Incremental 测试迁移

**Files:**
- Modify: `tests/test_etl/test_append_calc.py`
- Modify: `tests/test_etl/test_incremental_calc.py`
- Modify: `backend/etl/base.py`（docstring）

- [ ] **Step 1: append tail 80→300**

```python
# test_macd_append_divergence_matches_full
tail = df_full.iloc[-300:].reset_index(drop=True)
s_row = full_df.iloc[-301]
```

注释更新：`tail >= 250 + 3 cross cycles + dedup`。

- [ ] **Step 2: 删除 `test_incremental_calc.py` 中 `_macd_divergence_oracle` / `_dde_divergence_oracle` 及对应测试**（rolling 法已废弃）。保留 volume oracle。

- [ ] **Step 3: base.py docstring**

```python
def compute_price_signal_divergence(...):
    """Rolling-window price-signal divergence. Volume only; MACD/DDE use divergence_structure."""
```

- [ ] **Step 4: 全量测试**

```bash
pytest tests/test_etl/ -v
```

- [ ] **Step 5: Commit**

```bash
git commit -am "test: migrate append/incremental tests for structure divergence"
```

---

## Task 6: 文档更新

**Files:**
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: spec §6.2 背离表重写** — 记录：锚点金/死叉、直接+隔峰、MDIF、柱背、TG 标注、dedup=10、Volume 仍 60 窗 rolling。

- [ ] **Step 2: §12.20 / §12.29 / §12.60 更新** — 删除「60 bar rolling」MACD/DDE 描述；§12.29 改为「结构形成日 TG，消除前视」。

- [ ] **Step 3: CLAUDE.md** — MACD/DDE 背离条目指向 `divergence_structure.py`；注明对齐通达信顶底结构公式、非东财黑盒。

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md CLAUDE.md
git commit -m "docs: MACD/DDE Level 2 structure divergence spec sync"
```

---

## Task 7: 实库重算与 golden 验收

- [ ] **Step 1: 填 golden CSV 后跑对标**

```bash
pytest tests/test_etl/test_divergence_structure.py -v -k golden
```

MACD ≥85%，DDE ≥70%；不达标则微调 MDIF/柱背/TG 条件并记录于 plan 末尾 Changelog。

- [ ] **Step 2: 全市场重算**

```bash
python -m backend.cli calc --force
```

- [ ] **Step 3: 收尾核对**

- [ ] 全需求覆盖（D1/D2/D3）
- [ ] `pytest tests/ -v` PASS
- [ ] CLAUDE.md + spec 已更新
- [ ] DDL/视图/export 未误改
- [ ] `calc_volume` 仍用 rolling 法

---

## Self-Review

| 检查项 | 结果 |
|--------|------|
| D1 TG 标注 | Task 2 写入逻辑仅 TG |
| D2 线背∧柱背 | T 定义含 bar_ok |
| D3 DDE 同 sprint | Task 4 |
| Volume 不变 | 明确不变 |
| 无 TBD | 骨架代码已给出 |
| Recalc 250 | Task 3/4 |
| Append 等价 | Task 5 tail 300 |

**已知 gap：** `_compute_macd_bottom_structure` 完整代码在 Task 2 由执行者补全（与顶对称）；golden CSV 需人工填通达信标注后才能启用 KPI 门禁。

---

## Changelog（实施时追加）

| 日期 | 变更 |
|------|------|
| 2026-06-09 | 初版计划，用户批准 D1/D2/D3 |
