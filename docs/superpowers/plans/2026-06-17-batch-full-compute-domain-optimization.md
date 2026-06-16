# Batch FULL Compute Domain + MACD/Volume Weekly Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 calc **算域=写域** 数据契约；将 spec migration / batch FULL 周线 **MACD B4** 与 **Volume trend** 从 O(n²) 降至可接受墙钟；提供 **M0 运维 UX**（spec 状态可见、dry-run）；**不改变** DWS 语义、`SPEC_VERSION` 值、schema。

**Architecture:** 三层域契约（读域 245 tail / 算域 `[recalc_start,calc_date]` / 写域同算域）+ Volume FULL 先接 M2c `target_indices` + MACD B4 单次 resample 快路径（expanding 保留作 oracle，`CALC_B4_WEEKLY_FAST=0` 回滚）。与 spec-gate hotfix **独立 PR**。

**Tech Stack:** Python 3.9+, DuckDB, numpy, pandas, pytest, `profile_macd_b4_weekly.py`, `profile_volume_trend_v2.py`

**日期：** 2026-06-17  
**状态：** M5 E1–E3 证据完成；**Final DA/SA 双签 + 合 main 待批**  
**审批：** 用户同意 — 2026-06-17（DA+SA review 门禁 + 违规定停）  
**ACCEPTANCE_DATE：** `20260616`（S2 实库 pilot 与 E1–E3 一致）

**父计划 / 关联：**
- `2026-06-16-spec-gate-hotfix-impl.md`（已实施，**本 plan 不重复**）
- `2026-06-13-calc-macd-b4-weekly-m2d.md`（APPEND target_indices；本 plan = **FULL 路径 P2**）
- `2026-06-09-pipeline-30min-optimization.md`（M2c volume trend）
- `2026-06-09-daily-runbook.md`（migration SOP）

---

## 治理协议（实施时必须遵守）

### STOP 条件 — 立即停止推进，等待用户审核

出现以下任一情况，**禁止**继续下一 Task / 下一 Milestone，输出 STOP 报告并等待用户明确「继续」：

| # | 条件 |
|---|------|
| S1 | 任一 **Q/E 验收** 未通过（见 §验收矩阵） |
| S2 | 需 **bump `SPEC_VERSION`**、改 B4/Volume 算法公式、或改 `v_*_latest` DDL |
| S3 | 需 **`rebuild_all_dwd(con)` 无 ts_codes** 或日常全市场 FULL（非 spec migration 窄窗） |
| S4 | 计划外文件改动（export layout、schema 新表、orchestrator 12 路由注册变更） |
| S5 | expanding oracle 与 fast 路径 **任一 index 不等**（含 None 位置） |
| S6 | `pytest tests/ -v` 全量失败且非本 plan 引入可解释范围 |
| S7 | DA 或 SA Review 表任一项 **Fail** |
| S8 | 违反 `engineering-protocol` 决策树（习惯性全量、跳门禁、无等价性证明的优化） |

**STOP 报告模板：**

```markdown
## STOP — [Milestone/Task]
- 触发：S?
- 现象：（日志/测试输出）
- 影响：（数据质量 / 范围 / SLA）
- 建议：（回滚 / 修 plan / 缩小范围）
- 等待用户审核后继续。
```

### DA + SA Review 门禁（每 Milestone 末）

| Milestone | DA 签字 | SA 签字 | 用户 |
|-----------|---------|---------|------|
| M0 | ☑ | ☑ | ☑ |
| M1 | ☑ | ☑ | ☑ |
| M2 | ☑ | ☑ | ☑ |
| M3 | ☑ | ☑ | ☑ |
| M4 | ☑ | ☑ | ☑ |
| M5 | ☐ | ☐ | ☐ |

**未双签通过 → 不得进入下一 Milestone。** M0–M4 追溯双签：2026-06-17（见 `2026-06-17-compute-domain-da-sa-retro-review.md`）。

---

## ① 背景与硬编码锚点

