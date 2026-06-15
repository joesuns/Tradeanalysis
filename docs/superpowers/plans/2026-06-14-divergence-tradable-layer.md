# MACD/DDE 可交易背离消费层（一存三用）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 **不修改 DWS L1 结构背离算法** 的前提下，新增 MACD/DDE × 日/周「可交易背离」消费层，修正 Excel/screening 误用；L1 改名为「结构背离」保留对标能力。

**Architecture:** 存储层 `dws_*.{divergence}` 保持 Level 2 原值；新建 `divergence_tradable.py` 在结构状态机 TG 点输出 metadata（path/tg_lag/zone），经三条硬门槛生成 `tradable` + `reject_reason`；export/combo_eval/screening 读消费层，不触发全市场 calc FULL。

**Tech Stack:** Python 3.9, NumPy, pandas, DuckDB, pytest, openpyxl

**硬编码契约（全项目统一，禁止 magic number 散落）：**

| 常量 | 值 | 含义 |
|------|-----|------|
| `TRADABLE_PATH` | `direct` only | 仅 T1/B1，拒绝 T2/B2 隔峰 |
| `TRADABLE_TG_LAG_MAX` | `1` | TG 距段内价极值 bar 数 ≤1 |
| `TRADABLE_TOP_ZONE` | MACD: `macd_bar>0`；DDE: `ddx>0` | 顶背标注日区域 |
| `TRADABLE_BOTTOM_ZONE` | MACD: `macd_bar<0`；DDE: `ddx<0` | 底背标注日区域 |
| L1 `dedup` | `10` | 继承，消费层不修改 |
| L1 lookback | `250` | 消费层重算尾窗宽度 |

**Calc 路径约束：** Phase A–B **不改** `divergence_structure.py` 写入语义、不 bump `spec_version`、不 `rebuild_all_dwd`、不 mass FULL。消费层对 export 日有 L1 事件的 ts_code 子集做 tail 重算（≤250 bar）。

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `backend/etl/divergence_tradable.py` | **新建** 常量、`StructureEvent`、`classify_tradable()`、MACD/DDE 四通道封装 |
| `backend/etl/divergence_structure.py` | **Modify** 新增 `trace_*_structure_events()` 返回 TG 元数据（L1 标签不变） |
| `backend/export_wide.py` | 列重命名、可交易列 enrich、signal sheet 默认展示可交易列 |
| `backend/backtest/combo_eval.py` | 策略默认改读 `*_tradable`；保留 L1 参数 opt-in |
| `scripts/screen_divergence_tradable.py` | **新建** CLI：按日筛可交易背离 + reject_reason |
| `tests/test_etl/test_divergence_tradable.py` | **新建** TDD 主测试（含 601518/300930 回归） |
| `tests/test_etl/test_divergence_structure.py` | **Modify** 确保 trace 不破坏现有 golden |
| `tests/test_export/test_export_wide_tradable.py` | **新建** export 列名与 enrich 集成 |
| `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` | §6.2 增「三用」与可交易门槛 |
| `CLAUDE.md` | 背离架构 + screening 命令 |

**Phase A 不改：** DDL、`dws_*` 列、B4 gate、API router（Phase C 可选）。

---

## 阶段划分与验收门禁

```
M1 核心模块 + TDD ──► Gate-A（数据架构师）
M2 Export 集成      ──► Gate-B（交易专家）
M3 combo + screening ──► Gate-C（双签）
M4 文档             ──► Gate-D（收尾核对）
```

**修复循环（任意 Gate 失败）：** 按 superpowers:test-driven-development — RED（失败验收用例）→ GREEN → REFACTOR；**禁止**未写失败测试直接改生产代码。

---

## Gate-A：数据架构师验收清单

| # | 检查项 | 通过标准 |
|---|--------|----------|
| A1 | L1 不变性 | `compute_macd/dde_structure_divergence` 输出与改前 bit-equal（现有 pytest 全绿） |
| A2 | 四通道覆盖 | macd_daily/weekly、dde_daily/weekly 均有 `trace_*` + `evaluate_tradable_*` |
| A3 | 601518/300930 回归 | 指定日期 L1 保留；tradable 按表剔除；reject_reason 正确 |
| A4 | 计算范围 | enrich 仅 L1≠NULL 子集 + tail 250；无全市场结构重算入口 |
| A5 | 性能 | export enrich 全市场 5000 股 <30s 增量（同日复跑可缓存，可选） |
| A6 | pytest | `pytest tests/test_etl/test_divergence_tradable.py tests/test_etl/test_divergence_structure.py -v` 全绿 |

