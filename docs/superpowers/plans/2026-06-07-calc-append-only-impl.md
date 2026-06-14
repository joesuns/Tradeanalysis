# Calc 新日追算（Append-Only）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让常规交易日 calc 计算从 ~49min 降到秒级~低分钟级——通过「历史签名门禁」把绝大多数股路由到向量化只算新 bar 的 APPEND 路径，少数历史变动股走现有 FULL 重算。

**Architecture:** 双路径（SKIP / APPEND / FULL）+ 每股 `dws_calc_state` 状态表 + 强值序列签名。APPEND 跨股向量化只算新 bar，EMA 用存储种子递推、滚动指标读尾窗；FULL 复用现有 `calc_stock_pipeline` 并作 APPEND 的等价 oracle。三层开关 `CALC_APPEND` → `CALC_INCREMENTAL` → 全量。

**Tech Stack:** Python 3.9（用 `Optional[X]`，不用 `X | None`）、DuckDB、numpy/pandas、pytest。

**关联文档：**
- 设计 spec：`docs/superpowers/specs/2026-06-07-calc-append-only-design.md`
- 数据模型 spec：`docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` §12.7
- 增量优化：`docs/superpowers/plans/2026-06-07-calc-incremental-optimization.md`

**通用约定：**
- 测试命令统一 `python3 -m pytest ...`（环境无 `python`）。
- 提交风格沿用仓库：`feat:` / `fix:` / `perf:` / `test:` / `docs:`，小写，HEREDOC。
- 等价性 oracle：FULL 路径（`CALC_APPEND=0` 或 `recalc_start` 全量重算）。所有 APPEND 必须与之 `atol=1e-9` 逐值相等。

---

## Phase 0 — 基础设施（强签名 + 状态表 + 确定性指纹修复）

### Task 1: `load_latest_fingerprints` 非确定性修复

**背景：** 同一 calc_date 混有多代指纹时，`ROW_NUMBER ORDER BY calc_date DESC` 平局任取一行 → 跳过时灵时不灵。加 `trade_date DESC` 取最新 bar 的指纹（最新 bar 永远由最近一次写覆盖）。

**Files:**
- Modify: `backend/etl/base.py`（`load_latest_fingerprints`，约 line 462-486）
- Test: `tests/test_etl/test_incremental_calc.py`

- [ ] **Step 1: Write the failing test**

```python
def test_load_latest_fingerprints_picks_latest_trade_date_on_tie():
    """同一 calc_date 下混有两代指纹时，取最新 trade_date 的那条。"""
    from backend.etl.base import load_latest_fingerprints
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE dws_x (
            ts_code TEXT, trade_date TEXT, calc_date TEXT, input_fingerprint TEXT
        )
    """)
    # 同 calc_date=20260605，早 bar 旧指纹 OLD，最新 bar 新指纹 NEW
    con.execute("INSERT INTO dws_x VALUES ('A.SZ','20260101','20260605','OLD')")
    con.execute("INSERT INTO dws_x VALUES ('A.SZ','20260605','20260605','NEW')")
    fps = load_latest_fingerprints(con, "dws_x", ["A.SZ"])
    assert fps["A.SZ"] == "NEW"
    con.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_etl/test_incremental_calc.py::test_load_latest_fingerprints_picks_latest_trade_date_on_tie -v`
Expected: FAIL（可能返回 'OLD' 或不稳定）

- [ ] **Step 3: Implement — 排序加 `trade_date DESC`**

把 `load_latest_fingerprints` 中的窗口排序：

```python
        rows = con.execute(f"""
            SELECT ts_code, input_fingerprint FROM (
                SELECT ts_code, input_fingerprint,
                       ROW_NUMBER() OVER (PARTITION BY ts_code
                                          ORDER BY calc_date DESC, trade_date DESC) AS rn
                FROM {dws_table}
                WHERE ts_code IN ({placeholders}) AND input_fingerprint IS NOT NULL
            ) WHERE rn = 1
        """, ts_codes).fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_etl/test_incremental_calc.py::test_load_latest_fingerprints_picks_latest_trade_date_on_tie -v`