| 常量 | 值 | 位置 |
|------|-----|------|
| SIG / tail 窗 | 245 bar | `calc_router` / batch tails |
| RECALC_SPEC weekly lookback | 250 | `MACDCalculator.RECALC_SPEC_WEEKLY` 等 |
| MACD B4 周线 daily 历史 | 900 日 | `MACD_B4_WEEKLY_DAILY_HISTORY_DAYS` |
| Volume weekly anchor | 30 | `VOLUME_TREND_V2_WEEKLY` |
| 实库 MACD 周线 FULL 吞吐 | ~1 股/s | run_id `3f1f11a0` |
| profile expanding | 760 ms/股 | `profile_macd_b4_weekly.py` 100 股 |
| profile target 末 bar | 11 ms/股 | 同上，67× |

**反面教材（本 plan 要避免）：** 用 `run --skip-export` 做 migration 幂等验收 → 触发 4 路由 auto FULL。

---

## ② 数据契约 ADR（M0 写入 spec 附录）

### 三层域

| 域 | 含义 | FULL/spec refresh |
|----|------|-------------------|
| **读域** | tail 245 bar（`batch_load_quote_tails`） | 不变 |
| **算域** | `[recalc_start, calc_date]` 内 bar 索引 | **必须**只算这些 index |
| **写域** | 同算域（`insert_dws_batch_multi` narrow write） | 已存在，compute 须对齐 |

**禁止：** 写 250 bar、算 245 bar 全窗 expanding（当前 MACD B4 / Volume trend FULL 路径）。

---

## ③ 目标与非目标

### 目标

- `resolve_compute_indices(df, recalc_start, calc_date)` 统一推导算域索引
- Volume `batch_full_volume` 传 `trend_target_indices`
- MACD B4 单次 resample 快路径 + oracle 双轨
- `cli ops spec-status`、`calc --refresh-spec --dry-run`
- migration 单页 SOP（runbook）
- 实库 pilot 50 股 E1 签字

### 非目标

- 不改 DWD / 不 `rebuild_all_dwd` 无 `ts_codes`
- 不改 Calculator `SPEC_VERSION` 字符串
- 不改 schema / export_wide layout
- 不做 ComponentSpec / run Job 化解耦（L3 另立项）
- 不优化 MA/KPattern/PP/DDE 周线（无 O(n²) 证据；DDE 尾窗已优化）

---

## ④ 验收矩阵

| ID | 类型 | 内容 | 门槛 |
|----|------|------|------|
| Q1 | 单元 | B4 fast vs expanding（全索引 + 写窗子集 + 跨年 seed） | `trend`/`turning_point` **完全相等** |
| Q2 | 单元 | Volume trend 写窗 vs expanding | 写窗 index 逐 bar 相等 |
| Q3 | 单元 | `resolve_compute_indices` 边界（None recalc_start、缺 bar） | 确定性 |
| Q4 | 集成 | `test_append_calc` MACD/Volume weekly | `atol=1e-9` 不退化 |
| Q5 | profile | MACD weekly FULL fast | ≥**5×** vs expanding（100 股 × 245 bar） |
| Q6 | profile | Volume weekly trend 写窗 | ≥**10×** vs full expanding |
| E1 | 实库 | 50 股 pilot @ `ACCEPTANCE_DATE` | B4 列 + vol trend 100% match oracle |
| E2 | DQ | `v_dq_spec_freshness` | 已刷指标 `spec_stale=0` |
| E3 | 回归 | APPEND 同日复跑 | 墙钟不退化（±10%） |

---

## ⑤ 里程碑总览

```text
M0  ADR + runbook S0 + PR scope + STOP 协议     → DA+SA Review #0
M1  ops spec-status + refresh-spec --dry-run     → DA+SA Review #1
M2  resolve_compute_indices + Volume FULL        → DA+SA Review #2
M3  MACD B4 single-resample fast path            → DA+SA Review #3
M4  profile + docs + pytest 全绿                 → DA+SA Review #4
M5  实库 E1–E3 + Final Sign-off                  → DA+SA + 用户
```

**实施顺序（DA 裁定）：** M0 → M1 → M2 → M3 → M4 → M5（Volume 先于 MACD P2）。

---

## M0 — ADR、Runbook S0、PR Scope

### 任务

- [x] **M0.1** 创建分支 `feat/batch-full-compute-domain`（**禁止**与 spec-gate hotfix 未合并 diff 混用）
- [x] **M0.2** 在 `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` §calc 增 **三层域** 段落（3–5 句，链到本 plan）
- [x] **M0.3** 更新 `docs/superpowers/plans/2026-06-09-daily-runbook.md` **Migration 单页**：