## Gate-B：二级市场交易专家验收清单

| # | 检查项 | 通过标准 |
|---|--------|----------|
| B1 | Excel 语义 | 原「MACD背离」→「MACD结构背离」；新增「MACD可交易背离」在综合分析 sheet **位于结构列右侧** |
| B2 | 反直觉样例 | 601518/300930 周线 20260612：**结构=顶背离，可交易=-** |
| B3 | 日线反例 | 601518 20240927（DIF<0 顶背）：可交易=-，reason=zone_mismatch |
| B4 | 共振策略 | combo 默认模板使用可交易列；文档写明「单信号不下单」 |
| B5 | 日/周分工 | 文档说明：周=滤镜，日=执行（非代码强制） |
| B6 | DDE 定位 | DDE 可交易列为辅；`.BJ` 为 N/A |

## Gate-C：双签（M3 完成后）

- 数据架构师确认 A1–A6 仍成立
- 交易专家确认 B1–B6 + 手动 spot-check 10 股 Excel

---

### Task 0: 冻结验收样例（Golden Tradable）

**Files:**
- Create: `tests/fixtures/tradable_divergence_cases.csv`

- [ ] **Step 1: 创建 fixture CSV**

```csv
ts_code,trade_date,freq,indicator,l1_expected,tradable_expected,reject_reason
601518.SH,20260612,weekly,macd,top_divergence,,skip_peak
601518.SH,20260612,weekly,macd,top_divergence,,tg_lag
300930.SZ,20260612,weekly,macd,top_divergence,,skip_peak
300930.SZ,20230707,weekly,macd,bottom_divergence,,zone_mismatch
601518.SH,20240927,daily,macd,top_divergence,,zone_mismatch
601518.SH,20241206,daily,dde,top_divergence,,zone_mismatch
```

说明：`tradable_expected` 空 = 不可交易（`-`）；多条 reject 取**优先级**：`skip_peak` > `tg_lag` > `zone_mismatch`。

- [ ] **Step 2: 写 loader helper（测试内）**

```python
# tests/test_etl/test_divergence_tradable.py 顶部
import csv
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tradable_divergence_cases.csv"

def load_tradable_cases():
    with open(FIXTURE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
```

---

### Task 1: 结构事件 trace（metadata，不改 L1 标签）

**Files:**
- Modify: `backend/etl/divergence_structure.py`
- Test: `tests/test_etl/test_divergence_tradable.py`

- [ ] **Step 1: Write failing test — trace returns TG metadata**

```python
def test_macd_trace_events_marks_skip_peak_on_601518_weekly_20260612():
    from backend.etl.divergence_tradable import load_series_and_trace_macd
    events = load_series_and_trace_macd("601518.SH", "weekly")
    hit = [e for e in events if e.trade_date == "20260612" and e.l1_label == "top_divergence"]
    assert len(hit) == 1
    ev = hit[0]
    assert ev.path == "skip_peak"
    assert ev.tg_lag_bars >= 1
```

- [ ] **Step 2: Run test — verify FAIL**

Run: `pytest tests/test_etl/test_divergence_tradable.py::test_macd_trace_events_marks_skip_peak_on_601518_weekly_20260612 -v`  
Expected: FAIL — `load_series_and_trace_macd` / `trace_macd_structure_events` not defined

- [ ] **Step 3: Implement `trace_macd_structure_events` in divergence_structure.py**

在现有三轮循环（CH/CL → REF → T/B + TG write）基础上，TG 写入 `result[i]` 的同一分支记录：

```python
# backend/etl/divergence_structure.py 新增 dataclass + 函数签名
from dataclasses import dataclass
from typing import Literal, Optional, List

@dataclass
class StructureEvent:
    index: int
    l1_label: str  # top_divergence | bottom_divergence
    path: Literal["direct", "skip_peak"]
    tg_lag_bars: int  # i - argmax/argmin close in segment
    zone_ok: bool     # 顶: bar>0; 底: bar<0 at TG bar

def trace_macd_structure_events(close, dif, dea, macd_bar) -> List[StructureEvent]:
    """Same state machine as compute_macd_structure_divergence; emit StructureEvent on TG only."""
    ...
```

