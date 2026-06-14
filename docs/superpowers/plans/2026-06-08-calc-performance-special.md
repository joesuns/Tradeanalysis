# Calc 性能专项（Batch Write + Seed 批化 + State 降噪）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将新交易日全市场 calc 从实库 ~30–50 min（~1.6 stk/s）压到 **5–10 min**，同日复跑从 **630s** 压到 **<120s**——在零指标语义变更前提下，消灭 batch APPEND 的逐股 INSERT/seed SQL 与 SKIP 路径冗余 state 写。

**Architecture:** 当前 `CALC_BATCH_APPEND` 已实现「跨股批读 + 逐股算 + 逐股 INSERT」（`calc_batch_append.py` 中 `for ts_code` 循环）。本专项不改计算器公式，只做三件事：(1) `insert_dws_batch_multi` 按 `(indicator,freq)` 合并窄写为 **1 次 DuckDB INSERT**；(2) MACD/DDE/Volume 种子查询从「每股 1 SQL」改为「每批 1 SQL」；(3) SKIP 路径 `write_calc_state_from_df` 在 `history_fp` 未变时跳过，chunk worker 在 `batch_ctx` 存在时禁止重复 tail SQL。等价性：扩展现有 `tests/test_etl/test_batch_append_calc.py` golden（`atol=1e-9`）+ 新日/同日复跑墙钟验收。

**Tech Stack:** Python 3.9、DuckDB、pandas、numpy、pytest；复用 `insert_dws_batch`、`load_ema_seeds_batch`、`run_batch_append_phase`、`preflight_stock_modes_v2`。

**实库基线（引用既有 plan）：**

| 场景 | 当前 | 目标 |
|------|------|------|
| 新日 calc 本体 | ~49 min @ ~1.6 stk/s | **5–10 min** @ >10 stk/s |
| 同日复跑 calc 本体 | 630s（v1+v2 partial skip） | **<120s** |
| batch_append 阶段 | 批读已实现，逐股写 | 12 指标×2 频 ≈ **24 次 INSERT**（非 5388×12） |

**前置依赖：** `CALC_APPEND=1`、`CALC_BATCH_APPEND=1`、`CALC_FAST_SKIP=1` 已上线；`tests/test_etl/test_batch_append_calc.py` golden 已通过。

**关联文档：**
- `docs/superpowers/plans/2026-06-08-cross-stock-batch-append.md`（Phase 0–1 已完成）
- `docs/superpowers/plans/2026-06-08-calc-partial-skip-v2.md`（partial skip 已接入 orchestrator）
- `docs/superpowers/plans/2026-06-08-calc-fast-skip-preflight.md`（chunk preflight 基线）

**范围外（后续专项）：** 真·跨股 numpy 向量化（多股合并矩阵一次算）、物化尾窗表 `dwd_quote_tail_245`、DuckDB 索引治理、freshness-fetch 320s 解耦。

---

## File Map

| 文件 | 职责 |
|------|------|
| `backend/etl/base.py` | 新增 `insert_dws_batch_multi` |
| `backend/etl/calc_batch_seeds.py` | 新增 `load_zone_seeds_batch` |
| `backend/etl/calc_batch_append.py` | 批写/批种/批 state；重构 `_batch_append_loop` |
| `backend/etl/calc_state.py` | `should_refresh_calc_state` + `upsert_calc_state_batch` |
| `backend/etl/orchestrator.py` | SKIP 路径调用 state 降噪 helper |
| `backend/config.py` | 可选 `CALC_SKIP_STATE_REFRESH=1`（默认同日复跑跳过冗余 state 写） |
| `tests/test_etl/test_batch_append_calc.py` | 批写 golden + 批种单测 |
| `tests/test_etl/test_calc_state_batch.py` | **新建** state batch upsert 单测 |
| `CLAUDE.md` / spec §12.7 | 文档更新 |

---

## Phase 1 — 批量 DWS 窄写（最高 ROI）

### Task 1: `insert_dws_batch_multi`