```markdown
### Spec migration（一次性，禁并行 run/calc）

1. cp data/tradeanalysis.duckdb data/tradeanalysis.pre-migrate.duckdb
2. python3 -m backend.cli ops spec-status --date YYYYMMDD
3. CALC_AUTO_SPEC_REFRESH=0 python3 -m backend.cli calc --refresh-spec IND --date YYYYMMDD
4. python3 -m backend.cli ops spec-status --date YYYYMMDD  # 确认 stale=0
5. EXPORT_SPEC_GATE=1 python3 -m backend.cli export --date YYYYMMDD
6. python3 -m scripts.health_check  # Section J

禁止：migration 未完成时用 run --skip-export 测幂等。
```

- [x] **M0.4** 记录 `ACCEPTANCE_DATE=20260616`（与 S2 一致）

### M0 DA+SA Review #0

| 角色 | 检查项 | Pass |
|------|--------|------|
| DA | ADR 三层域与 export 截面一致 | ☐ |
| SA | PR 与 spec-gate hotfix 分离 | ☐ |
| SA | STOP 协议可执行 | ☐ |
| 用户 | migration 单页命令可跑 | ☐ |

---

## M1 — 运维 UX（spec-status + dry-run）

### Task 1: `resolve_compute_indices` 工具函数（先建，M2 使用）

**Files:**
- Create: `backend/etl/calc_compute_domain.py`
- Create: `tests/test_etl/test_calc_compute_domain.py`

- [ ] **Step 1: Write failing test**

```python
"""Compute domain indices for batch FULL."""
import pandas as pd

from backend.etl.calc_compute_domain import resolve_compute_indices


def test_resolve_compute_indices_inclusive_range():
    df = pd.DataFrame({
        "trade_date": ["20260101", "20260108", "20260115", "20260122"],
    })
    idx = resolve_compute_indices(df, "20260108", "20260122")
    assert idx == [1, 2, 3]


def test_resolve_compute_indices_none_recalc_returns_all():
    df = pd.DataFrame({"trade_date": ["20260101", "20260108"]})
    idx = resolve_compute_indices(df, None, "20260108")
    assert idx == [0, 1]
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `pytest tests/test_etl/test_calc_compute_domain.py -v`  
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
"""Compute-domain helpers: align batch FULL compute with narrow write window."""
from typing import List, Optional

import pandas as pd


def resolve_compute_indices(
    df: pd.DataFrame,
    recalc_start: Optional[str],
    calc_date: str,
) -> List[int]:
    """Row indices with recalc_start <= trade_date <= calc_date (inclusive).

    When recalc_start is None, all rows in df are included.
    """
    if df is None or len(df) == 0:
        return []
    tds = df["trade_date"].astype(str).tolist()
    if recalc_start is None:
        return list(range(len(tds)))
    out: List[int] = []
    for i, td in enumerate(tds):
        if recalc_start <= td <= calc_date:
            out.append(i)
    return out
```

- [ ] **Step 4: Run test — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_compute_domain.py tests/test_etl/test_calc_compute_domain.py
git commit -m "feat(calc): resolve_compute_indices for batch FULL compute domain"
```

---

### Task 2: `cli ops spec-status`

**Files:**
- Create: `backend/etl/ops_spec_status.py`
- Modify: `backend/cli.py` — `_register_ops_subparsers`
- Create: `tests/test_cli/test_ops_spec_status.py`

- [ ] **Step 1: Write failing test**（in-memory DuckDB + `v_dq_spec_freshness` mock 或 skip if view missing）

```python
def test_cmd_spec_status_prints_stale_rows(capsys, tmp_path):
    # Use duckdb :memory: + minimal v_dq_spec_freshness fixture via schema DDL slice
    # OR mock ops_spec_status.fetch_spec_freshness_rows
    from backend.etl.ops_spec_status import format_spec_status_table

    rows = [
        ("macd", "daily", "20260616", 5375, 0, 5375, "v3"),
    ]
    text = format_spec_status_table(rows)
    assert "macd" in text
    assert "5375" in text
```

- [ ] **Step 2: Implement `ops_spec_status.py`**

```python
"""Read-only spec freshness report for ops."""
from typing import List, Tuple

import duckdb

from backend.etl.calc_spec_gate import resolve_weekly_anchor_trade_date


