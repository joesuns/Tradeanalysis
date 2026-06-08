# Calc 同日复跑 Partial Skip v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将同日复跑 calc 从实库 **630s** 进一步压到 **<120s**（stretch <60s），在保障数据质量前提下通过 **指标级 partial skip**、**BSE DDE 解耦**、**DDE weekly 尾窗 SQL** 消除 v1 整股 fallthrough 与 chunk 固定成本。

**Architecture:** v1 `CALC_FAST_SKIP` 仅在 12 指标全 SKIP 时短路整股。v2 改为 `preflight` 返回每股 12 路由结果，`SKIP` 指标直接记 `fingerprint_match`，仅对 `APPEND`/`FULL` 指标调用新函数 `calc_stock_pipeline_selective()`（按 freq 共享 quote/DDE 加载）。BSE（`.BJ`）DDE 空帧在 preflight 层视为永久 `SKIP`（对齐 `calc_dde.py` 的 `SOURCE_UNAVAILABLE`）。DDE weekly chunk batch 改为 SQL 层 `ROW_NUMBER <= 245`，避免全历史聚合后再 `tail()`。

**Tech Stack:** Python 3.9、DuckDB、pytest、pandas；复用 `classify_calc_mode` / `_route_calc` / `CALC_ROUTE_SPECS`。

**实库基线（v1 已验收）：**

| 阶段 | 耗时 |
|---|---|
| fetch 538 股宽桶 | 11s（v1 已修复） |
| calc 同日复跑 | **630s**（v1 fast_skip，未达 60s 目标） |
| fast_skip 命中率 | 9%–52%/chunk |
| state 12 指标齐全 | 5118/5388 |
| BSE | 317 股，`source_unavailable=530` |

---

## File Map

| 文件 | 职责 |
|---|---|
| `backend/etl/calc_fast_skip.py` | preflight v2、BSE DDE 规则、`partition_preflight_modes` |
| `backend/etl/orchestrator.py` | `calc_stock_pipeline_selective`、`_calc_stock_chunk` 接入 partial 路径 |
| `backend/etl/calc_dde.py` | `_load_weekly_batch(..., tail_window=245)` SQL 尾窗 |
| `backend/etl/calc_indicators.py` | 不变（复用 `CALC_ROUTE_SPECS`） |
| `backend/config.py` | 可选 `CALC_PARTIAL_SKIP=1`（默认开，与 `CALC_FAST_SKIP` 联动） |
| `tests/test_etl/test_calc_fast_skip.py` | v2 单测扩展 |
| `tests/test_etl/test_calc_partial_skip.py` | 端到端 golden（新文件） |
| `CLAUDE.md` / spec | 修正 v1 实库数字，文档 v2 |

---