**实现要点：**
- 复用现有 `T1/T2/B1/B2` 数组；TG 时 `path = "direct" if T1[i-1] else "skip_peak"`
- `tg_lag_bars`：顶背 `i - argmax(close[gc:i+1])`；底背 `i - argmin(...)`
- `zone_ok`：顶 `macd_bar[i]>0`；底 `macd_bar[i]<0`
- `compute_macd_structure_divergence` **内部调用同一 helper**，保证 L1 bit-equal

- [ ] **Step 4: 同理 `trace_dde_structure_events(close, ddx, ddx2)`**

- [ ] **Step 5: Run tests — verify existing structure tests still PASS**

Run: `pytest tests/test_etl/test_divergence_structure.py tests/test_etl/test_divergence_tradable.py -v`  
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/etl/divergence_structure.py tests/test_etl/test_divergence_tradable.py tests/fixtures/tradable_divergence_cases.csv
git commit -m "feat: trace structure divergence TG metadata without changing L1 labels"
```

---

### Task 2: 可交易分类器（三条硬门槛）

**Files:**
- Create: `backend/etl/divergence_tradable.py`
- Test: `tests/test_etl/test_divergence_tradable.py`

- [ ] **Step 1: Write failing test — classify_tradable**

```python
from backend.etl.divergence_tradable import (
    TRADABLE_TG_LAG_MAX,
    classify_tradable,
    StructureEvent,
)

def test_classify_rejects_skip_peak():
    ev = StructureEvent(0, "top_divergence", "skip_peak", tg_lag_bars=0, zone_ok=True)
    out = classify_tradable(ev)
    assert out.tradable_label is None
    assert out.reject_reason == "skip_peak"

def test_classify_rejects_tg_lag():
    ev = StructureEvent(0, "top_divergence", "direct", tg_lag_bars=2, zone_ok=True)
    out = classify_tradable(ev)
    assert out.tradable_label is None
    assert out.reject_reason == "tg_lag"

def test_classify_rejects_zone_mismatch():
    ev = StructureEvent(0, "bottom_divergence", "direct", tg_lag_bars=0, zone_ok=False)
    out = classify_tradable(ev)
    assert out.tradable_label is None
    assert out.reject_reason == "zone_mismatch"

def test_classify_accepts_direct_fresh_zone_ok():
    ev = StructureEvent(0, "top_divergence", "direct", tg_lag_bars=1, zone_ok=True)
    out = classify_tradable(ev)
    assert out.tradable_label == "top_divergence"
    assert out.reject_reason is None
```

- [ ] **Step 2: Run — verify FAIL**

Run: `pytest tests/test_etl/test_divergence_tradable.py -k classify -v`  
Expected: FAIL — module not found

- [ ] **Step 3: Implement minimal `divergence_tradable.py`**

```python
# backend/etl/divergence_tradable.py
from dataclasses import dataclass
from typing import Optional, Literal

TRADABLE_TG_LAG_MAX = 1

RejectReason = Literal["skip_peak", "tg_lag", "zone_mismatch"]

@dataclass
class TradableVerdict:
    l1_label: str
    tradable_label: Optional[str]
    reject_reason: Optional[RejectReason]
    path: str
    tg_lag_bars: int

def classify_tradable(event) -> TradableVerdict:
    label = event.l1_label
    if event.path == "skip_peak":
        return TradableVerdict(label, None, "skip_peak", event.path, event.tg_lag_bars)
    if event.tg_lag_bars > TRADABLE_TG_LAG_MAX:
        return TradableVerdict(label, None, "tg_lag", event.path, event.tg_lag_bars)
    if not event.zone_ok:
        return TradableVerdict(label, None, "zone_mismatch", event.path, event.tg_lag_bars)
    return TradableVerdict(label, label, None, event.path, event.tg_lag_bars)
```

- [ ] **Step 4: Run — verify PASS**

Run: `pytest tests/test_etl/test_divergence_tradable.py -k classify -v`  
Expected: PASS

- [ ] **Step 5: Write failing integration test — fixture CSV 全量**

```python
@pytest.mark.parametrize("row", load_tradable_cases(), ids=lambda r: f"{r['ts_code']}_{r['trade_date']}_{r['indicator']}")
def test_tradable_cases_match_fixture(row):
    from backend.etl.divergence_tradable import evaluate_tradable_for_case
    verdict = evaluate_tradable_for_case(
        row["ts_code"], row["trade_date"], row["freq"], row["indicator"],
    )
    assert verdict.l1_label == row["l1_expected"]
    if row["tradable_expected"]:
        assert verdict.tradable_label == row["tradable_expected"]
    else:
        assert verdict.tradable_label is None
        assert verdict.reject_reason == row["reject_reason"]