def fetch_spec_freshness_rows(con, trade_date: str) -> List[Tuple]:
    rows = con.execute(
        """
        SELECT indicator, freq, anchor_trade_date, total, spec_ok, spec_stale, expected_spec
        FROM v_dq_spec_freshness
        WHERE anchor_trade_date IN (?, ?)
        ORDER BY indicator, freq
        """,
        [trade_date, resolve_weekly_anchor_trade_date(con, trade_date) or ""],
    ).fetchall()
    return rows


def suggest_refresh_spec(rows) -> str:
    stale_inds = sorted({r[0] for r in rows if int(r[5]) > 0})
    if not stale_inds:
        return ""
    return "calc --refresh-spec " + ",".join(stale_inds) + " --date ..."
```

- [ ] **Step 3: Wire CLI**

```python
# backend/cli.py _register_ops_subparsers
p = ops_sp.add_parser("spec-status", help="Show v_dq_spec_freshness for anchor date(s)")
p.add_argument("--date", required=True, help="Analysis anchor YYYYMMDD")
```

- [ ] **Step 4: pytest + commit**

Run: `pytest tests/test_cli/test_ops_spec_status.py -v`

---

### Task 3: `calc --refresh-spec --dry-run`

**Files:**
- Modify: `backend/etl/calc_spec_refresh.py`
- Modify: `backend/cli.py` — calc parser `--dry-run`
- Modify: `tests/test_etl/test_calc_spec_refresh.py`

- [ ] **Step 1: Failing test**

```python
def test_run_refresh_spec_dry_run_no_writes(in_memory_con):
    from backend.etl.calc_spec_refresh import run_refresh_spec
    # seed one stale macd row in v_dq / dws ...
    summary = run_refresh_spec(con, "20260616", ["macd"], dry_run=True)
    assert summary.get("dry_run") is True
    assert summary.get("refreshed", 0) == 0
    assert summary.get("stale_groups")
```

- [ ] **Step 2: Implement** — `run_refresh_spec(..., dry_run=False)` 在 stale_groups 非空时 **不**调用 `_execute_spec_stale_batch_full`，仅返回 `stale_groups` 规模与 indicators 列表

- [ ] **Step 3: Commit + M1 Review**

### M1 DA+SA Review #1

| 角色 | 检查项 | Pass |
|------|--------|------|
| DA | `spec-status` 读 `v_dq_spec_freshness` 与 export 同 anchor | ☐ |
| DA | dry-run **零 DWS 写** | ☐ |
| SA | CLI 不破坏现有 `calc --refresh-spec` | ☐ |
| SA | 测试不依赖实库 | ☐ |

---

## M2 — Volume FULL + 算域契约

### Task 4: Wire Volume batch FULL

**Files:**
- Modify: `backend/etl/calc_batch_append.py` — `batch_full_volume`
- Modify: `tests/test_etl/test_calc_spec_refresh.py` or new `test_batch_full_compute_domain.py`

- [ ] **Step 1: Failing test** — mock VolumeCalculator，断言 `_compute_volume_derived` 收到非 None `trend_target_indices` 且 len == 写窗 bar 数

- [ ] **Step 2: Change `batch_full_volume` compute lambda**

```python
def batch_full_volume(con, freq, ts_codes, calc_date, recalc_start, quote_groups, state_map=None):
    from backend.etl.calc_compute_domain import resolve_compute_indices
    # ...
    def _compute(c, _code, df):
        idx = resolve_compute_indices(df, recalc_start, calc_date)
        return c._compute_volume_derived(
            c._compute_volume_core(df),
            trend_target_indices=idx or None,
        )
```

- [ ] **Step 3: Q2 oracle test** — 扩展 `tests/test_etl/test_vector_append.py` 或新建：

```python
def test_volume_trend_write_window_matches_expanding():
    # full expanding compute_volume_trend_series(..., target_indices=None)
    # vs target_indices=resolve_compute_indices(...)
    # assert equal on window indices