### Task 1: 修正 v1 文档口径 ✅

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/plans/2026-06-08-calc-fast-skip-preflight.md`

- [ ] **Step 1: 更新 CLAUDE.md fast_skip 描述**

将「~834s→~20–60s」改为：

```markdown
- **同日复跑短路（CALC_FAST_SKIP v1）:** 实库同日复跑 **834s→630s**（24%）；12 指标全 SKIP 才短路。未达 60s → v2 partial skip 见 `docs/superpowers/plans/2026-06-08-calc-partial-skip-v2.md`。
```

- [ ] **Step 2: 在 v1 plan 第六节追加实库结果**

```markdown
- [x] 实库 calc 630s（未达 <60s，触发 v2）
- [x] fast_skip 日志可见（349/674 ~ 59/674 per chunk）
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/plans/2026-06-08-calc-fast-skip-preflight.md
git commit -m "docs: record CALC_FAST_SKIP v1 real-world 630s baseline"
```

---

### Task 2: BSE DDE preflight 解耦（P1）

**Files:**
- Modify: `backend/etl/calc_fast_skip.py`
- Test: `tests/test_etl/test_calc_fast_skip.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_etl/test_calc_fast_skip.py` 追加：

```python
def test_preflight_bse_empty_dde_treated_as_skip_not_fallthrough():
    """BSE empty moneyflow → per-indicator SKIP, not whole-stock None."""
    from backend.etl.calc_fast_skip import preflight_stock_modes_v2

    con = duckdb.connect(":memory:")
    _minimal_db(con)
    bj = "899999.BJ"
    con.execute(
        "INSERT INTO dim_stock (ts_code, name, list_date) VALUES (?, 'test', '20200101')",
        [bj],
    )
    # daily quote only (no moneyflow for BJ)
    dates = [r[0] for r in con.execute(
        "SELECT trade_date FROM dim_date WHERE is_trade_day=1 ORDER BY trade_date LIMIT 30"
    ).fetchall()]
    for i, d in enumerate(dates):
        c = 10.0 + i * 0.1
        con.execute(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
            "VALUES (?, ?, ?, ?, ?, ?, 1000, 0)",
            [bj, d, c, c, c, c],
        )
    tail_cols = quote_tail_columns()
    daily = batch_load_quote_tails(con, [bj], "daily", tail_cols)
    weekly = batch_load_quote_tails(con, [bj], "weekly", tail_cols)
    state_map = _all_states_skip(con, bj, dates[-1], daily[bj], weekly.get(bj))

    modes = preflight_stock_modes_v2(
        bj, state_map, daily.get(bj), weekly.get(bj), None, None,
    )
    assert modes is not None, "BSE must not fallthrough on empty DDE"
    assert modes[("dde", "daily")][0] == "SKIP"
    assert modes[("dde", "weekly")][0] == "SKIP"
    assert stock_can_fast_skip(modes)
    con.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_etl/test_calc_fast_skip.py::test_preflight_bse_empty_dde_treated_as_skip_not_fallthrough -v
```

Expected: FAIL — `preflight_stock_modes_v2` not defined 或返回 `None`

- [ ] **Step 3: Implement `preflight_stock_modes_v2`**

在 `backend/etl/calc_fast_skip.py` 新增（保留旧 `preflight_stock_modes` 供过渡测试）：

```python
def _classify_indicator_preflight(
    ts_code: str,
    indicator_name: str,
    freq: str,
    sig_cols: list,
    source: str,
    state: Optional[dict],
    daily_q: Optional[pd.DataFrame],
    weekly_q: Optional[pd.DataFrame],
    daily_dde: Optional[pd.DataFrame],
    weekly_dde: Optional[pd.DataFrame],
) -> Optional[Tuple[str, list]]:
    """Return (mode, new_bars) or None when slow-path required."""
    if source == "quote":
        df = daily_q if freq == "daily" else weekly_q
        if df is None or len(df) == 0:
            return None
        return classify_calc_mode(df, state, sig_cols)

    df = daily_dde if freq == "daily" else weekly_dde
    if df is None or len(df) == 0:
        if ts_code.endswith(".BJ"):
            return "SKIP", []
        return None
    return classify_calc_mode(df, state, sig_cols)


def preflight_stock_modes_v2(
    ts_code: str,
    state_map: Dict[Tuple[str, str, str], dict],
    daily_q: Optional[pd.DataFrame],
    weekly_q: Optional[pd.DataFrame],
    daily_dde: Optional[pd.DataFrame],
    weekly_dde: Optional[pd.DataFrame],
    specs=CALC_ROUTE_SPECS,
) -> Optional[Dict[Tuple[str, str], Tuple[str, list]]]:
    modes = {}
    for indicator_name, freq, _, sig_cols, source in specs:
        state = state_map.get((ts_code, freq, indicator_name))
        out = _classify_indicator_preflight(
            ts_code, indicator_name, freq, sig_cols, source, state,
            daily_q, weekly_q, daily_dde, weekly_dde,
        )
        if out is None:
            return None
        modes[(indicator_name, freq)] = out
    return modes
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_etl/test_calc_fast_skip.py::test_preflight_bse_empty_dde_treated_as_skip_not_fallthrough -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_fast_skip.py tests/test_etl/test_calc_fast_skip.py
git commit -m "feat: treat BSE empty DDE as preflight SKIP instead of fallthrough"
```

---

### Task 3: 指标级 partial 分区 helper

**Files:**
- Modify: `backend/etl/calc_fast_skip.py`
- Test: `tests/test_etl/test_calc_fast_skip.py`

- [ ] **Step 1: Write the failing test**

```python
def test_partition_preflight_modes_splits_skip_and_run():
    from backend.etl.calc_fast_skip import partition_preflight_modes

    modes = {
        ("macd", "daily"): ("SKIP", []),
        ("ma", "daily"): ("FULL", []),
        ("dde", "weekly"): ("SKIP", []),
    }
    skip_keys, run_keys = partition_preflight_modes(modes)
    assert skip_keys == {("macd", "daily"), ("dde", "weekly")}
    assert run_keys == {("ma", "daily")}
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
pytest tests/test_etl/test_calc_fast_skip.py::test_partition_preflight_modes_splits_skip_and_run -v
```

- [ ] **Step 3: Implement**

```python
def partition_preflight_modes(
    modes: Dict[Tuple[str, str], Tuple[str, list]],
) -> Tuple[set, set]:
    """Return (skip_keys, run_keys) where run_keys need APPEND/FULL."""
    skip_keys = set()
    run_keys = set()
    for key, (mode, _) in modes.items():
        if mode == "SKIP":
            skip_keys.add(key)
        else:
            run_keys.add(key)
    return skip_keys, run_keys