**Files:**
- Modify: `backend/etl/base.py`（在 `insert_dws_batch` 之后追加）
- Test: `tests/test_etl/test_batch_append_calc.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_etl/test_batch_append_calc.py` 末尾追加：

```python
def test_insert_dws_batch_multi_writes_all_stocks_one_insert():
    """Multi-stock narrow write == sum of per-stock insert_dws_batch row counts."""
    import duckdb
    import pandas as pd

    from backend.db.schema import create_all_tables
    from backend.etl.base import insert_dws_batch, insert_dws_batch_multi

    con = duckdb.connect(":memory:")
    create_all_tables(con)
    calc_date = "20260608"
    dws_cols = [
        "ts_code", "trade_date", "ema_12", "ema_26", "dif", "dea", "macd_bar",
        "divergence", "zone", "turning_point", "alert", "trend", "trend_strength",
        "calc_date", "input_fingerprint", "spec_version",
    ]
    float_cols = ["ema_12", "ema_26", "dif", "dea", "macd_bar", "trend_strength"]
    rows = []
    for code in ("A.SZ", "B.SZ"):
        df = pd.DataFrame({
            "trade_date": ["20260608"],
            "ema_12": [1.0], "ema_26": [2.0], "dif": [0.1], "dea": [0.05],
            "macd_bar": [0.05], "divergence": [None], "zone": [None],
            "turning_point": [None], "alert": [None], "trend": ["flat"],
            "trend_strength": [0.01],
        })
        n = insert_dws_batch(
            con, "dws_macd_daily", df, code, calc_date, dws_cols, float_cols,
            input_fingerprint="fp1", write_start="20260608", write_end="20260608",
        )
        assert n == 1
        rows.append((code, df, "fp1", "20260608", "20260608"))

    con.execute("DELETE FROM dws_macd_daily")
    total = insert_dws_batch_multi(
        con, "dws_macd_daily", rows, calc_date, dws_cols, float_cols,
    )
    assert total == 2
    n_db = con.execute(
        "SELECT COUNT(*) FROM dws_macd_daily WHERE calc_date = ?", [calc_date],
    ).fetchone()[0]
    assert n_db == 2
    con.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py::test_insert_dws_batch_multi_writes_all_stocks_one_insert -v`

Expected: FAIL with `ImportError` or `AttributeError: insert_dws_batch_multi`

- [ ] **Step 3: Implement `insert_dws_batch_multi`**

在 `backend/etl/base.py` 的 `insert_dws_batch` 函数之后追加：

```python
def insert_dws_batch_multi(
    con,
    table: str,
    stock_rows: list,
    calc_date: str,
    dws_cols: list,
    float_cols: list,
    spec_version: str = "v1",
) -> int:
    """Batch narrow-write multiple stocks in one DuckDB INSERT.

    ``stock_rows`` is a list of tuples:
        (ts_code, df, input_fingerprint, write_start, write_end)

    Returns total rows inserted. Empty after range filter → 0.
    """
    import pandas as pd

    if not stock_rows:
        return 0

    data_cols = [c for c in dws_cols if c != "ts_code"]
    parts = []
    for ts_code, df, input_fingerprint, write_start, write_end in stock_rows:
        if df is None or df.empty:
            continue
        batch = df[data_cols].copy()
        for c in data_cols:
            if c not in batch.columns:
                batch[c] = None
        if write_start is not None:
            batch = batch[batch["trade_date"] >= write_start]
        if write_end is not None:
            batch = batch[batch["trade_date"] <= write_end]
        if batch.empty:
            continue
        batch["ts_code"] = ts_code
        for c in float_cols:
            if c in batch.columns:
                batch[c] = batch[c].apply(to_float_safe)
        batch["calc_date"] = calc_date
        batch["spec_version"] = spec_version
        batch["input_fingerprint"] = input_fingerprint or compute_fingerprint(df, float_cols)
        parts.append(batch)

    if not parts:
        return 0

    big = pd.concat(parts, ignore_index=True)
    con.register("_batch_multi", big)
    cols_sql = ", ".join(dws_cols)
    con.execute(
        f"INSERT OR REPLACE INTO {table} ({cols_sql}) "
        f"SELECT {cols_sql} FROM _batch_multi"
    )
    con.unregister("_batch_multi")
    return len(big)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py::test_insert_dws_batch_multi_writes_all_stocks_one_insert -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/base.py tests/test_etl/test_batch_append_calc.py
git commit -m "feat: add insert_dws_batch_multi for cross-stock narrow writes"
```