```

- [ ] **Step 4: pytest + commit**

Run: `pytest tests/test_etl/test_calc_compute_domain.py tests/test_etl/test_vector_append.py -v -k volume`

### M2 DA+SA Review #2

| 角色 | 检查项 | Pass |
|------|--------|------|
| DA | 算域=写域（Volume trend 列） | ☐ |
| DA | Q2 PASS | ☐ |
| SA | APPEND 路径未改 | ☐ |
| SA | profile Q6 ≥10×（或记录 baseline 待 M4） | ☐ |

---

## M3 — MACD B4 Single-Resample Fast Path

### Task 5: Fast path in `b4_macd.py`

**Files:**
- Modify: `backend/etl/b4_macd.py`
- Modify: `backend/config.py` — `CALC_B4_WEEKLY_FAST=1` default
- Extend: `tests/test_etl/test_b4_macd_weekly_append.py`

- [ ] **Step 1: Failing test `test_b4_weekly_fast_path_matches_expanding_all_indices`**

对 `seed in [42, 99, 2025]`、`n_weeks in [60, 120, 245]` 抽样 index 全比较 `b4_weekly_series_from_daily_fast` vs `b4_weekly_series_from_daily` expanding。

- [ ] **Step 2: Implement fast function**

核心逻辑（须与 `b4_weekly_trend_and_crossover_at` 等价）：

```python
def b4_weekly_series_from_daily_fast(
    daily_df: pd.DataFrame,
    week_end_dates: List[str],
    target_indices: Optional[Set[int]] = None,
) -> Tuple[List[Optional[str]], List[Optional[str]]]:
    """Single resample+ewm; map each week_end to prefix weekly index."""
    # 1. For each target index i, week_end = week_end_dates[i]
    # 2. sub = daily[daily.trade_date <= week_end]
    # 3. Reuse cached weekly MACD series built incrementally OR
    #    build weekly from sub via ONE resample per unique prefix (careful: equivalence)
    # SA/DA 要求：以 expanding 为 oracle，fast 仅 refactor
    ...
```

**实现提示（SA 默认）：** 对每个 `target_indices` 中的 `i`，调用内部 `_b4_at_week_end_from_daily(daily_df, week_end_dates[i])` 但 **weekly dif/dea/macd 序列**从「daily 前缀一次 resample」缓存取得，避免重复 `convert_daily_to_weekly_resample_w`+`macd_ewm_columns`。若等价性无法在 2 天内证明 → **STOP S5**，回退仅 M2。

- [ ] **Step 3: Wire `calc_macd._apply_b4_trend_and_zone`**

```python
from backend.config import CALC_B4_WEEKLY_FAST
if CALC_B4_WEEKLY_FAST:
    from backend.etl.b4_macd import b4_weekly_series_from_daily_fast as _b4_series
else:
    _b4_series = b4_weekly_series_from_daily
```

- [ ] **Step 4: Wire `batch_full_macd` weekly**

```python
def _compute(c, ts_code, df):
    idx = resolve_compute_indices(df, recalc_start, calc_date)
    b4_target = set(idx) if freq == "weekly" else None
    target_idx = set(idx) if freq == "weekly" else None
    return c._compute_indicators(
        df, ema_seeds=seeds, daily_for_b4=daily_b4,
        # extend _compute_indicators to pass through OR use _compute_macd_derived directly
    )
```

需扩展 `MACDCalculator._compute_indicators` 签名接受 optional `target_indices` / `b4_target_indices`（与 `_compute_macd_derived` 已有参数对齐）。

- [ ] **Step 5: Q1 + Q5 profile**

Run:
```bash
pytest tests/test_etl/test_b4_macd_weekly_append.py -v
python3 scripts/profile_macd_b4_weekly.py --stocks 100 --bars 245
```

Expected: speedup ≥5× on write-window mode（新增 profile 模式 `--mode write_window`）

- [ ] **Step 6: Commit**

### M3 DA+SA Review #3

| 角色 | 检查项 | Pass |
|------|--------|------|
| DA | Q1 跨年/边界 week PASS | ☐ |
| DA | `CALC_B4_WEEKLY_FAST=0` 与 expanding 一致 | ☐ |
| SA | Q5 ≥5× | ☐ |
| SA | 未改 MACD `SPEC_VERSION` | ☐ |

---

## M4 — 文档、Profile、全量 pytest

### Task 6: Docs + CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` — calc 三层域、`ops spec-status`、`CALC_B4_WEEKLY_FAST`
- Modify: `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md` — M2d+ / P2+ 签字槽

- [ ] **M4.1** CLAUDE.md 常用命令增：

```bash
python3 -m backend.cli ops spec-status --date 20260616
python3 -m backend.cli calc --refresh-spec macd --date 20260616 --dry-run
```