Expected: PASS

- [ ] **Step 5: 回归**

Run: `python3 -m pytest tests/test_etl/test_fingerprint_skip.py tests/test_etl/test_incremental_calc.py -q`
Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add backend/etl/base.py tests/test_etl/test_incremental_calc.py
git commit -m "fix: deterministic latest fingerprint via trade_date DESC tiebreak"
```

---

### Task 2: 强值序列签名 `compute_history_signature`

**背景：** 现 `compute_fingerprint` 仅 min/max/mean/count（弱、有碰撞面）。新增对**实际值序列（四舍五入）**的 SHA256，作为 `dws_calc_state.history_fp` 的计算函数。签名域含全 lookback（除权重标定全历史 → 签名必变）。

**Files:**
- Modify: `backend/etl/base.py`（新增函数，紧接 `compute_fingerprint` 之后）
- Test: `tests/test_etl/test_incremental_calc.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_history_signature_detects_value_change_summary_stats_stable():
    """针对弱指纹 M2：min/max/mean/count 不变但有值互换，强签名必须变。"""
    import pandas as pd
    from backend.etl.base import compute_history_signature
    # 两个 DataFrame：交换两行的 close 值 → min/max/mean/count 全同，但序列不同
    df1 = pd.DataFrame({
        "trade_date": ["20260101", "20260102", "20260103"],
        "close_qfq": [10.0, 20.0, 30.0],
        "vol": [100.0, 200.0, 300.0],
    })
    df2 = pd.DataFrame({
        "trade_date": ["20260101", "20260102", "20260103"],
        "close_qfq": [20.0, 10.0, 30.0],  # 交换前两行
        "vol": [100.0, 200.0, 300.0],
    })
    cols = ["close_qfq", "vol"]
    assert compute_history_signature(df1, cols) != compute_history_signature(df2, cols)


def test_history_signature_stable_under_float_noise_below_precision():
    """低于精度的浮点噪声不改变签名（精度=6）。"""
    import pandas as pd
    from backend.etl.base import compute_history_signature
    df1 = pd.DataFrame({"trade_date": ["20260101"], "close_qfq": [10.0000001], "vol": [100.0]})
    df2 = pd.DataFrame({"trade_date": ["20260101"], "close_qfq": [10.0000002], "vol": [100.0]})
    cols = ["close_qfq", "vol"]
    assert compute_history_signature(df1, cols) == compute_history_signature(df2, cols)


def test_history_signature_changes_on_real_change():
    import pandas as pd
    from backend.etl.base import compute_history_signature
    df1 = pd.DataFrame({"trade_date": ["20260101"], "close_qfq": [10.0], "vol": [100.0]})
    df2 = pd.DataFrame({"trade_date": ["20260101"], "close_qfq": [10.5], "vol": [100.0]})
    assert compute_history_signature(df1, ["close_qfq", "vol"]) != \
           compute_history_signature(df2, ["close_qfq", "vol"])
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_etl/test_incremental_calc.py -k history_signature -v`
Expected: FAIL（ImportError: cannot import name 'compute_history_signature'）

- [ ] **Step 3: Implement**

在 `backend/etl/base.py` 的 `compute_fingerprint` 之后新增：

```python
def compute_history_signature(df: "pd.DataFrame", cols: list,
                              precision: int = 6) -> str:
    """Strong content signature over actual value sequences (not summary stats).

    Hashes the row-ordered, rounded values of ``cols`` (+ trade_date) so any
    real value change flips the signature, while sub-precision float noise does
    not. Replaces the lossy min/max/mean/count fingerprint for state gating.
    """
    if df is None or df.empty:
        return hashlib.sha256(b"empty").hexdigest()[:16]
    parts = []
    td = df["trade_date"].astype(str).tolist()
    parts.append("td:" + ",".join(td))
    for col in sorted(cols):
        if col not in df.columns:
            parts.append(f"{col}:absent")
            continue
        vals = df[col].to_numpy(dtype=float)
        rounded = np.where(np.isnan(vals), np.nan, np.round(vals, precision))
        parts.append(f"{col}:" + ",".join(
            "nan" if np.isnan(v) else format(v, f".{precision}f") for v in rounded
        ))
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