---

### Task 2: 重构 `_batch_append_loop` 使用批写

**Files:**
- Modify: `backend/etl/calc_batch_append.py`
- Test: `tests/test_etl/test_batch_append_calc.py`（既有 golden 应仍 PASS）

- [ ] **Step 1: Write regression guard test**

在 `tests/test_etl/test_batch_append_calc.py` 追加（确保批写不改语义）：

```python
def test_batch_append_loop_uses_single_insert(monkeypatch):
    """_batch_append_loop collects rows then calls insert_dws_batch_multi once."""
    import pandas as pd

    from backend.etl.base import CalcResult
    from backend.etl import calc_batch_append as mod

    calls = {"multi": 0, "single": 0}

    def fake_multi(*args, **kwargs):
        calls["multi"] += 1
        return 2

    def fake_single(*args, **kwargs):
        calls["single"] += 1
        return 1

    monkeypatch.setattr(mod, "insert_dws_batch_multi", fake_multi)
    monkeypatch.setattr(mod, "insert_dws_batch", fake_single)

    class FakeCalc:
        dws_table = "dws_ma_daily"
        SIGNATURE_COLS = ["close_qfq"]

        def _insert(self, *args, **kwargs):
            raise AssertionError("per-stock _insert must not be called")

    df = pd.DataFrame({"trade_date": ["20260608"], "close_qfq": [10.0]})
    data_groups = {"A.SZ": df, "B.SZ": df}
    new_bars_map = {"A.SZ": ["20260608"], "B.SZ": ["20260608"]}

    mod._batch_append_loop(
        FakeCalc(), ["A.SZ", "B.SZ"], "20260608", data_groups, new_bars_map,
        lambda c, code, frame, bars: frame,
    )
    assert calls["multi"] == 1
    assert calls["single"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py::test_batch_append_loop_uses_single_insert -v`

Expected: FAIL（当前仍调 `calc._insert`）

- [ ] **Step 3: Rewrite `_batch_append_loop`**

替换 `backend/etl/calc_batch_append.py` 中 `_batch_append_loop` 全文：

```python
def _batch_append_loop(
    calc,
    ts_codes: List[str],
    calc_date: str,
    data_groups: dict,
    new_bars_map: dict,
    compute_fn,
    dws_cols: Optional[List[str]] = None,
    float_cols: Optional[List[str]] = None,
):
    """Shared batch APPEND: compute per stock, INSERT once via insert_dws_batch_multi."""
    from backend.etl.base import compute_history_signature, insert_dws_batch_multi

    if dws_cols is None or float_cols is None:
        # Delegate metadata to calculator _insert helpers via a probe call pattern:
        # calculators expose DWS_COLS / FLOAT_COLS class attrs (add in Task 2b if missing).
        raise ValueError("dws_cols and float_cols required for batch append loop")

    stock_rows = []
    for ts_code in ts_codes:
        df = data_groups.get(ts_code)
        new_bars = new_bars_map.get(ts_code, [])
        if df is None or len(df) == 0 or not new_bars:
            continue
        out = compute_fn(calc, ts_code, df, new_bars)
        fp = compute_history_signature(out, calc.SIGNATURE_COLS)
        stock_rows.append((ts_code, out, fp, new_bars[0], new_bars[-1]))

    agg = CalcResult()
    if not stock_rows:
        return agg

    n = insert_dws_batch_multi(
        calc.con, calc.dws_table, stock_rows, calc_date, dws_cols, float_cols,
    )
    agg.calculated = n
    return agg
```