```

- [ ] **Step 6: Implement `evaluate_tradable_for_case` + DuckDB tail loader**

```python
def evaluate_tradable_for_case(ts_code, trade_date, freq, indicator):
    events = load_and_trace(ts_code, freq, indicator)  # 250 tail from DWD + calc series
    hit = [e for e in events if e.trade_date == trade_date]
    if not hit:
        return TradableVerdict(None, None, None, "", 0)
    return classify_tradable(hit[0])
```

- [ ] **Step 7: Run fixture tests — verify PASS**

Run: `pytest tests/test_etl/test_divergence_tradable.py -v`  
Expected: PASS（需实库 `data/tradeanalysis.duckdb`；CI 用 pytest marker `@pytest.mark.integration`）

- [ ] **Step 8: Commit**

```bash
git add backend/etl/divergence_tradable.py tests/test_etl/test_divergence_tradable.py
git commit -m "feat: tradable divergence classifier with three hard gates"
```

---

### Task 3: Export 集成（列重命名 + enrich）

**Files:**
- Modify: `backend/export_wide.py`
- Create: `tests/test_export/test_export_wide_tradable.py`

- [ ] **Step 1: Write failing test — column names**

```python
def test_col_names_include_structure_and_tradable():
    from backend.export_wide import _COL_NAMES
    assert _COL_NAMES["macd_divergence"] == "MACD结构背离"
    assert _COL_NAMES["macd_divergence_tradable"] == "MACD可交易背离"
    assert _COL_NAMES["dde_divergence"] == "DDE结构背离"
    assert _COL_NAMES["dde_divergence_tradable"] == "DDE可交易背离"
```

- [ ] **Step 2: Run — verify FAIL**

Run: `pytest tests/test_export/test_export_wide_tradable.py::test_col_names_include_structure_and_tradable -v`

- [ ] **Step 3: Implement column map + `_enrich_tradable_divergence(daily_df, con)`**

```python
# export_wide.py — daily query 之后、merge 之前
from backend.etl.divergence_tradable import enrich_dataframe_tradable_columns

def export_wide_to_excel(...):
    ...
    daily = enrich_dataframe_tradable_columns(daily, con, freq="daily")
    ...
    weekly = enrich_dataframe_tradable_columns(weekly, con, freq="weekly")
```

`enrich_dataframe_tradable_columns` 逻辑：
- 输入列：`macd_divergence`, `dde_divergence`（L1）
- 输出列：`macd_divergence_tradable`, `macd_divergence_reject`, `dde_divergence_tradable`, `dde_divergence_reject`
- 仅对 `macd_divergence.notna() | dde_divergence.notna()` 的 ts_code 调 tail trace
- reject 列：枚举中文 `隔峰` / `滞后` / `区域` / `-`

- [ ] **Step 4: 更新 `_SIGNAL_ONLY` 与 `_EVENT_SIGNAL_COLS`**

```python
_EVENT_SIGNAL_COLS = {
    ...
    "macd_divergence_tradable", "dde_divergence_tradable",
}
# 综合分析 sheet：tradable 列优先；结构列保留但靠后
_SIGNAL_ONLY = {
    ...
    "macd_divergence_tradable", "macd_divergence",  # tradable 在前
    "dde_divergence_tradable", "dde_divergence",
}
```

- [ ] **Step 5: Write failing test — enrich 601518 on 20260612**

```python
@pytest.mark.integration
def test_enrich_601518_weekly_tradable_empty_on_skip_peak():
    ...
    assert row["macd_divergence"] == "top_divergence"
    assert row["macd_divergence_tradable"] is None  # 导出显示 "-"
```

- [ ] **Step 6: Run export tests + full pytest**

Run: `pytest tests/test_export/test_export_wide_tradable.py -v`  
Run: `pytest tests/ -v --ignore=tests/test_export/test_export_wide_tradable.py -q`（或全量）

- [ ] **Step 7: Commit**

```bash
git add backend/export_wide.py tests/test_export/test_export_wide_tradable.py
git commit -m "feat: export structure rename + tradable divergence columns"
```

**→ 执行 Gate-A + Gate-B。失败则 TDD 修复循环，不得进入 Task 4。**

---

### Task 4: combo_eval + screening CLI

**Files:**
- Modify: `backend/backtest/combo_eval.py`
- Create: `scripts/screen_divergence_tradable.py`
- Test: `tests/test_backtest/test_combo_eval_tradable.py`

- [ ] **Step 1: Write failing test — combo uses tradable by default**

```python
def test_find_combo_signals_filters_macd_tradable_not_structure_only():
    from backend.backtest.combo_eval import find_combo_signals
    # mock 或 integration：传 macd_divergence=bottom 应走 tradable 列
    ...