确保 `base.py` 顶部已 `import numpy as np`（已有）。

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_etl/test_incremental_calc.py -k history_signature -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/etl/base.py tests/test_etl/test_incremental_calc.py
git commit -m "feat: strong value-sequence history signature for calc state gating"
```

---

### Task 3: `dws_calc_state` 表 DDL + 迁移

**Files:**
- Modify: `backend/db/schema.py`（新增 DDL 常量 + 在 `_create_dws` 后建表 + 迁移函数 + `drop_all_tables` 增列）
- Test: `tests/test_db/test_calc_state.py`（Create）

- [ ] **Step 1: Write the failing test**

```python
import duckdb
from backend.db.schema import create_all_tables


def test_dws_calc_state_table_exists_with_pk():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info('dws_calc_state')").fetchall()}
    assert {"ts_code", "freq", "last_trade_date", "history_fp",
            "quote_latest_adj", "spec_version", "updated_calc_date"} <= cols
    # PK 去重：同 (ts_code, freq) 二次插入 OR REPLACE 不增行
    con.execute("INSERT OR REPLACE INTO dws_calc_state "
                "(ts_code, freq, last_trade_date, history_fp, updated_calc_date) "
                "VALUES ('A.SZ','daily','20260101','fp1','20260101')")
    con.execute("INSERT OR REPLACE INTO dws_calc_state "
                "(ts_code, freq, last_trade_date, history_fp, updated_calc_date) "
                "VALUES ('A.SZ','daily','20260102','fp2','20260102')")
    n = con.execute("SELECT COUNT(*) FROM dws_calc_state WHERE ts_code='A.SZ'").fetchone()[0]
    assert n == 1
    con.close()
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_db/test_calc_state.py -v`
Expected: FAIL（table dws_calc_state 不存在）

- [ ] **Step 3: Implement DDL + 建表 + 迁移**

在 `backend/db/schema.py` 的 `_create_dws` 函数（约 line 953）尾部追加建表：

```python
def _create_dws(con: duckdb.DuckDBPyConnection):
    """Create all 10 DWS tables from templates + the calc-state table."""
    for freq in ("daily", "weekly"):
        for name, ddl in _DWS_DDL.items():
            table = f"dws_{name}_{freq}"
            con.execute(ddl.format(table=table))
    con.execute("""
        CREATE TABLE IF NOT EXISTS dws_calc_state (
            ts_code           VARCHAR NOT NULL,
            freq              VARCHAR NOT NULL,
            last_trade_date   VARCHAR NOT NULL,
            history_fp        VARCHAR NOT NULL,
            quote_latest_adj  DOUBLE,
            spec_version      VARCHAR DEFAULT 'v1',
            updated_calc_date VARCHAR NOT NULL,
            PRIMARY KEY (ts_code, freq)
        )
    """)
```

在 `drop_all_tables` 的 `_all_tables` 列表中加入 `"dws_calc_state"`（DWS 区块附近），确保测试可清库。

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_db/test_calc_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/db/schema.py tests/test_db/test_calc_state.py
git commit -m "feat: add dws_calc_state table for append-only calc routing"
```

---

## Phase 1 — 状态读写 + 路由判定

### Task 4: 状态读写 helper

**Files:**
- Create: `backend/etl/calc_state.py`
- Test: `tests/test_etl/test_calc_state.py`

- [ ] **Step 1: Write the failing test**