**Task 2b（同 Task 2 内）:** 为 6 个 Calculator 增加类常量（最小 diff，避免反射 `_insert`）：

在 `calc_macd.py` / `calc_ma.py` / `calc_kpattern.py` / `calc_volume.py` / `calc_price_position.py` / `calc_dde.py` 各增：

```python
DWS_COLS = [...]  # 与 _insert 内 dws_cols 相同
FLOAT_COLS = [...]  # 与 _insert 内 float_cols 相同
```

并更新各 `batch_append_*` 调用 `_batch_append_loop` 时传入 `CalcCls.DWS_COLS, CalcCls.FLOAT_COLS`。

`batch_append_macd` / `batch_append_dde` 在 for 循环结束后同样改为收集 `stock_rows` + 一次 `insert_dws_batch_multi`（不再调用 `calc._insert`）。

- [ ] **Step 4: Run full batch append golden suite**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py -v`

Expected: ALL PASS（含 `test_batch_append_macd_matches_per_stock_append` 等）

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_append.py backend/etl/calc_*.py tests/test_etl/test_batch_append_calc.py
git commit -m "perf: batch append uses insert_dws_batch_multi (one INSERT per indicator×freq)"
```

---

## Phase 2 — 种子查询批化（消灭 N+1 SQL）

### Task 3: MACD/DDE 批种一次加载

**Files:**
- Modify: `backend/etl/calc_batch_append.py`（`batch_append_macd`、`batch_append_dde`）
- Test: `tests/test_etl/test_batch_append_calc.py`

- [ ] **Step 1: Write the failing test**

```python
def test_batch_append_macd_loads_seeds_once(monkeypatch):
    """batch_append_macd calls load_ema_seeds_batch once for full ts_codes list."""
    import pandas as pd

    from backend.etl.calc_batch_append import batch_append_macd

    calls = []

    def spy(con, table, ts_codes, before_td, cols):
        calls.append(list(ts_codes))
        return {c: {"ema_12": 1.0, "ema_26": 2.0, "dea": 0.1} for c in ts_codes}

    monkeypatch.setattr(
        "backend.etl.calc_batch_seeds.load_ema_seeds_batch", spy,
    )
    monkeypatch.setattr(
        "backend.etl.calc_batch_append.insert_dws_batch_multi",
        lambda *a, **k: 2,
    )

    codes = ["S0.SZ", "S1.SZ", "S2.SZ"]
    df = pd.DataFrame({"trade_date": ["20260607", "20260608"], "close_qfq": [10.0, 10.1]})
    groups = {c: df for c in codes}
    new_bars = {c: ["20260608"] for c in codes}

    batch_append_macd(None, "daily", codes, "20260608", groups, new_bars)
    assert len(calls) == 1
    assert set(calls[0]) == set(codes)
```

- [ ] **Step 2: Run test — expect FAIL**（当前每股调一次 `load_ema_seeds_batch([ts_code], ...)`）

- [ ] **Step 3: Fix `batch_append_macd`**

在 `batch_append_macd` 循环**之前**：

```python
# Group by first_td — stocks sharing same anchor date share one seed query.
anchor_groups = {}
for ts_code in ts_codes:
    df = quote_groups.get(ts_code)
    new_bars = new_bars_map.get(ts_code, [])
    if df is None or len(df) == 0 or not new_bars:
        continue
    first_td = str(df.iloc[0]["trade_date"])
    anchor_groups.setdefault(first_td, []).append(ts_code)

seeds_by_code = {}
for first_td, codes_at_anchor in anchor_groups.items():
    batch_seeds = load_ema_seeds_batch(
        con, calc.dws_table, codes_at_anchor, first_td, seed_cols,
    )
    seeds_by_code.update(batch_seeds)

for ts_code in ts_codes:
    ...
    seeds = seeds_by_code.get(ts_code)
    out = calc._compute_indicators(df, ema_seeds=seeds)
    stock_rows.append(...)
```

