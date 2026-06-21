# 个股所属板块/概念导出 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在分析 Excel 导出中新增"行业板块"（TDX 通达信行业板块）和"概念板块"（DC 东方财富概念板块）两列，以 trade_date 为时间锚点，逗号分隔展示。

**Architecture:** 新建 ODS 三层表（snapshot/board/member）存储 TDX + DC 板块数据，7 天 TTL 缓存；fetch 末尾低优先级拉取、失败降级不阻断；导出时按 trade_date 查询板块成员并 GROUP_CONCAT 合并到 DataFrame。同时清理废弃的 tushare `concept` API 管线。

**Tech Stack:** DuckDB, tushare `tdx_index`/`tdx_member`/`dc_index`/`dc_member`, pandas

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `backend/db/schema.py` | 新增 3 张 ODS 板块表 DDL；标记旧 concept 表 deprecated | 修改 |
| `backend/fetch/ods_plate.py` | TDX + DC 板块拉取、TTL 缓存、UPSERT 入库 | **新建** |
| `backend/etl/build_dim.py` | 删除 `build_dim_concept()` 函数 | 修改 |
| `backend/etl/orchestrator.py` | 替换 concept 拉取为 plate 拉取；移除 build_dim_concept 步骤 | 修改 |
| `backend/export_wide.py` | 新增行业板块/概念板块列；`sector` 中文名改为"上市板块" | 修改 |
| `backend/export_column_comments.py` | 加载新列的表头注释 | 修改 |
| `docs/export/export-column-comments.yaml` | 更新 sector 注释；新增 tdx_industry_board / dc_concept_board 注释 | 修改 |
| `tests/test_fetch/test_ods_plate.py` | 板块拉取 + TTL 缓存逻辑测试 | **新建** |
| `tests/test_export/test_export_plate_columns.py` | 导出列存在性 + 合并逻辑测试 | **新建** |
| `CLAUDE.md` | 更新命令、架构、注意事项 | 修改 |

---

### Task 1: 新增 ODS 板块表 DDL

**Files:**
- Modify: `backend/db/schema.py`（在 `ods_concept_detail` DDL 之后插入）

**背景：** 参照 123 的 `dc_concept_meta`/`dc_concept_board`/`dc_concept_member` 三层设计，新增三张表，通过 `source` 列区分 TDX 和 DC 数据源。

- [ ] **Step 1: 在 `schema.py` 中插入三张新表的 DDL**

在 `ods_concept_detail` 的 DDL 块（约第 102 行）之后插入以下内容。先找到精确插入位置：

```python
# 位置：schema.py _ALL_DDL 列表中，ods_concept_detail 条目之后

# 3.7 ods_plate_snapshot — TTL-tracked plate fetch metadata
"""CREATE TABLE IF NOT EXISTS ods_plate_snapshot (
    trade_date     TEXT NOT NULL,
    source         TEXT NOT NULL,
    idx_type       TEXT NOT NULL,
    n_boards       INTEGER,
    n_members      INTEGER,
    fetched_at     TEXT NOT NULL DEFAULT (now()),
    PRIMARY KEY (trade_date, source, idx_type)
)""",

# 3.8 ods_plate_board — plate (board) definitions
"""CREATE TABLE IF NOT EXISTS ods_plate_board (
    trade_date     TEXT NOT NULL,
    source         TEXT NOT NULL,
    board_ts_code  TEXT NOT NULL,
    board_name     TEXT,
    fetched_at     TEXT NOT NULL DEFAULT (now()),
    PRIMARY KEY (trade_date, source, board_ts_code)
)""",

# 3.9 ods_plate_member — plate member stocks
"""CREATE TABLE IF NOT EXISTS ods_plate_member (
    trade_date     TEXT NOT NULL,
    source         TEXT NOT NULL,
    board_ts_code  TEXT NOT NULL,
    con_code       TEXT NOT NULL,
    con_name       TEXT,
    fetched_at     TEXT NOT NULL DEFAULT (now()),
    PRIMARY KEY (trade_date, source, board_ts_code, con_code)
)""",
```

同时在 `ods_concept_detail` 的 DDL 上方加一行注释标记其为 deprecated：

```python
# 3.6 ods_concept_detail — DEPRECATED (replaced by ods_plate_* tables, 2026-06-21)
```

- [ ] **Step 2: 在 teardown 列表中注册新表**

找到 schema.py 末尾的 `_TEARDOWN_ORDER` 列表，在 `ods_concept_detail` 条目附近插入三张新表（放在 concept 相关表之前或同级位置）。确认精确位置后用 Edit 工具修改。

- [ ] **Step 3: 运行 schema 迁移验证**

```bash
python -c "
from backend.db.connection import get_connection
con = get_connection(read_only=False)
tables = con.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ods_plate%'\").fetchall()
print('New tables:', tables)
con.close()
"
```

预期：输出 `[('ods_plate_snapshot',), ('ods_plate_board',), ('ods_plate_member',)]`

- [ ] **Step 4: Commit**