```python
import duckdb
from backend.db.schema import create_all_tables
from backend.etl.calc_state import load_calc_state, upsert_calc_state


def test_upsert_and_load_calc_state():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    assert load_calc_state(con, "daily", ["A.SZ"]) == {}
    upsert_calc_state(con, "A.SZ", "daily",
                      last_trade_date="20260605", history_fp="fp1",
                      quote_latest_adj=1.23, calc_date="20260605")
    st = load_calc_state(con, "daily", ["A.SZ", "B.SZ"])
    assert st["A.SZ"]["last_trade_date"] == "20260605"
    assert st["A.SZ"]["history_fp"] == "fp1"
    assert "B.SZ" not in st
    # 覆盖
    upsert_calc_state(con, "A.SZ", "daily",
                      last_trade_date="20260608", history_fp="fp2",
                      quote_latest_adj=1.24, calc_date="20260608")
    st2 = load_calc_state(con, "daily", ["A.SZ"])
    assert st2["A.SZ"]["history_fp"] == "fp2"
    con.close()
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_etl/test_calc_state.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: Implement**

```python
"""Read/write helpers for dws_calc_state (append-only calc routing)."""
from typing import Optional, List, Dict


def load_calc_state(con, freq: str, ts_codes: List[str]) -> Dict[str, dict]:
    """Return {ts_code: {last_trade_date, history_fp, quote_latest_adj}} for freq."""
    if not ts_codes:
        return {}
    ph = ",".join(["?"] * len(ts_codes))
    rows = con.execute(f"""
        SELECT ts_code, last_trade_date, history_fp, quote_latest_adj
        FROM dws_calc_state
        WHERE freq = ? AND ts_code IN ({ph})
    """, [freq] + list(ts_codes)).fetchall()
    return {
        r[0]: {"last_trade_date": r[1], "history_fp": r[2], "quote_latest_adj": r[3]}
        for r in rows
    }