对 `batch_append_dde` 做同样改动（`seed_cols = ("ddx2",)`）。

- [ ] **Step 4: Run tests — expect PASS**

Run: `python3 -m pytest tests/test_etl/test_batch_append_calc.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_append.py tests/test_etl/test_batch_append_calc.py
git commit -m "perf: batch EMA seed load once per anchor group in MACD/DDE append"
```

---

### Task 4: `load_zone_seeds_batch`（Volume）

**Files:**
- Modify: `backend/etl/calc_batch_seeds.py`
- Modify: `backend/etl/calc_batch_append.py`（`batch_append_volume`）
- Test: `tests/test_etl/test_batch_append_calc.py`

- [ ] **Step 1: Write the failing test**

```python
def test_load_zone_seeds_batch_matches_fetch_zone_seed():
    import duckdb

    from backend.etl.calc_batch_seeds import load_zone_seeds_batch
    from backend.etl.calc_volume import VolumeCalculator

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_volume_daily (
            ts_code TEXT, trade_date TEXT, calc_date TEXT, zone TEXT
        )
    """)
    con.execute("""
        INSERT INTO dws_volume_daily VALUES
        ('V.SZ','20260606','20260607','normal'),
        ('V.SZ','20260607','20260607','explosive'),
        ('W.SZ','20260606','20260607','low_volume')
    """)
    batch = load_zone_seeds_batch(con, "dws_volume_daily", ["V.SZ", "W.SZ"], "20260608")
    calc = VolumeCalculator(con, "daily")
    assert batch["V.SZ"] == "explosive"
    assert batch["W.SZ"] == calc._fetch_zone_seed("W.SZ", "20260608")
    con.close()
```

- [ ] **Step 2: Run test — expect FAIL**

- [ ] **Step 3: Implement `load_zone_seeds_batch`**

在 `backend/etl/calc_batch_seeds.py` 追加：

```python
def load_zone_seeds_batch(
    con,
    dws_table: str,
    ts_codes: List[str],
    before_trade_date: str,
) -> Dict[str, Optional[str]]:
    """Return {ts_code: zone} for latest DWS bar with trade_date < before_trade_date."""
    if not ts_codes:
        return {}
    ph = ",".join(["?"] * len(ts_codes))
    rows = con.execute(f"""
        SELECT ts_code, zone FROM (
            SELECT ts_code, zone,
                   ROW_NUMBER() OVER (
                       PARTITION BY ts_code
                       ORDER BY trade_date DESC, calc_date DESC
                   ) AS rn
            FROM {dws_table}
            WHERE ts_code IN ({ph}) AND trade_date < ?
              AND zone IS NOT NULL
        ) WHERE rn = 1
    """, list(ts_codes) + [before_trade_date]).fetchall()
    return {r[0]: r[1] for r in rows}
```

更新 `batch_append_volume`：按 `first_date` 分组批载 zone seed（同 Task 3 anchor 模式），再进 `_batch_append_loop`。

- [ ] **Step 4: Run golden `test_batch_append_volume_matches_per_stock_append` — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_seeds.py backend/etl/calc_batch_append.py tests/test_etl/test_batch_append_calc.py
git commit -m "perf: batch zone seed loader for volume append"
```

---

## Phase 3 — State 写入降噪（同日复跑关键）

### Task 5: `should_refresh_calc_state` + `upsert_calc_state_batch`

**Files:**
- Modify: `backend/etl/calc_state.py`
- Create: `tests/test_etl/test_calc_state_batch.py`
- Modify: `backend/config.py`

- [ ] **Step 1: Write the failing tests**

创建 `tests/test_etl/test_calc_state_batch.py`：

```python
import duckdb

from backend.db.schema import ensure_calc_state_table
from backend.etl.calc_state import (
    should_refresh_calc_state,
    upsert_calc_state_batch,
)


