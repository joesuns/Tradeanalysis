# MACD Weekly B4 target_indices (M2d) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 MACD 周线 APPEND 路径上 `b4_weekly_series_from_daily` 的 O(n²) resample+ewm 重复，将真新日 MACD 周线 `batch_compute` 从 ~45min 压到 ~5–10min，且 **B4 离散列**（`trend`/`turning_point`）与全窗 expanding 完全等价。

**Architecture:** 复制 M2c+ 模式——APPEND 仅对 `new_bars` 对应 `week_end` 索引调用 `b4_weekly_trend_and_crossover_at`；FULL/`calculate()` 保持 `target_indices=None` 全序列。EMA core 已向量化（M2a）；divergence 全窗状态机不变（M2b）。**禁止** v1 捆绑「单次 resample」优化（P2 另立项）。

**Tech Stack:** Python 3.9、numpy、pandas、pytest；`CALC_VECTOR_APPEND=1`（默认）。

**父计划：** `2026-06-13-calc-vector-append-m2.md`（M2）、`2026-06-09-pipeline-30min-optimization.md`

**前置：** M1.1 已合入（`2026-06-13-calc-preflight-merge-m1.1.md`），避免 benchmark 被冷路径污染。

**禁止：** 与 M1.1 同 PR；裁 divergence 状态机；`SPEC_VERSION` bump（算法未变）。

---

## 硬编码锚点（证据）

| 常量 | 值 | 位置 |
|------|-----|------|
| SIG 尾窗 | 245 bar | `calc_router` / batch tails |
| B4 周线日线历史 | 900 日 | `MACD_B4_WEEKLY_DAILY_HISTORY_DAYS` |
| 周线 B4 MACD | (12,26,9) | `B4_MACD_PARAMS_WEEKLY` |
| 日线 B4 MACD | (10,20,7) | `B4_MACD_PARAMS_DAILY` |
| SIGNATURE_COLS | `["close_qfq"]` | `MACDCalculator` |

---

## 验收合同（合入硬门槛）

| ID | 内容 | 门槛 |
|----|------|------|
| Q1 | `test_append_calc.py` MACD weekly | `atol=1e-9` |
| Q2 | B4 离散列 oracle | `trend`/`turning_point` full vs target **完全相等**（含 None） |
| Q3 | `require_b4_weekly_target_indices` | fail-fast（仿 M2c+ volume） |
| E1 | `pytest tests/ -v` | 全绿 |
| P1 | `profile_macd_b4_weekly.py` | 500 股 × 245 bar；末 bar 50/50 |

**观测（非阻塞合入）：** 实库 MACD 周线 batch 秒数；`benchmark_run` E2E。

---

## File Map

| 文件 | 职责 |
|------|------|
| `backend/etl/b4_macd.py` | `b4_weekly_series_from_daily(..., target_indices)` |
| `backend/etl/calc_macd.py` | `require_*`、`_apply_b4_*`、`append_calculate` |
| `backend/etl/calc_batch_append.py` | weekly `batch_append_macd` 接线 |
| `scripts/profile_macd_b4_weekly.py` | **新建** profiling |
| `tests/test_etl/test_b4_macd_weekly_append.py` | **新建** B4 oracle |
| `tests/test_etl/test_vector_append.py` | batch weekly 补充（可选） |
| `CLAUDE.md` / M2 plan / pipeline plan | 文档 |

**不改动：** schema、export_wide、orchestrator 注册表、`SPEC_VERSION`。

---

### Task 1: B4 weekly `target_indices` oracle 测试

**Files:**
- Create: `tests/test_etl/test_b4_macd_weekly_append.py`

- [ ] **Step 1: Write failing tests**