```

- [ ] **Step 2: Modify combo_eval**

- 新参数 `macd_divergence_tradable` / `use_tradable=True`（默认 True）
- `use_tradable=True` 时 JOIN 条件改为读 enrich 逻辑或 inline SQL 子查询；**M3 简化方案**：Python 侧先 `enrich` 再 filter

- [ ] **Step 3: Create `scripts/screen_divergence_tradable.py`**

```bash
python scripts/screen_divergence_tradable.py --date 20260612 --freq weekly --indicator macd
# 输出：ts_code, l1, tradable, reject_reason
```

- [ ] **Step 4: Run tests — PASS**

- [ ] **Step 5: Commit**

**→ 执行 Gate-C。**

---

### Task 5: 文档更新

**Files:**
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` §6.2
- Modify: `CLAUDE.md`

- [ ] **Step 1: spec 增节「6.2.1 结构背离三用与可交易门槛」**

- L1/L2 定义、三条硬门槛、reject 优先级、日/周/DDE 定位

- [ ] **Step 2: CLAUDE.md 更新**

- 背离段落：结构 vs 可交易
- 新命令：`scripts/screen_divergence_tradable.py`

- [ ] **Step 3: Commit**

**→ Gate-D 收尾核对（engineering-protocol §5）。**

---

## TDD 修复循环（验收失败时）

任意 Gate 失败，执行：

1. **RED**：将失败验收项转为 **failing pytest**（或扩展 `tradable_divergence_cases.csv`）
2. **Verify RED**：肉眼确认失败原因 = 业务预期（非 typo）
3. **GREEN**：最小改动 `divergence_tradable.py` / `trace_*` / `export_wide.py`
4. **Verify GREEN**：`pytest` 相关目录 + 原 structure 测试不回退
5. **Re-run Gate**：由对应角色 re-check
6. **Commit**：一修复一 commit，message 含 `fix(tradable): <gate-id>`

**禁止：** 为通过 Gate 放宽三条硬门槛（需交易专家书面确认 + 更新 fixture）。

---

## Phase B（可选，本 plan 外独立立项）

- 回测脚本：`scripts/backtest_tradable_divergence.py` — L1 vs tradable vs 共振 hold 1/3/5/10/20
- 仅当回测完成后再评估：是否持久化 tradable 列到 ADS 视图 / bump spec_version

---

## Self-Review（plan 完成时）

| Spec 要求 | Task |
|-----------|------|
| L1 不改 | Task 1 bit-equal 测试 |
| 四通道日/周 MACD+DDE | Task 1–2 |
| 三条硬门槛 | Task 2 |
| Excel 语义 | Task 3 |
| 601518/300930 回归 | Task 0 fixture |
| 不全库 rebuild | Architecture 约束 |
| TDD | 每 Task RED-GREEN |
| 双角色验收 | Gate-A/B/C |

---

**Plan complete and saved to `docs/superpowers/plans/2026-06-14-divergence-tradable-layer.md`. Two execution options:**

---

## 完成记录（2026-06-14）

- **分支：** `feat/divergence-tradable`（基于 main @ PR #5 merge `305cf00`）
- **Gate-A：** 35 passed / 3 skipped（`test_divergence_tradable*` + `test_divergence_structure` + export/combo/screening）
- **Gate-B：** Excel 列「结构背离」+「可交易背离」；601518/300930 fixture 回归 PASS
- **Gate-C：** combo `use_tradable=True` 默认；`screen_divergence_tradable.py` CLI
- **Gate-D：** spec §6.2.1 + CLAUDE.md（PR #5 已预写 screening 命令）
- **Task 0–5：** 全部落地；pytest 35 passed

**Two execution options:**

**1. Subagent-Driven (recommended)** — 每 Task 派生子 agent，Task 3 后停 Gate-A/B 人工验收

**2. Inline Execution** — 本会话按 Task 0→5 连续执行，Gate 点暂停等你确认

**Which approach?**