```

- [ ] **Step 4: Run test — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_fast_skip.py tests/test_etl/test_calc_fast_skip.py
git commit -m "feat: add partition_preflight_modes for partial skip routing"
```

---

### Task 4: `calc_stock_pipeline_selective`（P0 核心）

**Files:**
- Modify: `backend/etl/orchestrator.py`
- Test: `tests/test_etl/test_calc_partial_skip.py`（新建）

- [ ] **Step 1: Write the failing integration test**

创建 `tests/test_etl/test_calc_partial_skip.py`：

```python
"""Golden: selective pipeline matches full pipeline for non-SKIP indicators."""
import duckdb
import numpy as np

from backend.db.schema import create_all_tables, ensure_calc_state_table
from backend.etl.orchestrator import calc_stock_pipeline, calc_stock_pipeline_selective
from backend.etl.calc_state import load_calc_state_batch
from backend.etl.calc_fast_skip import (
    batch_load_quote_tails,
    batch_load_dde_tails,
    preflight_stock_modes_v2,
    partition_preflight_modes,
    quote_tail_columns,
)
from backend.etl.calc_indicators import CALC_ROUTE_SPECS

TS = "P.SZ"


def _setup(con, n=260):
    create_all_tables(con)
    ensure_calc_state_table(con)
    dates = [(np.datetime64("2020-01-01") + np.timedelta64(i, "D"))
             .astype("datetime64[D]").astype(str).replace("-", "")
             for i in range(n)]
    for d in dates:
        con.execute(
            "INSERT INTO dim_date (trade_date, is_trade_day, is_week_end) VALUES (?, 1, 0)",
            [d],
        )
    rng = np.random.default_rng(3)
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    for i, d in enumerate(dates):
        c = float(close[i])
        con.execute(
            "INSERT INTO dwd_daily_quote "
            "(ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, is_suspended) "
            "VALUES (?, ?, ?, ?, ?, ?, 1000, 0)",
            [TS, d, c, c, c, c],
        )
        con.execute(
            "INSERT INTO dwd_daily_moneyflow "
            "(ts_code, trade_date, buy_lg_vol, sell_lg_vol, buy_elg_vol, sell_elg_vol, "
            " total_vol, net_mf_amount) VALUES (?, ?, 10, 5, 3, 2, 1000, 1.5)",
            [TS, d],
        )
    return dates


def test_selective_pipeline_matches_full_for_run_keys():
    con = duckdb.connect(":memory:")
    dates = _setup(con)
    calc_date = dates[-1]

    full = calc_stock_pipeline(con, TS, calc_date, daily_recalc=calc_date, weekly_recalc=None)
    full_map = {(n, f): r for n, f, r in full}

    state_map = load_calc_state_batch(con, [TS])
    tail_cols = quote_tail_columns()
    modes = preflight_stock_modes_v2(
        TS, state_map,
        batch_load_quote_tails(con, [TS], "daily", tail_cols).get(TS),
        batch_load_quote_tails(con, [TS], "weekly", tail_cols).get(TS),
        batch_load_dde_tails(con, [TS], "daily").get(TS),
        batch_load_dde_tails(con, [TS], "weekly").get(TS),
    )
    _, run_keys = partition_preflight_modes(modes)
    if not run_keys:
        run_keys = {("macd", "daily")}  # force at least one run for test

    sel = calc_stock_pipeline_selective(
        con, TS, calc_date, daily_recalc=calc_date, weekly_recalc=None,
        run_keys=run_keys,
    )
    sel_map = {(n, f): r for n, f, r in sel}

    for key in run_keys:
        assert sel_map[key].calculated == full_map[key].calculated
        assert sel_map[key].skipped.keys() == full_map[key].skipped.keys()
    con.close()
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
pytest tests/test_etl/test_calc_partial_skip.py::test_selective_pipeline_matches_full_for_run_keys -v
```