def test_should_refresh_false_when_fp_and_date_unchanged():
    state = {
        "last_trade_date": "20260608",
        "history_fp": "abc123",
        "updated_calc_date": "20260609",
    }
    assert should_refresh_calc_state(state, "20260609", "abc123") is False


def test_should_refresh_true_when_fp_changes():
    state = {
        "last_trade_date": "20260608",
        "history_fp": "old",
        "updated_calc_date": "20260609",
    }
    assert should_refresh_calc_state(state, "20260609", "new") is True


def test_upsert_calc_state_batch_round_trip():
    con = duckdb.connect(":memory:")
    ensure_calc_state_table(con)
    records = [
        ("000001.SZ", "daily", "macd", "20260608", "fp1", "20260609", None),
        ("000002.SZ", "daily", "macd", "20260608", "fp2", "20260609", None),
    ]
    n = upsert_calc_state_batch(con, records)
    assert n == 2
    cnt = con.execute("SELECT COUNT(*) FROM dws_calc_state").fetchone()[0]
    assert cnt == 2
    con.close()
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python3 -m pytest tests/test_etl/test_calc_state_batch.py -v`

- [ ] **Step 3: Implement helpers + config flag**

`backend/etl/calc_state.py`：

```python
def should_refresh_calc_state(
    state: Optional[dict],
    calc_date: str,
    new_history_fp: str,
) -> bool:
    """Skip UPSERT when same calc_date run already has identical history_fp."""
    if state is None:
        return True
    if state.get("updated_calc_date") == calc_date and state.get("history_fp") == new_history_fp:
        return False
    return True


def upsert_calc_state_batch(con, records: list) -> int:
    """Bulk INSERT OR REPLACE into dws_calc_state. records: 7-tuples matching upsert cols."""
    if not records:
        return 0
    import pandas as pd
    df = pd.DataFrame(records, columns=[
        "ts_code", "freq", "indicator", "last_trade_date", "history_fp",
        "updated_calc_date", "quote_latest_adj",
    ])
    df["spec_version"] = "v1"
    con.register("_calc_state_batch", df)
    con.execute("""
        INSERT OR REPLACE INTO dws_calc_state
            (ts_code, freq, indicator, last_trade_date, history_fp, quote_latest_adj,
             spec_version, updated_calc_date)
        SELECT ts_code, freq, indicator, last_trade_date, history_fp, quote_latest_adj,
               spec_version, updated_calc_date
        FROM _calc_state_batch
    """)
    con.unregister("_calc_state_batch")
    return len(df)
```

更新 `write_calc_state_from_df`：在 `upsert_calc_state` 前调用 `should_refresh_calc_state`；若 False 则 return False。

`backend/config.py` 追加：

```python
# CALC_SKIP_STATE_REFRESH: skip dws_calc_state UPSERT when history_fp unchanged on same calc_date
CALC_SKIP_STATE_REFRESH = os.getenv("CALC_SKIP_STATE_REFRESH", "1").strip() != "0"
```

`write_calc_state_from_df` 读取该 flag（`CALC_SKIP_STATE_REFRESH=0` 回退旧行为）。

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_state.py backend/config.py tests/test_etl/test_calc_state_batch.py
git commit -m "perf: skip redundant calc_state refresh on unchanged SKIP fingerprint"
```

---

### Task 6: batch_append / chunk SKIP 路径批量化 state 写

**Files:**
- Modify: `backend/etl/calc_batch_append.py`（`run_batch_append_phase` SKIP 循环）
- Modify: `backend/etl/orchestrator.py`（`_calc_stock_chunk` skip_keys 循环）
- Test: `tests/test_etl/test_calc_state_batch.py`

- [ ] **Step 1: Write integration test**

```python
def test_run_batch_append_skip_does_not_rewrite_unchanged_state(monkeypatch):
    """SKIP stocks with same fp should not call upsert_calc_state per indicator."""
    # Minimal monkeypatch on run_batch_append_phase: count upsert_calc_state calls.
    ...
```