```python
"""M2d: MACD weekly B4 target_indices equivalence."""
import numpy as np
import pandas as pd
import pytest

from backend.etl.b4_macd import b4_weekly_series_from_daily


def _synthetic_daily(n_days: int = 600, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-01", periods=n_days, freq="B")
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n_days))
    return pd.DataFrame({
        "trade_date": [d.strftime("%Y%m%d") for d in dates],
        "close_qfq": close,
    })


def _week_ends_from_daily(daily: pd.DataFrame, n_weeks: int) -> list:
    from backend.etl.b4_macd import convert_daily_to_weekly_resample_w
    w = convert_daily_to_weekly_resample_w(daily)
    return w["trade_date"].astype(str).tail(n_weeks).tolist()


def test_b4_weekly_target_indices_matches_full_expanding():
    daily = _synthetic_daily(600)
    week_ends = _week_ends_from_daily(daily, 120)
    full_t, full_c = b4_weekly_series_from_daily(daily, week_ends)
    for idx in [0, 1, 59, 119]:
        t_sub, c_sub = b4_weekly_series_from_daily(
            daily, week_ends, target_indices={idx},
        )
        assert t_sub[idx] == full_t[idx]
        assert c_sub[idx] == full_c[idx]
        for j, (tv, cv) in enumerate(zip(t_sub, c_sub)):
            if j != idx:
                assert tv is None
                assert cv is None


def test_b4_weekly_target_indices_multi_bar():
    daily = _synthetic_daily(600)
    week_ends = _week_ends_from_daily(daily, 80)
    full_t, full_c = b4_weekly_series_from_daily(daily, week_ends)
    targets = {10, 40, 79}
    t_sub, c_sub = b4_weekly_series_from_daily(
        daily, week_ends, target_indices=targets,
    )
    for idx in targets:
        assert t_sub[idx] == full_t[idx]
        assert c_sub[idx] == full_c[idx]
```

- [ ] **Step 2: Run — expect FAIL**

Run: `python3 -m pytest tests/test_etl/test_b4_macd_weekly_append.py -v`  
Expected: FAIL `unexpected keyword argument 'target_indices'`

- [ ] **Step 3: Implement `b4_weekly_series_from_daily` target_indices**

In `backend/etl/b4_macd.py`:

```python
from typing import List, Optional, Set, Tuple

def b4_weekly_series_from_daily(
    daily_df: pd.DataFrame,
    week_end_dates: List[str],
    target_indices: Optional[Set[int]] = None,
) -> Tuple[List[Optional[str]], List[Optional[str]]]:
    n = len(week_end_dates)
    trends: List[Optional[str]] = [None] * n
    crosses: List[Optional[str]] = [None] * n
    indices = range(n) if target_indices is None else sorted(target_indices)
    for i in indices:
        if i < 0 or i >= n:
            continue
        t, c = b4_weekly_trend_and_crossover_at(daily_df, week_end_dates[i])
        trends[i] = t
        crosses[i] = c
    return trends, crosses
```

- [ ] **Step 4: Run — expect PASS**

Run: `python3 -m pytest tests/test_etl/test_b4_macd_weekly_append.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/etl/b4_macd.py tests/test_etl/test_b4_macd_weekly_append.py
git commit -m "feat(b4): weekly MACD series target_indices for APPEND path"
```

---

### Task 2: `require_b4_weekly_target_indices` gate

**Files:**
- Modify: `backend/etl/calc_macd.py`
- Modify: `tests/test_etl/test_b4_macd_weekly_append.py`

- [ ] **Step 1: Write gate tests**

```python
def test_require_b4_weekly_target_indices_gate():
    from backend.etl.calc_macd import (
        MACDCalculator,
        require_b4_weekly_target_indices,
    )
    import pandas as pd

    df = pd.DataFrame({
        "trade_date": ["20260101", "20260108", "20260115"],
        "close_qfq": [10.0, 10.5, 11.0],
    })
    assert require_b4_weekly_target_indices(df, ["20260115"]) == [2]
    with pytest.raises(ValueError, match="new_bars"):
        require_b4_weekly_target_indices(df, None)
    with pytest.raises(ValueError, match="duplicate"):
        require_b4_weekly_target_indices(df, ["20260101", "20260101"])
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Add helpers to `calc_macd.py`**

```python
def resolve_b4_weekly_target_indices(df: pd.DataFrame, new_bars: list) -> list:
    td_set = {str(d) for d in new_bars}
    return [i for i, d in enumerate(df["trade_date"].astype(str)) if d in td_set]


def require_b4_weekly_target_indices(
    df: pd.DataFrame,
    new_bars: Optional[list],
    *,
    ts_code: str = "",
) -> list:
    prefix = f"ts_code={ts_code} " if ts_code else ""
    if new_bars is None:
        raise ValueError(f"{prefix}APPEND MACD weekly B4 requires new_bars")
    if len(new_bars) == 0:
        raise ValueError(f"{prefix}APPEND MACD weekly B4 new_bars must be non-empty")
    indices = resolve_b4_weekly_target_indices(df, new_bars)
    if len(indices) != len(new_bars):
        str_bars = [str(d) for d in new_bars]
        if len(set(str_bars)) != len(str_bars):
            raise ValueError(f"{prefix}duplicate dates in new_bars: {new_bars}")
        td_in_df = set(df["trade_date"].astype(str))
        missing = [s for s in str_bars if s not in td_in_df]
        raise ValueError(
            f"{prefix}new_bars not in tail df: missing={missing} new_bars={new_bars}"
        )
    return indices