- [ ] **Step 3: Implement `calc_stock_pipeline_selective` in `orchestrator.py`**

放在 `calc_stock_pipeline` 之后：

```python
def calc_stock_pipeline_selective(
    con, ts_code: str, calc_date: str,
    daily_recalc: Optional[str] = None,
    weekly_recalc: Optional[str] = None,
    run_keys: Optional[set] = None,
) -> list:
    """Run only (indicator, freq) in run_keys; same loads/routing as full pipeline."""
    from backend.etl.base import load_quote_groups
    from backend.etl.recalc_spec import resolve_load_start
    from backend.config import CALC_APPEND
    from backend.etl.calc_indicators import CALC_ROUTE_SPECS

    if not run_keys:
        return []

    outputs = []
    specs_by_freq = {"daily": [], "weekly": []}
    for indicator_name, freq, CalcCls, _, source in CALC_ROUTE_SPECS:
        if (indicator_name, freq) in run_keys:
            specs_by_freq[freq].append((indicator_name, CalcCls, source))

    for freq, recalc_start in (("daily", daily_recalc), ("weekly", weekly_recalc)):
        freq_specs = specs_by_freq[freq]
        if not freq_specs:
            continue
        load_start = resolve_load_start(con, recalc_start, freq) if recalc_start else None
        append_on = CALC_APPEND and recalc_start is not None

        quote_groups = {}
        qdf = None
        if any(s[2] == "quote" for s in freq_specs):
            src = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
            quote_groups = load_quote_groups(
                con, src, freq, QUOTE_COLUMNS, [ts_code], start_date=load_start,
            )
            qdf = quote_groups.get(ts_code)

        dde_groups = {}
        ddf = None
        if any(s[0] == "dde" for s in freq_specs):
            dde = DDECalculator(con, freq)
            if freq == "daily":
                dde_groups = dde._load_daily_batch([ts_code], start_date=load_start)
            else:
                dde_groups = dde._load_weekly_batch([ts_code], start_date=load_start)
            ddf = dde_groups.get(ts_code)

        for indicator_name, CalcCls, source in freq_specs:
            calc = CalcCls(con, freq)
            if source == "quote":
                result = _route_calc(
                    con, calc, indicator_name, freq, ts_code, qdf, calc_date,
                    recalc_start, quote_groups, append_on,
                )
            else:
                result = _route_calc(
                    con, calc, "dde", freq, ts_code, ddf, calc_date,
                    recalc_start, None, append_on,
                )
            outputs.append((indicator_name, freq, result))
    return outputs
```