def upsert_calc_state(con, ts_code: str, freq: str, last_trade_date: str,
                      history_fp: str, calc_date: str,
                      quote_latest_adj: Optional[float] = None,
                      spec_version: str = "v1"):
    """Insert-or-replace one (ts_code, freq) state row."""
    con.execute("""
        INSERT OR REPLACE INTO dws_calc_state
            (ts_code, freq, last_trade_date, history_fp, quote_latest_adj,
             spec_version, updated_calc_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [ts_code, freq, last_trade_date, history_fp, quote_latest_adj,
          spec_version, calc_date])
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_etl/test_calc_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_state.py tests/test_etl/test_calc_state.py
git commit -m "feat: dws_calc_state read/write helpers"
```

---

### Task 5: 路由判定 `classify_calc_mode`

**背景：** 输入「已加载的 per-stock quote df（尾窗）」+ state，输出 SKIP / APPEND / FULL 及新 bar 列表。签名域 = 整个 df（已含 lookback）。

**Files:**
- Create: `backend/etl/calc_router.py`
- Test: `tests/test_etl/test_calc_router.py`

- [ ] **Step 1: Write the failing test**

```python
import pandas as pd
from backend.etl.calc_router import classify_calc_mode

SIG_COLS = ["close_qfq", "vol"]


def _df(dates, closes):
    return pd.DataFrame({"trade_date": dates, "close_qfq": closes,
                         "vol": [100.0] * len(dates)})


def test_no_state_returns_full():
    df = _df(["20260101", "20260102"], [10.0, 11.0])
    mode, new_bars = classify_calc_mode(df, state=None, sig_cols=SIG_COLS)
    assert mode == "FULL"


def test_signature_changed_returns_full():
    df = _df(["20260101", "20260102"], [10.0, 11.0])
    state = {"last_trade_date": "20260102", "history_fp": "STALE"}
    mode, _ = classify_calc_mode(df, state=state, sig_cols=SIG_COLS)
    assert mode == "FULL"


def test_no_new_bars_same_sig_returns_skip():
    df = _df(["20260101", "20260102"], [10.0, 11.0])
    from backend.etl.base import compute_history_signature
    fp = compute_history_signature(df, SIG_COLS)
    state = {"last_trade_date": "20260102", "history_fp": fp}
    mode, new_bars = classify_calc_mode(df, state=state, sig_cols=SIG_COLS)
    assert mode == "SKIP"
    assert new_bars == []


def test_new_bars_same_history_returns_append():
    # df 含到 0103；历史签名按 [.., last_trade_date=0102] 子集算
    df = _df(["20260101", "20260102", "20260103"], [10.0, 11.0, 12.0])
    from backend.etl.base import compute_history_signature
    hist = df[df["trade_date"] <= "20260102"]
    fp = compute_history_signature(hist, SIG_COLS)
    state = {"last_trade_date": "20260102", "history_fp": fp}
    mode, new_bars = classify_calc_mode(df, state=state, sig_cols=SIG_COLS)
    assert mode == "APPEND"
    assert new_bars == ["20260103"]
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_etl/test_calc_router.py -v`
Expected: FAIL（module not found）

- [ ] **Step 3: Implement**

```python
"""Route each stock to SKIP / APPEND / FULL for append-only calc."""
from typing import Optional, List, Tuple
import pandas as pd
from backend.etl.base import compute_history_signature


def classify_calc_mode(df: "pd.DataFrame", state: Optional[dict],
                       sig_cols: List[str]) -> Tuple[str, List[str]]:
    """Decide calc mode for one stock given its loaded tail-window df and state.

    Returns (mode, new_bars) where mode in {"SKIP","APPEND","FULL"} and
    new_bars is the list of trade_dates strictly after state.last_trade_date.

    - No state                       -> FULL (establish baseline)
    - History signature changed      -> FULL (ex-div / fill / correction)
    - Same signature, no new bars     -> SKIP
    - Same signature, has new bars    -> APPEND
    Signature domain = bars up to and including state.last_trade_date.
    """
    if state is None:
        return "FULL", []
    last_td = state["last_trade_date"]
    hist = df[df["trade_date"] <= last_td]
    cur_fp = compute_history_signature(hist, sig_cols)
    if cur_fp != state["history_fp"]:
        return "FULL", []
    new_bars = df[df["trade_date"] > last_td]["trade_date"].astype(str).tolist()
    if not new_bars:
        return "SKIP", []
    return "APPEND", new_bars
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_etl/test_calc_router.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_router.py tests/test_etl/test_calc_router.py
git commit -m "feat: classify_calc_mode router (SKIP/APPEND/FULL)"
```

---

## Phase 2 — APPEND 计算（逐指标，等价性锁定）

**通用模式（每个指标任务都遵循）：**
1. 在 Calculator 上新增方法 `append_calculate(self, ts_code, df, new_bars, calc_date, state)`。
2. `append_calculate` 用**完整尾窗 df** 计算指标（与 FULL 同算法、同窗口），但只 `_insert(write_start=new_bars[0], write_end=new_bars[-1])` 写新 bar。
3. EMA 类用 `resolve_ema_seeds` 取种子递推（已实现）。
4. **等价性测试（契约）**：对同一 df，FULL 全量算出的「新 bar 行」与 `append_calculate` 写出的行 `atol=1e-9` 相等。

> **实施者注意：** 写每个指标的 `append_calculate` 前，先 Read 对应 `calc_*.py` 的 `_compute_*` 与 `_insert`，复用其计算与列定义，仅改「写窗口」为 new_bars。

### Task 6: PricePosition APPEND（最简，先立模式）

**Files:**
- Modify: `backend/etl/calc_price_position.py`
- Test: `tests/test_etl/test_append_calc.py`（Create）

- [ ] **Step 1: Write the failing equivalence test**

```python
import duckdb
import pandas as pd
import numpy as np
from backend.etl.calc_price_position import PricePositionCalculator


def _make_df(n=300):
    dates = [f"2026{1000 + i:04d}" for i in range(n)]  # 形如 '20261000'+i 占位
    # 用真实可排序日期
    dates = [f"202601{i+1:02d}" if i < 28 else f"202602{i-27:02d}" for i in range(n)]
    rng = np.random.default_rng(42)
    close = 10 + np.cumsum(rng.normal(0, 0.2, n))
    return pd.DataFrame({"trade_date": dates, "close_qfq": close})


def test_pp_append_matches_full_on_new_bar():
    df = _make_df(300)
    full_df = PricePositionCalculator(None, "daily")._compute_positions(df.copy())
    last_full = full_df.iloc[-1]  # 最后一根 = 新 bar

    appended = PricePositionCalculator(None, "daily")._compute_positions_append(
        df.copy(), new_bars=[df.iloc[-1]["trade_date"]])
    app_row = appended[appended["trade_date"] == df.iloc[-1]["trade_date"]].iloc[0]

    for w in (60, 120, 250):
        col = f"price_position_{w}d"
        a, b = app_row[col], last_full[col]
        if pd.isna(b):
            assert pd.isna(a)
        else:
            assert abs(a - b) < 1e-9
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/test_etl/test_append_calc.py::test_pp_append_matches_full_on_new_bar -v`
Expected: FAIL（`_compute_positions_append` 不存在）

- [ ] **Step 3: Implement `_compute_positions_append`**

PP 是纯滚动函数，新 bar 值只取决于尾窗。最简实现：复用 `_compute_positions`（全窗算），返回的 df 含所有 bar；调用方只写 new_bars。为体现「只算新 bar」语义并避免无谓全列计算，提供薄封装：

```python
    def _compute_positions_append(self, df: pd.DataFrame, new_bars: list) -> pd.DataFrame:
        """Compute positions over the full tail window, return full df.

        PP at any bar depends only on its trailing window, which the tail-window
        df fully contains; the caller writes only new_bars rows. Equivalent to
        FULL on those bars by construction.
        """
        return self._compute_positions(df)
```

并新增 `append_calculate`：

```python
    def append_calculate(self, ts_code: str, df: pd.DataFrame, new_bars: list,
                         calc_date: str, state: dict) -> "CalcResult":
        from backend.etl.base import compute_history_signature
        result = CalcResult()
        df = self._compute_positions_append(df, new_bars)
        fp = compute_history_signature(df, ["close_qfq"])
        self._insert(ts_code, df, calc_date, input_fingerprint=fp,
                     write_start=new_bars[0], write_end=new_bars[-1])
        result.calculated += 1
        return result
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_etl/test_append_calc.py::test_pp_append_matches_full_on_new_bar -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_price_position.py tests/test_etl/test_append_calc.py
git commit -m "feat: PricePosition append_calculate with FULL equivalence"
```

---

### Task 7: MA APPEND

**Files:** Modify `backend/etl/calc_ma.py`; Test `tests/test_etl/test_append_calc.py`

- [ ] **Step 1:** Read `backend/etl/calc_ma.py` 的 `_compute_*` + `_insert`，记下输出列与 float_cols。
- [ ] **Step 2: Write equivalence test**（与 Task 6 同型：FULL 算出的最后一根 == append 写出的新 bar 行，对 MA 全部输出列 `atol=1e-9`）。MA 为有限窗口（MA5/MA10 + 5-bar 回归），新 bar 只依赖尾窗。
- [ ] **Step 3:** 新增 `append_calculate`：复用现 `_compute_*`（全尾窗算），`_insert(write_start=new_bars[0], write_end=new_bars[-1])`，`input_fingerprint=compute_history_signature(df, ["close_qfq"])`。
- [ ] **Step 4:** 跑等价测试 PASS。
- [ ] **Step 5: Commit** `feat: MA append_calculate with FULL equivalence`

---

### Task 8: KPattern APPEND

**Files:** Modify `backend/etl/calc_kpattern.py`; Test `tests/test_etl/test_append_calc.py`

- [ ] **Step 1:** Read `calc_kpattern.py`：形态判定依赖当根 + 近几根 + MA10 趋势上下文，确认所需最大回看根数。
- [ ] **Step 2: Write equivalence test**：FULL 最后一根的形态 + strength == append 新 bar 行（全列 `atol=1e-9`，形态字符串相等）。
- [ ] **Step 3:** 新增 `append_calculate`：尾窗须 ≥ 形态所需回看；复用现形态计算；只写 new_bars。
- [ ] **Step 4:** PASS。
- [ ] **Step 5: Commit** `feat: KPattern append_calculate with FULL equivalence`

---

### Task 9: MACD APPEND（EMA 种子递推）

**Files:** Modify `backend/etl/calc_macd.py`; Test `tests/test_etl/test_append_calc.py`

- [ ] **Step 1:** Read `calc_macd.py` 的 `_compute_indicators`（已支持 `ema_seeds`）与 `_compute_divergence`（用 `compute_price_signal_divergence`）。
- [ ] **Step 2: Write equivalence tests（两个）：**
  - (a) EMA 种子递推等价：构造 df，用 `resolve_ema_seeds` 取倒数第 2 根后的种子，append 算最后一根的 `ema_12/ema_26/dif/dea/macd` == FULL 最后一根，`atol=1e-9`。
  - (b) 背离等价：FULL 最后一根的 `divergence` 标记 == append 新 bar（确认日/去重在尾窗内一致；尾窗须 ≥ 60+dedup）。
- [ ] **Step 3:** 新增 `append_calculate`：
  - 用 `resolve_ema_seeds(self.con, self.dws_table, ts_code, df, self.freq, ("ema_12","ema_26","dea"), recalc_start=new_bars[0])` 取种子；
  - `_compute_indicators(df, ema_seeds=seeds)` 全尾窗算；
  - `_insert(write_start=new_bars[0], write_end=new_bars[-1])`，`input_fingerprint=compute_history_signature(df, ["close_qfq"])`。
- [ ] **Step 4:** 两个等价测试 PASS。
- [ ] **Step 5: Commit** `feat: MACD append_calculate (seeded EMA) with FULL equivalence`

---

### Task 10: DDE APPEND

**Files:** Modify `backend/etl/calc_dde.py`; Test `tests/test_etl/test_append_calc.py`

- [ ] **Step 1:** Read `calc_dde.py`：`ddx2 = EMA(ddx,5)`（种子递推），背离用 `compute_price_signal_divergence(require_finite_signal_window=True, spike_filter_top=True)`；DDE 输入含 moneyflow。注意 BSE(.BJ) 无 moneyflow。
- [ ] **Step 2: Write equivalence tests：** ddx2 种子递推等价 + 背离等价（同 Task 9 形态）。
- [ ] **Step 3:** 新增 `append_calculate`：种子取 `("ddx2",)`；签名列含 moneyflow 净额列 + `close_qfq`；只写 new_bars。
- [ ] **Step 4:** PASS。
- [ ] **Step 5: Commit** `feat: DDE append_calculate with FULL equivalence`

---

### Task 11: Volume APPEND

**Files:** Modify `backend/etl/calc_volume.py`; Test `tests/test_etl/test_append_calc.py`

- [ ] **Step 1:** Read `calc_volume.py`：volume_ratio、加权回归趋势（`weighted_window_slopes`）、120 日分位 + 迟滞、量价背离（`compute_price_signal_divergence`）。**注意迟滞（zone 进/出）是有状态的**——确认 zone 的递推是否依赖前一根 zone；若是，append 需取前一根 zone 作种子。
- [ ] **Step 2: Write equivalence test**：FULL 最后一根全列（含 zone）== append 新 bar；**专门覆盖迟滞跨阈值的边界用例**（zone 切换发生在新 bar）。
- [ ] **Step 3:** 新增 `append_calculate`：尾窗 ≥ 120；若 zone 有状态，从 DWS 取前一根 zone 作起点；只写 new_bars。签名列 `["close_qfq","vol"]`。
- [ ] **Step 4:** PASS（含迟滞边界）。
- [ ] **Step 5: Commit** `feat: Volume append_calculate with FULL equivalence`

---

## Phase 3 — 编排接入 + 灰度 + 验收

### Task 12: `CALC_APPEND` 开关

**Files:** Modify `backend/config.py`; Test `tests/test_etl/test_calc_router.py`（或新建）

- [ ] **Step 1: Write test**：`monkeypatch.setenv("CALC_APPEND","0")` 后 `importlib.reload(config)`，断言 `config.CALC_APPEND is False`；默认 True。
- [ ] **Step 2: Run fail.**
- [ ] **Step 3: Implement** —— 在 `backend/config.py` 末尾加：

```python
CALC_APPEND = os.getenv("CALC_APPEND", "1").strip() != "0"
```

- [ ] **Step 4: Run pass.**
- [ ] **Step 5: Commit** `feat: add CALC_APPEND flag (default on)`

---

### Task 13: APPEND 接入单股 pipeline

**背景：** 在 `calc_stock_pipeline`（`backend/etl/orchestrator.py:679`）内，对每股每 freq 用 `classify_calc_mode` 路由：FULL → 现 `calc()`；APPEND → `append_calculate()`；SKIP → 跳过。计算完更新 `dws_calc_state`。受 `CALC_APPEND` 开关控制（关 → 全走 FULL）。

**Files:** Modify `backend/etl/orchestrator.py`; Test `tests/test_etl/test_append_calc.py`

- [ ] **Step 1: Write integration test**（用 `:memory:` + `create_all_tables`，造一只股 DWD 到 T，先跑一次（FULL 建 state），再追加一根 DWD bar 到 T+1 跑第二次，断言：第二次该股走 APPEND（state.last_trade_date==T+1），且 T+1 的 DWS 行与「整段全量重算」在 T+1 上 `atol=1e-9` 相等，且 T 及更早历史行未被改写（calc_date 不变））。
- [ ] **Step 2: Run fail.**
- [ ] **Step 3: Implement** —— 在 `calc_stock_pipeline` 内按 freq：
  - load state（`load_calc_state`）；
  - 对该 freq 的 quote df 调 `classify_calc_mode`；
  - 分支调用 `calc()`（FULL）或 `append_calculate()`（APPEND）或跳过（SKIP）；
  - 成功后 `upsert_calc_state(...)`（`last_trade_date = df.trade_date.max()`，`history_fp = compute_history_signature(df, sig_cols)`，`quote_latest_adj` 可后续补）；
  - `CALC_APPEND=0` 时强制 FULL。
  - DDE moneyflow 路径单独处理其 df 与签名列。
- [ ] **Step 4: Run pass.**
- [ ] **Step 5: Commit** `feat: wire append-only routing into calc_stock_pipeline`

---

### Task 14: 全链路回归 + 文档

**Files:** Modify `CLAUDE.md`、`docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`

- [ ] **Step 1:** `python3 -m pytest tests/ -q` 全绿。
- [ ] **Step 2（实库验收，人工）：** 在真实库连跑两次最新数据日 calc：
  - 第 1 次：建 state（多数 FULL），记耗时。
  - 第 2 次（数据未变）：应几乎全 SKIP/APPEND，**秒级**；`health_check` 全绿；抽样股 APPEND 结果 == 全量重算。
  - 模拟「新增一个交易日」：仅该日走 APPEND，验证耗时与正确性。
- [ ] **Step 3:** 更新 `CLAUDE.md`：calc flow 增加双路径（SKIP/APPEND/FULL）、`CALC_APPEND` 开关、`dws_calc_state`、强签名说明；更新 spec §12.7。
- [ ] **Step 4: Commit** `docs: document append-only calc (CALC_APPEND, dws_calc_state)`

---

## Self-Review 覆盖核对

- 设计 §3 双路径 → Task 5(路由) + Task 13(接入) ✓
- 设计 §4 状态表 → Task 3(DDL) + Task 4(读写) ✓
- 设计 §4.1 强签名（修 M2/H2/M1）→ Task 2 ✓
- 设计 §5 向量化追加 → Task 6-11（先逐指标等价；跨股批量化在 Task 13 接入后作为**性能后续项**，见下）✓（正确性优先）
- 设计 §6 判定流程 → Task 5 ✓
- 设计 §7 等价性 → Task 6-11 每个含 `atol=1e-9` 契约测试 ✓
- 设计 §10 指纹非确定性 → Task 1 ✓
- 设计 §6/§3 三层开关 → Task 12 + Task 13 ✓

**范围提示（YAGNI/分期）：** 本计划 Task 6-13 先落「逐股 APPEND + 等价锁定」，已能大幅减负（每股算/写 1 bar 而非 255）。**若实库验收显示逐股迭代开销仍是瓶颈（1.6 stk/s 未显著改善），再追加「跨股向量化批处理」专项**（把 Task 13 的逐股循环改为按 freq 批量取尾窗 + 跨股一次性算新 bar）。先证正确、再压性能，避免一次性大爆炸难以等价验证。