```

- [ ] **Step 4: Run gate tests — PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_macd.py tests/test_etl/test_b4_macd_weekly_append.py
git commit -m "feat(macd): require_b4_weekly_target_indices APPEND gate"
```

---

### Task 3: `_apply_b4_trend_and_zone` + `_compute_macd_derived` 接线

**Files:**
- Modify: `backend/etl/calc_macd.py`
- Modify: `tests/test_etl/test_b4_macd_weekly_append.py`

- [ ] **Step 1: Write integration test（MACDCalculator weekly derived）**

```python
def test_macd_weekly_derived_b4_target_matches_full(memory_con):
    """_compute_macd_derived: target_indices only changes B4 at target bars."""
    import numpy as np
    import pandas as pd
    from backend.etl.calc_macd import MACDCalculator

    # Build minimal weekly tail df + synthetic daily_for_b4 via monkeypatch load
    # Compare full derived vs target_indices={last_idx} on trend/turning_point
```

（实施时用 `conftest` memory db 或纯 DataFrame 路径：直接调 `_compute_macd_derived` on synthetic weekly df + daily_for_b4。）

- [ ] **Step 2: Modify `_apply_b4_trend_and_zone`**

```python
    def _apply_b4_trend_and_zone(
        self,
        df: pd.DataFrame,
        daily_for_b4: Optional[pd.DataFrame] = None,
        b4_target_indices: Optional[set] = None,
    ) -> pd.DataFrame:
        ...
        elif daily_for_b4 is not None and not daily_for_b4.empty:
            week_ends = df["trade_date"].astype(str).tolist()
            trends, crosses = b4_weekly_series_from_daily(
                daily_for_b4, week_ends, target_indices=b4_target_indices,
            )
            df["trend"] = trends
            df["turning_point"] = crosses
```

- [ ] **Step 3: Pass `b4_target_indices` from `_compute_macd_derived`**

```python
    def _compute_macd_derived(
        self,
        df: pd.DataFrame,
        daily_for_b4: Optional[pd.DataFrame] = None,
        target_indices: Optional[set] = None,
        b4_target_indices: Optional[set] = None,
    ) -> pd.DataFrame:
        ...
        df = self._apply_b4_trend_and_zone(
            df, daily_for_b4=daily_for_b4, b4_target_indices=b4_target_indices,
        )
```

**约定：** APPEND 路径 `b4_target_indices = target_indices`（同一 new_bars 映射）；divergence 仍用 `target_indices` 写裁剪。

- [ ] **Step 4: Run tests — PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_macd.py tests/test_etl/test_b4_macd_weekly_append.py
git commit -m "feat(macd): wire B4 weekly target_indices into derived path"
```

---

### Task 4: `append_calculate` weekly 路径（Q1 门禁）

**Files:**
- Modify: `backend/etl/calc_macd.py`
- Verify: `tests/test_etl/test_append_calc.py`

- [ ] **Step 1: Update `append_calculate`**

```python
        daily_b4 = None
        b4_target = None
        if self.freq == "weekly":
            b4_start = self._weekly_b4_daily_start(calc_date)
            daily_b4 = self._load_daily_for_b4(
                ts_code, start_date=b4_start, end_date=calc_date,
            )
            b4_target = set(require_b4_weekly_target_indices(
                df, new_bars, ts_code=ts_code,
            ))
        df = self._compute_macd_core(df, ema_seeds=seeds)
        target_idx = set(b4_target) if b4_target is not None else None
        df = self._compute_macd_derived(
            df,
            daily_for_b4=daily_b4,
            target_indices=target_idx,
            b4_target_indices=target_idx,
        )
```

（删除对 `_compute_indicators` 的单一调用，避免 B4 全窗。）

- [ ] **Step 2: Run append oracle**

Run: `python3 -m pytest tests/test_etl/test_append_calc.py -v -k macd`  
Expected: PASS（`atol=1e-9`）

- [ ] **Step 3: Commit**

```bash
git add backend/etl/calc_macd.py
git commit -m "perf(macd): append_calculate weekly B4 target_indices only"
```

---

### Task 5: `batch_append_macd` weekly 接线

**Files:**
- Modify: `backend/etl/calc_batch_append.py`

- [ ] **Step 1: In vector path weekly branch, after `target_idx` built:**

```python
        if freq == "weekly" and new_bars:
            from backend.etl.calc_macd import require_b4_weekly_target_indices
            b4_idx = set(require_b4_weekly_target_indices(
                df, new_bars, ts_code=ts_code,
            ))
            target_idx = b4_idx