- [ ] **M4.2** pipeline plan 附录增 P2+ 行（profile 数字、E3 待签）

- [ ] **M4.3** `pytest tests/ -v` 全绿

### M4 DA+SA Review #4

| 角色 | 检查项 | Pass |
|------|--------|------|
| DA | spec/runbook/ADR 一致 | ☐ |
| SA | 日常 `run` 命令面无 breaking change | ☐ |
| 共同 | Q1–Q6 全 PASS | ☐ |

---

## M5 — 实库 Pilot E1–E3

**前置：** spec-gate hotfix 已合入；`v_dq_spec_freshness` 视图已 `CREATE OR REPLACE`。

- [x] **M5.1** 备份 DB
- [x] **M5.2** `CALC_AUTO_SPEC_REFRESH=0`
- [x] **M5.3** Pilot 50 股：

```bash
python3 -m backend.cli calc --refresh-spec macd --date 20260616 \
  --ts-code $(head -50 codes.txt)  # 或用 --dry-run 先估
```

- [x] **M5.4** Oracle 脚本对比 B4 `trend`/`turning_point` + volume `trend`（stored vs recompute）
- [x] **M5.5** `ops spec-status` + `health_check` Section J
- [x] **M5.6** APPEND 同日复跑墙钟对比（E3）

### M5 Final DA+SA + User Sign-off

| ID | 项 | Pass |
|----|-----|------|
| E1 | 50 股 oracle 100% | ☑ |
| E2 | spec_stale=0（已刷指标） | ☑ |
| E3 | APPEND 不退化 | ☑ |
| — | 用户批准合 main | ☐ |

---

## ⑥ File Map

| 文件 | 职责 |
|------|------|
| `backend/etl/calc_compute_domain.py` | **新建** — `resolve_compute_indices` |
| `backend/etl/b4_macd.py` | fast path + expanding 保留 |
| `backend/etl/calc_macd.py` | `_compute_indicators` 透传 target_indices |
| `backend/etl/calc_batch_append.py` | `batch_full_macd/volume` 算域 |
| `backend/etl/ops_spec_status.py` | **新建** — spec-status |
| `backend/etl/calc_spec_refresh.py` | `--dry-run` |
| `backend/config.py` | `CALC_B4_WEEKLY_FAST` |
| `backend/cli.py` | ops spec-status, calc --dry-run |
| `tests/test_etl/test_calc_compute_domain.py` | **新建** |
| `tests/test_etl/test_b4_macd_weekly_append.py` | fast oracle |
| `tests/test_cli/test_ops_spec_status.py` | **新建** |
| `scripts/audit_macd_b4_oracle.py` | M5 E1 stored vs expanding |
| `scripts/health_check.py` | Section J spec_freshness @ export anchor |

**不改动：** DWD、export_wide layout、orchestrator CALCULATORS 列表、DWS 基表 DDL。

**M1 例外（SA T1 Pass，2026-06-17）：** 允许新增只读 DQ 视图 `v_dq_spec_freshness`（`backend/db/schema.py`），供 `ops spec-status` / `health_check` Section J；**非** DWS 语义或 `v_*_latest` 变更。

---

## ⑦ 预期收益（非阻塞签字，M5 实测为准）

| 场景 | 优化前 | 优化后（估） |
|------|--------|--------------|
| MACD 周线 batch FULL | ~68 min / 5293 股 | ~7–15 min |
| Volume 周线 batch FULL | 未单独测（O(n²) trend） | 显著下降 |
| 日常 APPEND 真新日 | M2d/M2c 已有 | **不退化**（E3） |
| Migration 运维 | 多命令 + env | `spec-status` + 单页 SOP |

---

## ⑧ Self-Review（plan 作者核对）

- [x] Spec 覆盖：三层域 → M0/M2；MACD P2 → M3；Volume → M2；Ops UX → M1
- [x] 无 TBD placeholder
- [x] DA/SA 门禁 + STOP 协议
- [x] 与 spec-gate hotfix PR 分离
- [x] engineering-protocol 决策树：窄窗 FULL only，无全库 rebuild

---

**Plan complete.** 实施时须 subagent-driven-development + **每 Milestone DA+SA 双签**；触发 STOP 条件时 **等待用户审核** 后再继续。