（实施时写完整 fixture：2 股全 SKIP，`history_fp` 已匹配 → `upsert_calc_state` 调用 0 次。）

- [ ] **Step 2: Refactor SKIP loops to collect records**

`run_batch_append_phase` 中替换每股每指标 `write_calc_state_from_df` 为：

```python
state_records = []
for ts_code, modes in stock_modes.items():
    for (indicator_name, freq), (mode, _) in modes.items():
        if mode != "SKIP":
            continue
        ...
        fp = state_signature(tdf, st["last_trade_date"], sig_cols)
        if should_refresh_calc_state(st, calc_date, fp):
            state_records.append((ts_code, freq, indicator_name, st["last_trade_date"], fp, calc_date, None))
upsert_calc_state_batch(con, state_records)
```

`_calc_stock_chunk` 的 `skip_keys` 循环同样改为 chunk 级 `state_records` 聚合后一次 `upsert_calc_state_batch`。

- [ ] **Step 3: Run `pytest tests/test_etl/test_calc_state_batch.py tests/test_etl/test_orchestrator.py -v`**

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/etl/calc_batch_append.py backend/etl/orchestrator.py tests/test_etl/test_calc_state_batch.py
git commit -m "perf: batch calc_state upsert on SKIP paths"
```

---

## Phase 4 — 同日复跑 chunk 零重复 SQL（硬化）

### Task 7: `batch_ctx` 存在时禁止 chunk 重复 preflight SQL

**Files:**
- Modify: `backend/etl/orchestrator.py`（`_calc_stock_chunk` L944-950）
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_etl/test_orchestrator.py` 追加：

```python
def test_calc_stock_chunk_reuses_batch_ctx_without_reload(monkeypatch):
    """When batch_ctx has tails, chunk must not call load_calc_state_batch / batch_load_*."""
    calls = []

    def spy_load_state(*args, **kwargs):
        calls.append("state")
        return {}

    monkeypatch.setattr("backend.etl.calc_state.load_calc_state_batch", spy_load_state)
    monkeypatch.setattr("backend.etl.calc_fast_skip.batch_load_quote_tails", lambda *a, **k: (calls.append("quote"), {}))
    # Invoke _calc_stock_chunk with minimal batch_ctx + empty chunk that fast-skips
    ...
    assert "state" not in calls
    assert "quote" not in calls
```

- [ ] **Step 2: Run test — expect FAIL**（当 `batch_ctx` 缺键时仍走 `elif fast_on` 分支）

- [ ] **Step 3: Harden branch logic**

将 `_calc_stock_chunk` 中：

```python
        if batch_ctx and batch_ctx.get("state_map") is not None:
            ...
            fast_on = True
        elif fast_on:
            state_map = load_calc_state_batch(con, chunk)
            ...
```

改为：

```python
        if batch_ctx is not None:
            # Always reuse batch_append preloaded frames; never re-query tails in chunk.
            state_map = {k: v for k, v in batch_ctx.get("state_map", {}).items() if k[0] in chunk}
            daily_tails = {c: batch_ctx["daily_tails"][c] for c in chunk if c in batch_ctx.get("daily_tails", {})}
            weekly_tails = {c: batch_ctx["weekly_tails"][c] for c in chunk if c in batch_ctx.get("weekly_tails", {})}
            dde_daily = {c: batch_ctx["dde_daily"][c] for c in chunk if c in batch_ctx.get("dde_daily", {})}
            dde_weekly = {c: batch_ctx["dde_weekly"][c] for c in chunk if c in batch_ctx.get("dde_weekly", {})}
            fast_on = CALC_FAST_SKIP and CALC_APPEND and incremental
        elif fast_on:
            ...
```

**边界：** `CALC_BATCH_APPEND=0` 时 `batch_ctx=None`，仍走 `elif fast_on` 原路径（单测覆盖）。