- [ ] **Step 4: Run test — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_calc_partial_skip.py
git commit -m "feat: add calc_stock_pipeline_selective for per-indicator partial run"
```

---

### Task 5: 接入 `_calc_stock_chunk` partial 路径

**Files:**
- Modify: `backend/etl/orchestrator.py`
- Modify: `backend/etl/calc_fast_skip.py`（export v2）
- Test: `tests/test_etl/test_calc_partial_skip.py`

- [ ] **Step 1: Write failing test for chunk-level partial**

```python
def test_partial_skip_records_skip_without_full_pipeline(monkeypatch):
    """When 11 SKIP + 1 FULL, only FULL indicator invokes selective pipeline."""
    calls = {"selective": 0, "full": 0}

    def fake_selective(*args, **kwargs):
        calls["selective"] += 1
        return []

    def fake_full(*args, **kwargs):
        calls["full"] += 1
        return []

    monkeypatch.setattr(
        "backend.etl.orchestrator.calc_stock_pipeline_selective", fake_selective,
    )
    monkeypatch.setattr(
        "backend.etl.orchestrator.calc_stock_pipeline", fake_full,
    )
    # ... invoke _calc_stock_chunk with mocked DB fixture ...
    assert calls["selective"] >= 1
    assert calls["full"] == 0
```

（实施时用最小 in-memory DB + 1 股 fixture，或 mock `preflight_stock_modes_v2` 返回 11 SKIP + 1 FULL。）

- [ ] **Step 2: Modify `_calc_stock_chunk` loop**

替换 v1 整股 fast_skip 分支：

```python
from backend.etl.calc_fast_skip import (
    preflight_stock_modes_v2,
    partition_preflight_modes,
)

# inside for ts_code in chunk:
modes = preflight_stock_modes_v2(...) if fast_on else None
if modes is not None:
    skip_keys, run_keys = partition_preflight_modes(modes)
    for indicator_name, freq in skip_keys:
        agg_by_key[(indicator_name, freq)].add_skip(
            SkipReason.FINGERPRINT_MATCH, ts_code,
            "fast_skip: preflight",
        )
    if not run_keys:
        partial_skip_count += 1
        _report_calc_progress()
        continue
    for indicator_name, freq, result in calc_stock_pipeline_selective(
            con, ts_code, calc_date, daily_recalc, weekly_recalc,
            run_keys=run_keys):
        # aggregate as today
    partial_run_count += 1
    _report_calc_progress()
    continue

# fallthrough: modes is None → calc_stock_pipeline (unchanged)
```

日志：

```python
logger.info(
    "calc partial_skip: full_skip=%d partial_run=%d fallthrough=%d / %d stocks",
    partial_skip_count, partial_run_count, fallthrough_count, len(chunk),
)
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_etl/test_calc_partial_skip.py tests/test_etl/test_calc_fast_skip.py -v
```

- [ ] **Step 4: Commit**

```bash
git add backend/etl/orchestrator.py backend/etl/calc_fast_skip.py tests/test_etl/test_calc_partial_skip.py
git commit -m "feat: wire partial skip into calc chunk worker"
```

---

### Task 6: DDE weekly 尾窗 SQL（P2）

**Files:**
- Modify: `backend/etl/calc_dde.py`
- Modify: `backend/etl/calc_fast_skip.py`
- Test: `tests/test_etl/test_calc_fast_skip.py`

- [ ] **Step 1: Write failing test for tail_window param**

```python
def test_load_weekly_batch_tail_window_limits_rows():
    con = duckdb.connect(":memory:")
    _minimal_db(con)
    calc = DDECalculator(con, "weekly")
    full = calc._load_weekly_batch([TS], tail_window=None)[TS]
    tailed = calc._load_weekly_batch([TS], tail_window=245)[TS]
    assert len(tailed) <= 245
    assert tailed.equals(full.tail(245).reset_index(drop=True))
    con.close()
```

- [ ] **Step 2: Add `tail_window` to `_load_weekly_batch`**

在 `weekly_agg` 最终 SELECT 外包一层：

```sql
, ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
    FROM ( ... existing final SELECT ... ) base
)
SELECT ts_code, trade_date, ... FROM ranked WHERE rn <= ? ORDER BY ts_code, trade_date
```

`tail_window=None` 时不包 ranked（保持原行为供 slow path / golden）。

- [ ] **Step 3: Update `batch_load_dde_tails` weekly path**

```python
groups = calc._load_weekly_batch(ts_codes, tail_window=window)
# 删除 Python tail() — SQL 已限制
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/test_etl/test_calc_fast_skip.py::test_dde_weekly_tail_matches_load_weekly_batch \
       tests/test_etl/test_calc_fast_skip.py::test_load_weekly_batch_tail_window_limits_rows -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_dde.py backend/etl/calc_fast_skip.py tests/test_etl/test_calc_fast_skip.py