```bash
git add backend/db/schema.py
git commit -m "feat: add ods_plate_snapshot/board/member DDL for TDX+DC plate data

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 板块数据拉取模块

**Files:**
- Create: `backend/fetch/ods_plate.py`

**背景：** 新建拉取模块，统一处理 TDX 行业板块 + DC 概念板块。核心逻辑：(1) 检查 TTL 缓存；(2) 未命中则逐个板块拉取成员股；(3) UPSERT 入库。

- [ ] **Step 1: 创建拉取模块骨架 + TTL 检查函数**

```python
"""Fetch TDX industry plates and DC concept plates from tushare.

Data sources:
  - TDX (通达信): tdx_index(idx_type='行业板块') → tdx_member per board
  - DC  (东方财富): dc_index(idx_type='概念板块') → dc_member per board

TTL: 7-day cache per (trade_date, source, idx_type) tracked via ods_plate_snapshot.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# TTL in days — plate membership changes slowly
_PLATE_SNAPSHOT_TTL_DAYS = 7

# Per-source tushare API config
_PLATE_SOURCES = {
    "tdx": {
        "index_func": "tdx_index",
        "member_func": "tdx_member",
        "idx_type": "行业板块",
        "ts_code_field": "ts_code",
        "name_field": "name",
        "member_con_code_field": "con_code",
        "member_con_name_field": "con_name",
    },
    "dc": {
        "index_func": "dc_index",
        "member_func": "dc_member",
        "idx_type": "概念板块",
        "ts_code_field": "ts_code",
        "name_field": "name",
        "member_con_code_field": "con_code",
        "member_con_name_field": "name",
    },
}


def _is_snapshot_fresh(con, trade_date: str, source: str, idx_type: str) -> bool:
    """Check if a valid snapshot exists within TTL for (trade_date, source, idx_type)."""
    cutoff = (datetime.now() - timedelta(days=_PLATE_SNAPSHOT_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    row = con.execute(
        """SELECT fetched_at FROM ods_plate_snapshot
           WHERE trade_date = ? AND source = ? AND idx_type = ?""",
        [trade_date, source, idx_type]
    ).fetchone()
    if row is None:
        return False
    return row[0] >= cutoff


def _count_members_for_date(con, trade_date: str, source: str) -> int:
    """Count existing member rows for a given date+source (fast stale check)."""
    row = con.execute(
        "SELECT COUNT(*) FROM ods_plate_member WHERE trade_date = ? AND source = ?",
        [trade_date, source]
    ).fetchone()
    return row[0] if row else 0


def _fetch_boards(client, trade_date: str, source_cfg: dict) -> list[dict]:
    """Fetch board list from tushare index API. Returns list of board dicts."""
    records = client.call(
        source_cfg["index_func"],
        trade_date=trade_date,
        idx_type=source_cfg["idx_type"],
    )
    ts_code_field = source_cfg["ts_code_field"]
    name_field = source_cfg["name_field"]
    boards = []
    for r in records:
        ts_code = r.get(ts_code_field, "")
        name = r.get(name_field, "")
        if ts_code and name:
            boards.append({"board_ts_code": ts_code, "board_name": name})
    return boards


def _fetch_members_for_board(client, trade_date: str, board_ts_code: str,
                              source_cfg: dict) -> list[dict]:
    """Fetch member stocks for a single board. Returns list of {con_code, con_name}."""
    records = client.call(
        source_cfg["member_func"],
        trade_date=trade_date,
        ts_code=board_ts_code,
    )
    con_code_field = source_cfg["member_con_code_field"]
    con_name_field = source_cfg["member_con_name_field"]
    members = []
    for r in records:
        con_code = r.get(con_code_field, "")
        con_name = r.get(con_name_field, "")
        if con_code:
            members.append({"con_code": con_code, "con_name": con_name or ""})
    return members
```

- [ ] **Step 2: 实现主拉取函数 `fetch_plate_data()`**

```python
def fetch_plate_data(client, con, trade_date: str, ts_codes: Optional[list[str]] = None) -> dict:
    """Fetch TDX + DC plate members for a trade_date. TTL-cached; degraded on failure.

    Returns dict: {source: {n_boards, n_members, cached, error}}
    """
    results = {}
    for source, cfg in _PLATE_SOURCES.items():
        idx_type = cfg["idx_type"]
        result = {"n_boards": 0, "n_members": 0, "cached": False, "error": None}

        # TTL gate
        if _is_snapshot_fresh(con, trade_date, source, idx_type):
            n = _count_members_for_date(con, trade_date, source)
            result["n_members"] = n
            result["cached"] = True
            logger.info(
                "progress fetch.plate: %s %s cache hit | members=%d",
                source, idx_type, n,
            )
            results[source] = result
            continue

        logger.info(
            "progress fetch.plate: %s %s cache miss | fetching...",
            source, idx_type,
        )
        t_start = time.monotonic()

        try:
            # Step A: fetch board list
            boards = _fetch_boards(client, trade_date, cfg)
            result["n_boards"] = len(boards)
            logger.info(
                "progress fetch.plate: %s %s boards=%d",
                source, idx_type, len(boards),
            )

            # Step B: fetch members per board
            total_members = 0
            for i, b in enumerate(boards):
                try:
                    members = _fetch_members_for_board(
                        client, trade_date, b["board_ts_code"], cfg,
                    )
                    # UPSERT board
                    con.execute(
                        """INSERT OR REPLACE INTO ods_plate_board
                           (trade_date, source, board_ts_code, board_name, fetched_at)
                           VALUES (?, ?, ?, ?, now())""",
                        [trade_date, source, b["board_ts_code"], b["board_name"]],
                    )
                    # UPSERT members
                    for m in members:
                        con.execute(
                            """INSERT OR REPLACE INTO ods_plate_member
                               (trade_date, source, board_ts_code, con_code, con_name, fetched_at)
                               VALUES (?, ?, ?, ?, ?, now())""",
                            [trade_date, source, b["board_ts_code"],
                             m["con_code"], m["con_name"]],
                        )
                        total_members += 1
                except Exception as e:
                    logger.warning(
                        "fetch.plate: %s member %s failed: %s",
                        source, b["board_ts_code"], e,
                    )
                    continue

                # Progress heartbeat every 100 boards
                if (i + 1) % 100 == 0:
                    logger.info(
                        "progress fetch.plate: %s %d/%d boards | members=%d",
                        source, i + 1, len(boards), total_members,
                    )

            result["n_members"] = total_members

            # Step C: write snapshot meta record
            con.execute(
                """INSERT OR REPLACE INTO ods_plate_snapshot
                   (trade_date, source, idx_type, n_boards, n_members, fetched_at)
                   VALUES (?, ?, ?, ?, ?, now())""",
                [trade_date, source, idx_type, len(boards), total_members],
            )

            elapsed = time.monotonic() - t_start
            logger.info(
                "progress fetch.plate: %s %s done | boards=%d members=%d | %.0fs",
                source, idx_type, len(boards), total_members, elapsed,
            )

        except Exception as e:
            result["error"] = str(e)
            logger.warning(
                "fetch.plate: %s %s degraded: %s",
                source, idx_type, e,
            )

        results[source] = result

    return results
```

- [ ] **Step 3: 实现导出时查询函数 `load_plate_enrichment()`**

```python
def load_plate_enrichment(con, trade_date: str) -> dict[str, dict[str, str]]:
    """Load plate enrichment for export.

    Returns dict[ts_code -> {'tdx_industry_board': '...', 'dc_concept_board': '...'}].

    If no plate data exists for trade_date, returns empty dicts for all stocks.
    """
    enrichment = {}

    # TDX industry plates → tdx_industry_board column
    tdx_rows = con.execute(
        """SELECT con_code AS ts_code,
                  STRING_AGG(DISTINCT board_name, ',' ORDER BY board_name) AS boards
           FROM ods_plate_member
           WHERE trade_date = ? AND source = 'tdx'
           GROUP BY con_code""",
        [trade_date],
    ).fetchall()
    for ts_code, boards in tdx_rows:
        enrichment.setdefault(ts_code, {})["tdx_industry_board"] = boards

    # DC concept plates → dc_concept_board column
    dc_rows = con.execute(
        """SELECT con_code AS ts_code,
                  STRING_AGG(DISTINCT board_name, ',' ORDER BY board_name) AS boards
           FROM ods_plate_member
           WHERE trade_date = ? AND source = 'dc'
           GROUP BY con_code""",
        [trade_date],
    ).fetchall()
    for ts_code, boards in dc_rows:
        enrichment.setdefault(ts_code, {})["dc_concept_board"] = boards

    return enrichment
```

- [ ] **Step 4: Commit**

```bash
git add backend/fetch/ods_plate.py
git commit -m "feat: add ods_plate fetch module with TDX+DC plate data and TTL caching

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 板块拉取模块测试

**Files:**
- Create: `tests/test_fetch/test_ods_plate.py`

- [ ] **Step 1: 编写测试文件**

```python
"""Tests for ods_plate fetch module — TTL logic, enrichment query, snapshot freshness."""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta


class TestSnapshotFreshness:
    """_is_snapshot_fresh TTL gate tests."""

    def test_fresh_within_ttl(self):
        """Snapshot fetched today → fresh."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.execute.return_value.fetchone.return_value = [now_str]

        assert _is_snapshot_fresh(con, "20260620", "tdx", "行业板块") is True

    def test_expired_beyond_ttl(self):
        """Snapshot fetched 10 days ago → stale."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        stale = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute.return_value.fetchone.return_value = [stale]

        assert _is_snapshot_fresh(con, "20260620", "tdx", "行业板块") is False

    def test_no_snapshot_exists(self):
        """No snapshot row → stale."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        con.execute.return_value.fetchone.return_value = None

        assert _is_snapshot_fresh(con, "20260620", "dc", "概念板块") is False

    def test_each_source_independent(self):
        """TDX fresh + DC stale → independent TTL."""
        from backend.fetch.ods_plate import _is_snapshot_fresh

        con = MagicMock()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stale = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

        # First call → fresh, second call → stale
        con.execute.return_value.fetchone.side_effect = [[now_str], [stale]]

        assert _is_snapshot_fresh(con, "20260620", "tdx", "行业板块") is True
        assert _is_snapshot_fresh(con, "20260620", "dc", "概念板块") is False