- [ ] **Step 4: Run orchestrator tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/etl/orchestrator.py tests/test_etl/test_orchestrator.py
git commit -m "perf: chunk workers reuse batch_ctx tails without duplicate SQL"
```

---

## Phase 5 — 实库验收 + 文档

### Task 8: 墙钟 benchmark 与文档

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`（§12.7 配置项）
- Modify: 本 plan 状态

- [x] **Step 1: 全量测试**

Run: `python3 -m pytest tests/ -v`

Expected: ALL PASS

- [ ] **Step 2: 新日实库验收**（手动，需实库）

```bash
python3 -m backend.cli fetch
python3 -m backend.cli calc --date $(date +%Y%m%d)
```

| 检查项 | 预期 |
|--------|------|
| `progress calc.batch_append: done` | < 600s（stretch < 360s） |
| `calc ALL DONE` stk/s | **>10** |
| DWS 新 bar 行数 | ≈ 活跃股数 × 12 指标（日线+周线） |
| golden 等价 | 抽样 10 股 `v_dws_*_latest` 与上一版逐股 append 一致 |

- [ ] **Step 3: 同日复跑验收**（手动，需实库）

```bash
python3 -m backend.cli calc --date <same_date>   # 第二次
```

| 检查项 | 预期 |
|--------|------|
| calc 本体 | **<120s** |
| `calc idempotent skip` 或 fast_skip 主导 | 日志可见 |
| `0 calculated` + fingerprint_match | 与优化前一致 |

- [x] **Step 4: 更新 CLAUDE.md**

在 calc 段补充：

```markdown
- **性能专项（batch write）：** `insert_dws_batch_multi` 按 `(indicator,freq)` 一次窄写；MACD/DDE/Volume 种子批载；SKIP 路径 `CALC_SKIP_STATE_REFRESH` 跳过冗余 `dws_calc_state` UPSERT；`batch_ctx` chunk 零重复 tail SQL。
```

- [x] **Step 5: 本 plan 验收节打勾**（无 commit，benchmark 待手动）

```bash
git add CLAUDE.md docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md docs/superpowers/plans/2026-06-08-calc-performance-special.md
git commit -m "docs: calc performance special benchmark results and flags"
```

---

## 风险与边界

| 风险 | 缓解 |
|------|------|
| 批写合并后单行脏数据整批失败 | golden 测试 + 实库抽样；`insert_dws_batch_multi` 保持与单股相同列转换 |
| `should_refresh_calc_state` 误跳过 | 仅当 `history_fp` **且** `updated_calc_date` 同 calc_date；DWD 变更 → fp 变 → 仍刷新 |
| `CALC_BATCH_APPEND=0` 回退 | 不改慢路径语义；仅 batch 路径受益 |
| 写入仍受 DuckDB 单文件锁限 | 本专项不承诺 <60s 同日复跑；进一步需 v2 DDE weekly SQL 尾窗（见 partial-skip-v2 plan） |

## 验收标准

- [x] 全部 golden：`test_batch_append_calc.py` 6 指标 APPEND 等价 `atol=1e-9`
- [x] `insert_dws_batch_multi` 单测通过
- [x] `test_calc_state_batch.py` 通过
- [x] `pytest tests/ -v` 全绿（430 passed）
- [ ] 新日 calc **<600s**（目标 360s）— **手动实库 benchmark**
- [ ] 同日复跑 calc **<120s** — **手动实库 benchmark**
- [x] CLAUDE.md / spec 已更新

---

## Self-Review（计划自检）

**Spec 覆盖：**
- 新日批写批种 → Task 1–4
- 同日复跑 state/SQL 降噪 → Task 5–7
- 文档与验收 → Task 8

**Placeholder 扫描：** 无 TBD；Task 6 集成测试骨架用 `...` 标注——**实施者必须在编码时写全 fixture**（计划允许此处因 monkeypatch 依赖最终 chunk 签名；实施 Task 6 第一步禁止留 `...`）。

**类型一致性：** `stock_rows` 元组 5 元组、`upsert_calc_state_batch` 7 元组全文一致。