```

Pass to `_compute_macd_derived`:

```python
            out = calc._compute_macd_derived(
                base,
                daily_for_b4=daily_b4,
                target_indices=target_idx or None,
                b4_target_indices=target_idx or None,
            )
```

Non-vector weekly path: same `require_b4_weekly_target_indices` before `_compute_indicators` split — prefer unified `_compute_macd_core` + `_compute_macd_derived` for weekly APPEND.

- [ ] **Step 2: Run batch tests**

Run: `python3 -m pytest tests/test_etl/test_vector_append.py tests/test_etl/test_batch_append_calc.py -v`

- [ ] **Step 3: Commit**

```bash
git add backend/etl/calc_batch_append.py
git commit -m "perf(macd): batch_append_macd weekly B4 target_indices"
```

---

### Task 6: Profiling 脚本

**Files:**
- Create: `scripts/profile_macd_b4_weekly.py`

- [ ] **Step 1: Create script（仿 `profile_volume_trend_v2.py`）**

```python
#!/usr/bin/env python3
"""M2d: profile MACD weekly B4 b4_weekly_series_from_daily cost."""
import argparse
import time
import numpy as np
import pandas as pd
from backend.etl.b4_macd import b4_weekly_series_from_daily, convert_daily_to_weekly_resample_w

def bench_expanding(daily, week_ends, repeat=1):
    t0 = time.perf_counter()
    for _ in range(repeat):
        b4_weekly_series_from_daily(daily, week_ends)
    return time.perf_counter() - t0

def bench_target_last(daily, week_ends, repeat=1):
    t0 = time.perf_counter()
    for _ in range(repeat):
        b4_weekly_series_from_daily(daily, week_ends, target_indices={len(week_ends) - 1})
    return time.perf_counter() - t0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stocks", type=int, default=500)
    parser.add_argument("--bars", type=int, default=245)
    args = parser.parse_args()
    # loop stocks, synthetic daily 600d, week_ends=tail bars
    # print expanding vs last-only ms/stock and 5389 extrapolation
```

- [ ] **Step 2: Run profile**

Run: `python3 scripts/profile_macd_b4_weekly.py --stocks 500 --bars 245`  
Expected: last-only **>>** faster than expanding（目标 ~200× 量级）

- [ ] **Step 3: Commit**

```bash
git add scripts/profile_macd_b4_weekly.py
git commit -m "chore: profile_macd_b4_weekly M2d benchmark script"
```

---

### Task 7: 文档与全量验收

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/plans/2026-06-13-calc-vector-append-m2.md`
- Modify: `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md`

- [ ] **Step 1: CLAUDE.md** — M2 段补 M2d B4 weekly `target_indices` + profile 命令

- [ ] **Step 2: 全量 pytest**

Run: `python3 -m pytest tests/ -v`  
Expected: PASS

- [ ] **Step 3: 实库签字（运维）**

```bash
python3 -m backend.cli run --date <真新日> --skip-export  # 或 benchmark_run
grep 'calc.batch_compute: MACD周线' /tmp/run.log
```

记录 MACD 周线 batch 秒数，写入 pipeline plan 附录 M2d。

- [ ] **Step 4: Commit docs**

```bash
git add CLAUDE.md docs/superpowers/plans/
git commit -m "docs: M2d MACD weekly B4 target_indices sign-off slot"
```

---

## Self-Review（计划自检）

| 检查 | 结果 |
|------|------|
| Spec Q1–Q3 各有 Task | ✅ Task 1–5 |
| `append_calculate` 覆盖 | ✅ Task 4 |
| P2 单次 resample 未纳入 | ✅ 仅 `target_indices` |
| Placeholder/TBD | 无 |
| `SPEC_VERSION` / schema | 未改 |

---

## Execution Handoff

**Plan saved to `docs/superpowers/plans/2026-06-13-calc-macd-b4-weekly-m2d.md`.**

**依赖：** 先完成 `2026-06-13-calc-preflight-merge-m1.1.md`。

**Two execution options:**

1. **Subagent-Driven (recommended)** — 每 Task 派 subagent + review  
2. **Inline Execution** — 本会话按 Task 执行

**Which approach?**