class TestLoadPlateEnrichment:
    """load_plate_enrichment export query tests."""

    def test_enrichment_empty_when_no_data(self):
        """No plate data → empty dict."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        con.execute.return_value.fetchall.return_value = []

        result = load_plate_enrichment(con, "20260620")
        assert result == {}

    def test_enrichment_single_stock_multi_board(self):
        """Stock in 2 boards → comma-separated."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        # First call: TDX, second call: DC
        con.execute.return_value.fetchall.side_effect = [
            [("000001.SZ", "银行,金融")],   # TDX
            [("000001.SZ", "央企改革,深证100")],  # DC
        ]

        result = load_plate_enrichment(con, "20260620")
        assert result["000001.SZ"]["tdx_industry_board"] == "银行,金融"
        assert result["000001.SZ"]["dc_concept_board"] == "央企改革,深证100"

    def test_enrichment_stock_no_dc_concept(self):
        """BSE stock → TDX board but no DC concept."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        con.execute.return_value.fetchall.side_effect = [
            [("830001.BJ", "制造业")],  # TDX
            [],                         # DC: no data for BSE stocks
        ]

        result = load_plate_enrichment(con, "20260620")
        assert result["830001.BJ"]["tdx_industry_board"] == "制造业"
        assert "dc_concept_board" not in result["830001.BJ"]

    def test_enrichment_multiple_stocks(self):
        """Multiple stocks with different board counts."""
        from backend.fetch.ods_plate import load_plate_enrichment

        con = MagicMock()
        con.execute.return_value.fetchall.side_effect = [
            [("000001.SZ", "银行"), ("000002.SZ", "房地产")],  # TDX
            [("000001.SZ", "央企改革,沪深300"), ("000002.SZ", "物业管理")],  # DC
        ]

        result = load_plate_enrichment(con, "20260620")
        assert len(result) == 2
        assert result["000001.SZ"]["tdx_industry_board"] == "银行"
        assert result["000002.SZ"]["dc_concept_board"] == "物业管理"
```

- [ ] **Step 2: 运行测试验证**

```bash
pytest tests/test_fetch/test_ods_plate.py -v
```

预期：5 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_fetch/test_ods_plate.py
git commit -m "test: add ods_plate TTL + enrichment query unit tests

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 清理废弃的 concept 管线 — orchestrator + build_dim

**Files:**
- Modify: `backend/etl/orchestrator.py`（行 134-141, 149-165）
- Modify: `backend/etl/build_dim.py`（行 69-87）

- [ ] **Step 1: 删除 `build_dim_concept()` 函数**

在 `backend/etl/build_dim.py` 中，删除第 69-87 行的 `build_dim_concept()` 函数（整个函数体）。文件末尾只保留 `build_dim_stock()` 和 `build_dim_date()`。

- [ ] **Step 2: 从 orchestrator 中移除 concept fetch 步骤**

在 `backend/etl/orchestrator.py` 中，删除第 134-141 行（整个 `fetch_concept_detail` 代码块）：

```python
            # 删除以下 block:
            # Concept detail LAST — per-stock calls, low priority, skip on failure
            lid, t0 = log_etl_start(con, "fetch_concept_detail")
            try:
                n = fetch_concept_detail(client, con, ts_codes=codes)
                log_etl_end(con, lid, "fetch_concept_detail", t0, "success", row_count=n)
            except Exception as e:
                log_etl_end(con, lid, "fetch_concept_detail", t0, "degraded",
                            error_msg=f"skipped (rate limited): {e}")
```

替换为板块拉取调用（放在同一"低优先级、失败降级"位置）：

```python
            # Plate (board/concept) data — low priority, skip on failure
            from backend.fetch.ods_plate import fetch_plate_data

            lid, t0 = log_etl_start(con, "fetch_plate_data")
            try:
                plate_results = fetch_plate_data(client, con, end, ts_codes=codes)
                total_members = sum(r.get("n_members", 0) for r in plate_results.values())
                cached = all(r.get("cached") for r in plate_results.values())
                log_etl_end(con, lid, "fetch_plate_data", t0, "success",
                            row_count=total_members,
                            extra=str(dict((k, {
                                "boards": v["n_boards"], "members": v["n_members"],
                                "cached": v["cached"],
                            }) for k, v in plate_results.items())))
            except Exception as e:
                log_etl_end(con, lid, "fetch_plate_data", t0, "degraded",
                            error_msg=f"skipped (rate limited): {e}")
```

- [ ] **Step 3: 从 orchestrator 中移除 `build_dim_concept` 步骤**

在 `orchestrator.py` 中，将 build-dim 步骤的循环从：

```python
            for dim_step, fn in [
                ("build_dim_stock", build_dim_stock),
                ("build_dim_date", build_dim_date),
                ("build_dim_concept", build_dim_concept),
            ]:
```

改为：

```python
            for dim_step, fn in [
                ("build_dim_stock", build_dim_stock),
                ("build_dim_date", build_dim_date),
            ]:
```

同时删除循环体内的 `if dim_step == "build_dim_concept":` 分支（原来第 157-158 行特殊处理 concept count 的逻辑）。

- [ ] **Step 4: 移除 orchestrator 顶部旧的 concept import**

找到文件顶部 `from backend.fetch.ods_concept import fetch_concept_detail` 并删除。

- [ ] **Step 5: 运行现有测试确认无回归**

```bash
pytest tests/test_etl/ -v --timeout=60
```

预期：所有已有测试通过（无 concept 管线测试依赖）。

- [ ] **Step 6: Commit**

```bash
git add backend/etl/orchestrator.py backend/etl/build_dim.py
git commit -m "refactor: replace deprecated concept API with plate fetch in pipeline

- Remove build_dim_concept() from build_dim.py
- Remove fetch_concept_detail step from orchestrator
- Add fetch_plate_data call (TDX+DC) as low-priority degraded step
- Clean up unused concept imports

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 导出集成——新增行业板块/概念板块列

**Files:**
- Modify: `backend/export_wide.py`（`_COL_NAMES`, `_ID_COLS`, `export_wide_to_excel()`）

- [ ] **Step 1: 更新列名映射 `_COL_NAMES`**

在 `_COL_NAMES` 字典中修改 sector 中文名，并新增两个键：

```python
_COL_NAMES = {
    # ... 其他不变 ...
    "sector": "上市板块",          # was "板块" — 改为更准确地描述交易所板块
    "industry": "行业",            # 不变
    "tdx_industry_board": "行业板块",   # 新增：TDX 行业板块
    "dc_concept_board": "概念板块",     # 新增：DC 概念板块
    # ... 其余不变 ...
}
```

- [ ] **Step 2: 更新 `_ID_COLS` 列表**

在 `_ID_COLS` 中，在 `industry` 之后、`is_st` 之前插入两列：

```python
_ID_COLS = [
    "ts_code", "trade_date", "stock_code", "stock_name",
    "exchange", "sector", "industry", "tdx_industry_board", "dc_concept_board", "is_st",
]
```

- [ ] **Step 3: 在 `export_wide_to_excel()` 中注入板块数据**

在 daily 数据 `_format_numbers()` + `enrich_tradable_columns()` 之后（约第 304 行 `log_tradable_enrich_progress(daily_enrich)` 之后），插入板块 enrichment 合并逻辑：

```python
    # ---- Enrich with plate/concept data ----
    from backend.fetch.ods_plate import load_plate_enrichment

    t_plate = time.monotonic()
    logger.info("progress export: loading plate enrichment | date=%s", trade_date)
    plate_enrichment = load_plate_enrichment(con, trade_date)
    if plate_enrichment:
        # Build enrichment DataFrame from the dict
        plate_df_data = []
        for ts_code, cols in plate_enrichment.items():
            plate_df_data.append({
                "ts_code": ts_code,
                "tdx_industry_board": cols.get("tdx_industry_board"),
                "dc_concept_board": cols.get("dc_concept_board"),
            })
        plate_df = pd.DataFrame(plate_df_data)
        daily = daily.merge(plate_df, on="ts_code", how="left")
        logger.info(
            "progress export: plate enrichment done | enriched=%d rows | %.0fs",
            len(plate_df), time.monotonic() - t_plate,
        )
    else:
        logger.info("progress export: no plate data for %s | %.0fs",
                    trade_date, time.monotonic() - t_plate)
```

- [ ] **Step 4: 处理空值——`N/A` 填充**

在 `_format_numbers()` 调用后，对新增两列的空值进行填充。在 enrichment merge 之后插入：

```python
    # Fill missing plate/concept values with "N/A"
    for col in ["tdx_industry_board", "dc_concept_board"]:
        if col in daily.columns:
            daily[col] = daily[col].fillna("N/A")
```

- [ ] **Step 5: 更新周线 identity 列 drop 列表**

周线 sheet 不再需要删除新增的两列（因为它们应该出现在两个 sheet 中）。确认 `id_cols_drop`（约第 335-338 行）不包含 `tdx_industry_board` 和 `dc_concept_board`。如果需要，在 daily → weekly merge 中确保新列被正确传递。

在 `id_cols_drop` 中**不添加** `tdx_industry_board` 和 `dc_concept_board`——它们作为股票身份列，应该在周线 sheet 中保留。

- [ ] **Step 6: 更新 `_SIGNAL_COLS` 集合**

新增两列是板块/概念分类，不属于信号列。确认 `_SIGNAL_COLS` / `_EVENT_SIGNAL_COLS` / `_STATE_METRIC_COLS` 集合**不包含** `tdx_industry_board` 和 `dc_concept_board`，这样它们的空值不会被错误地展示为 `-`（事件阴性）而是展示为 `N/A`。

查看 `_N/A_FILL_COLS` 或类似逻辑——需要确保这两列的空值处理走 `N/A` 路径而非 `-` 路径。

让我先检查现有的空值处理逻辑：

读取 export_wide.py 中 `_format_numbers()` 和空值填充相关代码。

- [ ] **Step 7: Commit**

```bash
git add backend/export_wide.py
git commit -m "feat: add 行业板块/概念板块 columns to export, rename sector→上市板块

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 导出列注释更新

**Files:**
- Modify: `docs/export/export-column-comments.yaml`
- Modify: `backend/export_column_comments.py`（如需要）

- [ ] **Step 1: 更新 `sector` 列注释**

找到 `sector` 条目，将注释从"板块分类（概念/行业聚合展示字段）"改为准确描述：

```yaml
sector:
  comment: "上市板块（主板/创业板/科创板/北交所），基于股票代码前缀推导。"
```

- [ ] **Step 2: 新增两列的注释**

在 `sector` 条目之后插入：

```yaml
tdx_industry_board:
  comment: "TDX通达信行业板块分类（如银行、汽车制造、半导体等），以分析日为锚点，多板块以逗号分隔。数据源：tushare tdx_index/tdx_member。"
dc_concept_board:
  comment: "DC东方财富概念板块分类（如新能源、人工智能、国企改革等），以分析日为锚点，多概念以逗号分隔。数据源：tushare dc_index/dc_member。"
```

- [ ] **Step 3: 确认 `export_column_comments.py` 无需修改**

`backend/export_column_comments.py` 从 YAML 动态加载注释，新增键会自动生效。只需确认没有硬编码的注释白名单。快速检查：

```bash
grep -n "sector\|industry" backend/export_column_comments.py
```

- [ ] **Step 4: Commit**

```bash
git add docs/export/export-column-comments.yaml
git commit -m "docs: update export column comments for plate/concept columns

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: 导出列集成测试

**Files:**
- Create: `tests/test_export/test_export_plate_columns.py`

- [ ] **Step 1: 编写集成测试**

```python
"""Integration tests for plate/concept columns in export."""
import pytest
import duckdb
import pandas as pd
from unittest.mock import MagicMock, patch


class TestPlateColumnsInExport:
    """Verify plate columns appear correctly in exported DataFrame."""

    def test_id_cols_include_plate_columns(self):
        """_ID_COLS must include tdx_industry_board and dc_concept_board."""
        from backend.export_wide import _ID_COLS

        assert "tdx_industry_board" in _ID_COLS
        assert "dc_concept_board" in _ID_COLS

        # Position check: after industry, before is_st
        idx_industry = _ID_COLS.index("industry")
        idx_tdx = _ID_COLS.index("tdx_industry_board")
        idx_dc = _ID_COLS.index("dc_concept_board")
        idx_st = _ID_COLS.index("is_st")

        assert idx_tdx == idx_industry + 1, "tdx_industry_board must follow industry"
        assert idx_dc == idx_tdx + 1, "dc_concept_board must follow tdx_industry_board"
        assert idx_st == idx_dc + 1, "is_st must follow dc_concept_board"

    def test_col_names_include_new_columns(self):
        """_COL_NAMES must map new columns to Chinese names."""
        from backend.export_wide import _COL_NAMES

        assert _COL_NAMES["sector"] == "上市板块"
        assert _COL_NAMES["tdx_industry_board"] == "行业板块"
        assert _COL_NAMES["dc_concept_board"] == "概念板块"

    def test_plate_merge_with_existing_daily(self):
        """Plate enrichment merges correctly into daily DataFrame."""
        import pandas as pd
        from backend.fetch.ods_plate import load_plate_enrichment

        # Simulate daily DataFrame with 3 stocks
        daily = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ", "830001.BJ"],
            "trade_date": ["20260620"] * 3,
            "close": [10.5, 20.3, 5.0],
        })

        # Simulate enrichment
        enrichment = {
            "000001.SZ": {"tdx_industry_board": "银行", "dc_concept_board": "央企改革,沪深300"},
            "000002.SZ": {"tdx_industry_board": "房地产", "dc_concept_board": "物业管理"},
            # 830001.BJ: no DC concept
        }

        plate_data = []
        for ts_code, cols in enrichment.items():
            plate_data.append({
                "ts_code": ts_code,
                "tdx_industry_board": cols.get("tdx_industry_board"),
                "dc_concept_board": cols.get("dc_concept_board"),
            })
        plate_df = pd.DataFrame(plate_data)
        merged = daily.merge(plate_df, on="ts_code", how="left")

        # Fill N/A
        for col in ["tdx_industry_board", "dc_concept_board"]:
            if col in merged.columns:
                merged[col] = merged[col].fillna("N/A")

        assert merged.loc[0, "tdx_industry_board"] == "银行"
        assert merged.loc[0, "dc_concept_board"] == "央企改革,沪深300"
        assert merged.loc[2, "dc_concept_board"] == "N/A"

    def test_plate_not_in_signal_cols(self):
        """Plate columns must NOT be in signal/event/state metric sets."""
        from backend.export_wide import (
            _EVENT_SIGNAL_COLS, _STATE_METRIC_COLS, _SIGNAL_COLS
        )

        for col in ["tdx_industry_board", "dc_concept_board"]:
            assert col not in _EVENT_SIGNAL_COLS, f"{col} must not be event signal"
            assert col not in _STATE_METRIC_COLS, f"{col} must not be state metric"
            assert col not in _SIGNAL_COLS, f"{col} must not be in signal set"
```

- [ ] **Step 2: 运行测试验证**

```bash
pytest tests/test_export/test_export_plate_columns.py -v
```

预期：4 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_export/test_export_plate_columns.py
git commit -m "test: add export plate column integration tests

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: 空值处理——确保板块列展示为 `N/A`

**Files:**
- Modify: `backend/export_wide.py`（`apply_display_nulls()` 附近，约第 433-448 行）

**背景：** 现有机理已探明——

- `_format_numbers()`（第 416 行）：仅做数值四舍五入和单位换算，**不处理 null**
- `apply_display_nulls()`（第 433 行）：null → 展示值转换。`_EVENT_SIGNAL_COLS` → `"-"`；`_STATE_METRIC_COLS | _FUNDAMENTAL_NA_COLS` → `"N/A"`
- `apply_display_nulls()` 在 `_translate_df()`（第 453 行）中被调用，位于 weekly merge 之后
- `tdx_industry_board` 和 `dc_concept_board` **不在** `_EVENT_SIGNAL_COLS` / `_STATE_METRIC_COLS` / `_FUNDAMENTAL_NA_COLS` 中，所以 `apply_display_nulls` **不会改写它们**

**结论：** Task 5 Step 4 中显式 `fillna("N/A")` 在 enrichment merge 之后、weekly merge 之前执行。`apply_display_nulls` 后执行时不会覆盖已有的 `"N/A"` 字符串（`fillna` 只替换 NaN）。方案已验证可行。

- [ ] **Step 1: 新增 `_PLATE_CLASSIFICATION_COLS` 常量作为文档锚点**

在 `_FUNDAMENTAL_NA_COLS` 定义（约第 233 行）之后，添加一个仅作文档用途的常量，明确标记板块/概念列的分类列身份：

```python
# Classification columns — stock attributes not derived from signals; null → "N/A"
_PLATE_CLASSIFICATION_COLS = {"tdx_industry_board", "dc_concept_board"}
```

- [ ] **Step 2: 编写空值展示的针对性测试**

在 `tests/test_export/test_export_plate_columns.py` 中追加：

```python
class TestPlateNullDisplay:
    """Verify plate columns display N/A (not '-') for missing data."""

    def test_plate_cols_not_in_event_signal(self):
        """Plate columns must NOT be classified as event signals."""
        from backend.export_wide import _EVENT_SIGNAL_COLS
        assert "tdx_industry_board" not in _EVENT_SIGNAL_COLS
        assert "dc_concept_board" not in _EVENT_SIGNAL_COLS

    def test_plate_cols_not_in_state_metric(self):
        """Plate columns must NOT be classified as state metrics (they're attributes)."""
        from backend.export_wide import _STATE_METRIC_COLS
        assert "tdx_industry_board" not in _STATE_METRIC_COLS
        assert "dc_concept_board" not in _STATE_METRIC_COLS

    def test_apply_display_nulls_leaves_na_untouched(self):
        """apply_display_nulls must not replace existing 'N/A' strings."""
        import pandas as pd
        from backend.export_wide import apply_display_nulls

        df = pd.DataFrame({
            "tdx_industry_board": ["银行", "N/A", pd.NA],
            "dc_concept_board": ["新能源", pd.NA, "N/A"],
        })
        result = apply_display_nulls(df)
        # Existing "N/A" preserved; pd.NA remains as NaN (to be filled by explicit fillna)
        assert result.loc[1, "tdx_industry_board"] == "N/A"
        assert result.loc[2, "dc_concept_board"] == "N/A"
        # pd.NA is NOT filled by apply_display_nulls (not in any signal set)
        assert pd.isna(result.loc[2, "tdx_industry_board"])
        assert pd.isna(result.loc[1, "dc_concept_board"])

    def test_plate_na_filled_before_apply_display_nulls(self):
        """End-to-end: fillna before apply_display_nulls → N/A preserved."""
        import pandas as pd
        from backend.export_wide import apply_display_nulls

        df = pd.DataFrame({
            "close": [10.5, 20.3],
            "tdx_industry_board": [None, "银行"],
            "dc_concept_board": ["新能源", None],
        })
        # Simulate fillna before apply_display_nulls (as done in export_wide_to_excel)
        for col in ["tdx_industry_board", "dc_concept_board"]:
            if col in df.columns:
                df[col] = df[col].fillna("N/A")

        result = apply_display_nulls(df)
        assert result.loc[0, "tdx_industry_board"] == "N/A"
        assert result.loc[1, "dc_concept_board"] == "N/A"
```

- [ ] **Step 3: 运行测试确认**

```bash
pytest tests/test_export/test_export_plate_columns.py -v
```

预期：8 passed（Task 7 的 4 个 + 本 Task 的 4 个）

- [ ] **Step 4: Commit**

```bash
git add backend/export_wide.py tests/test_export/test_export_plate_columns.py
git commit -m "fix: ensure plate columns show N/A for missing data via explicit fillna

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: 文档更新

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 更新 CLAUDE.md**

需要更新的章节：

1. **项目结构** — 新增 `backend/fetch/ods_plate.py` 条目：

在 `backend/fetch/` 列表中添加：
```
    ods_plate.py          # TDX+DC 板块/概念拉取（TTL 缓存）
```

2. **各指标说明** — 新增"板块/概念数据"小节：

```markdown
#### 板块/概念数据

- **板块数据源：**
  - TDX 行业板块：`tdx_index` → `tdx_member`（约 60-80 个行业板块）
  - DC 概念板块：`dc_index` → `dc_member`（约 400-500 个概念板块）
- **缓存策略：** 7 天 TTL，`ods_plate_snapshot` 记录 fetch 时间
- **导出列：** `tdx_industry_board`（行业板块）、`dc_concept_board`（概念板块），逗号分隔，空值显示 `N/A`
- **时间锚点：** 以分析日 `trade_date` 为准，通过 `ods_plate_member` 查询
- **管道位置：** `cli run` fetch 末尾低优先级步骤，失败降级不阻断
- **旧 concept 管线：** `ods_concept_detail` + `dim_concept` + `dim_concept_stock` 已废弃，DDL 保留但不再写入
```

3. **常见命令** — 无新增命令（板块拉取集成在 `cli run` 内）。

4. **数据流** — 在 ODS 层补充板块表：
```
tushare API → ODS(7表 + 3 板块表) → ...
```

5. **导出语义** — 在已有说明中补充板块/概念空值规则。

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for plate/concept feature

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: 端到端烟雾测试

- [ ] **Step 1: 运行板块拉取（dry-run 风格验证）**

```bash
python -c "
from backend.db.connection import get_connection
from backend.fetch.client import TushareClient
from backend.fetch.ods_plate import fetch_plate_data

con = get_connection(read_only=False)
client = TushareClient()

# Fetch for a recent date — should hit cache on second run
results = fetch_plate_data(client, con, '20260620')
for source, r in results.items():
    print(f'{source}: boards={r[\"n_boards\"]} members={r[\"n_members\"]} cached={r[\"cached\"]} error={r[\"error\"]}')

# Second run — should be cached
results2 = fetch_plate_data(client, con, '20260620')
for source, r in results2.items():
    print(f'{source}: boards={r[\"n_boards\"]} members={r[\"n_members\"]} cached={r[\"cached\"]} error={r[\"error\"]}')

con.close()
"
```

预期：
- 第一次：cached=False，拉取到真实板块数据
- 第二次：cached=True，秒级返回

- [ ] **Step 2: 运行导出验证列存在**

```bash
python -m backend.cli export --date 20260620 --ts-code 000001.SZ
```

检查输出的 Excel 文件中"综合分析"sheet：
- 表头行是否包含"行业板块""概念板块"
- 000001.SZ 行是否有板块/概念数据
- `sector` 列中文名是否为"上市板块"

- [ ] **Step 3: 运行现有全量测试确认无回归**

```bash
pytest tests/ -v --timeout=120
```

- [ ] **Step 4: Commit（如有修改）**

```bash
git add .
git commit -m "test: end-to-end smoke test for plate/concept export

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 验收清单

每个实施阶段完成后，邀请**数据架构师**和**交易专家**进行验收：

### Phase 1 验收（Task 1-4：数据层）
- [ ] ODS 三张表 DDL 正确，`source` 列区分 TDX/DC
- [ ] 拉取模块 TTL 逻辑正确（7 天缓存）
- [ ] 废弃 concept 管线已彻底移除，旧测试无回归
- [ ] `fetch_plate_data()` 失败降级不抛异常

### Phase 2 验收（Task 5-8：导出层）
- [ ] `sector` 列中文名改为"上市板块"
- [ ] "行业板块""概念板块"列在 `_ID_COLS` 中位置正确（industry 之后）
- [ ] 多板块逗号分隔正确
- [ ] 空值展示为 `N/A` 而非 `-`
- [ ] 表头注释 YAML 已更新

### Phase 3 验收（Task 9-10：文档 + 端到端）
- [ ] CLAUDE.md 完整记录新功能
- [ ] 端到端导出文件包含板块/概念列
- [ ] 第二次拉取命中缓存（cached=True）
- [ ] 全量 pytest 通过