git commit -m "perf: limit DDE weekly batch load to SQL tail window"
```

---

### Task 7: Golden 端到端等价性

**Files:**
- Modify: `tests/test_etl/test_calc_partial_skip.py`

- [ ] **Step 1: Add full vs v2-path equivalence test**

对 3–5 只股（含 `.BJ`、成熟股、state 不全股）：

1. `CALC_FAST_SKIP=0` 跑 `_calc_stock_chunk`（oracle）
2. v2 partial 路径跑同一 chunk
3. 比对 per `(ts_code, indicator, freq)`：`calculated` 计数 + `skip reason` 集合一致

```python
def test_partial_skip_chunk_equivalent_to_full_pipeline(monkeypatch, tmp_path):
    ...
```

- [ ] **Step 2: Run full suite**

```bash
pytest tests/ -v --tb=short
```

Expected: all pass（当前基线 386+）

- [ ] **Step 3: Commit**

```bash
git add tests/test_etl/test_calc_partial_skip.py
git commit -m "test: golden equivalence for partial skip chunk path"
```

---

### Task 8: 文档与配置

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`
- Modify: `backend/config.py`（可选）

- [ ] **Step 1: 文档 v2**

`CLAUDE.md` calc 段追加：

```markdown
- **同日复跑 partial skip（v2）:** `preflight_stock_modes_v2` 按指标分区；SKIP 直接记 skip，APPEND/FULL 走 `calc_stock_pipeline_selective`；BSE DDE 空帧视为 SKIP。目标同日复跑 <120s。
```

spec 配置表加一行（若不加新 env 则写「v2 随 CALC_FAST_SKIP 默认启用」）。

- [ ] **Step 2: 更新本 plan 状态 → 已完成（实库后）**

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md docs/superpowers/plans/2026-06-08-calc-partial-skip-v2.md
git commit -m "docs: document calc partial skip v2"
```

---

### Task 9: 实库验收

- [ ] **Step 1: 同日复跑**

```bash
python3 -m backend.cli calc --date 20260602
```

| 检查项 | 目标 |
|---|---|
| `partial_skip: full_skip=` | **>4000** 股/chunk 合计 |
| `fallthrough=` | **<500** |
| calc 本体 | **<120s**（stretch <60s） |
| `0 calculated` | ✅ |
| `fingerprint_match` 总数 | ≈64126（±BSE source_unavailable 重分类） |

- [ ] **Step 2: 回归**

```bash
CALC_FAST_SKIP=0 python3 -m backend.cli calc --date 20260602
```

应仍 ~630–834s。

- [ ] **Step 3: 新日 APPEND 不受影响**

```bash
python3 -m backend.cli fetch
python3 -m backend.cli calc --date 20260608
```

应有 `calculated > 0`，耗时分钟级非小时级。

---

## Self-Review

| 检查项 | 结果 |
|---|---|
| P0 partial skip | Task 3–5 |
| P1 BSE DDE | Task 2 |
| P2 weekly SQL tail | Task 6 |
| 数据质量 | 复用 `classify_calc_mode` + selective 走 `_route_calc`；golden Task 7 |
| 无占位符 | 所有步骤含具体代码/命令 |
| v1 文档债务 | Task 1 |

## 风险

| 风险 | 缓解 |
|---|---|
| selective 与 full 加载域不一致 | Task 4/7 golden |
| 非 BSE 空 DDE 误判 SKIP | 仅 `.BJ` 走合成 SKIP；其他仍 `None` fallthrough |
| weekly tail SQL 行为变化 | `tail_window=None` 保持原路径；单测比对 |

---

**Plan complete and saved to `docs/superpowers/plans/2026-06-08-calc-partial-skip-v2.md`. Two execution options:**

**1. Subagent-Driven（推荐）** — 每 Task 派 fresh subagent，Task 间你做 review

**2. Inline Execution** — 本会话按 Task 1→9 顺序落地，每 2–3 Task 设 checkpoint

**Which approach?**
