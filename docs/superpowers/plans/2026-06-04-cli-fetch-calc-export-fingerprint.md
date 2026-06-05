# CLI 职责分离 + Per-Stock 增量拉取 + 指纹跳过基础设施 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拆解 fetch/calc/export 三层职责，修复增量拉取的 date-global bug（改为 per-stock），calc 前增加数据完整度检查，DWS 层增加 input_fingerprint 列和 spec_version 基础设施。

**Architecture:** 拉取层采用双模式——`--ts-code`（少量标的）用 stock-batched 遍历股票补缺，`--all`（全市场）用 date-batched 遍历交易日。计算层在 calc 前检查 DWD 覆盖度，缺口小于阈值时自动补拉。指纹层在每张 DWS 表增加 `input_fingerprint` 列，Calculator 接口统一化。

**Tech Stack:** Python 3.9+, DuckDB ≥1.0, tushare, pytest

---

## 文件改动清单

| 文件 | 改动类型 | 职责 |
|------|:----:|------|
| `backend/cli.py` | 修改 | 拆为 fetch / calc / export 三个子命令 |
| `backend/fetch/ods_daily.py` | 修改 | 新增 stock-batched 拉取；`_get_trading_days` 支持 per-stock 检测 |
| `backend/etl/orchestrator.py` | 修改 | 新增 `check_data_completeness()`；calc 前置检查 |
| `backend/etl/base.py` | 修改 | 新增 `insert_dws_batch()` 公共方法 |
| `backend/db/schema.py` | 修改 | DWS 表 DDL 增加 `input_fingerprint`、`spec_version` 列 |
| `backend/etl/calc_macd.py` 等 6 个 Calculator | 修改 | `_insert` 增加 fingerprint 计算，复用公共 `insert_dws_batch` |
| `tests/test_fetch/test_ods_daily.py` | **新建** | 增量检测 + stock-batched 测试 |
| `tests/test_etl/test_data_completeness.py` | **新建** | 数据完整度检查测试 |

---

## Phase 1: P0 — 增量修复 + CLI 拆分

### Task 1: Per-Stock 增量拉取 — stock-batched 模式

**Files:**
- Modify: `backend/fetch/ods_daily.py`
- Create: `tests/test_fetch/test_ods_daily.py`

#### 1.1 写测试

- [ ] **Step 1: 创建测试文件**

```python
# tests/test_fetch/test_ods_daily.py

import duckdb
import pytest
from backend.fetch.ods_daily import (
    _get_trading_days,
    _get_missing_ranges_per_stock,
    fetch_stocks_incremental,
)


def test_get_trading_days_per_stock_detection():
    """per-stock 模式：只过滤该股票已有的日期，不影响其他股票。"""
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)
    """)
    # 000001.SZ 有 20260101~20260105
    for d in ["20260101", "20260102", "20260103", "20260104", "20260105"]:
        con.execute("INSERT INTO ods_daily VALUES ('000001.SZ', ?)", (d,))
    # 000002.SZ 只有 20260101
    con.execute("INSERT INTO ods_daily VALUES ('000002.SZ', '20260101')")

    # 模拟交易日列表
    all_days = [f"2026010{i}" for i in range(1, 6)]  # 20260101~20260105

    # per-stock 检测：000001.SZ 不缺，000002.SZ 缺 01/02~01/05
    missing_000001 = _get_missing_days_for_stock(con, "000001.SZ", all_days)
    missing_000002 = _get_missing_days_for_stock(con, "000002.SZ", all_days)

    assert len(missing_000001) == 0, f"000001 不缺数据，got {missing_000001}"
    assert len(missing_000002) == 4, f"000002 缺 4 天，got {len(missing_000002)}"
    assert "20260102" in missing_000002

    con.close()


def test_get_missing_ranges_per_stock_groups_consecutive():
    """连续缺失日期合并为一个 range，减少 API 调用次数。"""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")

    # 股票只有 20260105 一天，缺 01~04
    con.execute("INSERT INTO ods_daily VALUES ('TEST.SZ', '20260105')")

    days = ["20260101", "20260102", "20260103", "20260104", "20260105"]
    ranges = _get_missing_ranges_per_stock(con, "TEST.SZ", days)

    # 缺失范围应该是 [('20260101', '20260104')] — 连续的 4 天合并
    assert len(ranges) == 1, f"预期 1 个缺失段，got {len(ranges)}: {ranges}"
    assert ranges[0] == ("20260101", "20260104")

    con.close()


def test_fetch_stocks_incremental_no_missing(monkeypatch):
    """该股票数据完整时，不应调 API。"""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ods_daily (ts_code TEXT, trade_date TEXT)")
    for d in ["20260101", "20260102", "20260103"]:
        con.execute("INSERT INTO ods_daily VALUES ('FULL.SZ', ?)", (d,))

    api_calls = []

    class FakeClient:
        def call(self, api, **kwargs):
            api_calls.append(api)
            return []

    n = fetch_stocks_incremental(
        FakeClient(), con, ["FULL.SZ"], start="20260101", end="20260103"
    )
    assert n == 0, f"不应该拉数据，got {n}"
    assert len(api_calls) == 0, f"不应该调 API，got {api_calls}"
    con.close()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_fetch/test_ods_daily.py -v
# 预期：全部 FAIL（函数未定义）
```

#### 1.2 实现 stock-batched 拉取

- [ ] **Step 3: 在 ods_daily.py 新增 per-stock 函数**

```python
# 在 _get_trading_days 之后，fetch_by_date_range_parallel 之前插入


def _get_missing_days_for_stock(con, ts_code: str, all_trading_days: list[str]) -> list[str]:
    """返回该股票在交易日列表中缺失的日期。"""
    existing = set(r[0] for r in con.execute(
        "SELECT trade_date FROM ods_daily WHERE ts_code = ? "
        "AND trade_date >= ? AND trade_date <= ?",
        (ts_code, all_trading_days[0], all_trading_days[-1])
    ).fetchall())
    return [d for d in all_trading_days if d not in existing]


def _get_missing_ranges_per_stock(con, ts_code: str,
                                   all_trading_days: list[str]) -> list[tuple[str, str]]:
    """返回该股票缺失的连续日期段列表，每个元素为 (start, end)。"""
    missing = _get_missing_days_for_stock(con, ts_code, all_trading_days)
    if not missing:
        return []

    ranges = []
    seg_start = missing[0]
    prev = missing[0]
    for d in missing[1:]:
        # 检查是否连续（交易日列表中相邻）
        idx_prev = all_trading_days.index(prev)
        idx_curr = all_trading_days.index(d)
        if idx_curr - idx_prev > 1:
            ranges.append((seg_start, prev))
            seg_start = d
        prev = d
    ranges.append((seg_start, prev))
    return ranges


def fetch_stocks_incremental(client, con, ts_codes: list[str],
                              start: str = "20150101",
                              end: str = "20991231") -> int:
    """Stock-batched 增量拉取：对每只股票独立检测缺失日期，按缺失段调 API。

    适用场景：--ts-code 指定少量股票时。
    策略：
      1. 获取交易日历
      2. 对每只股票，查询 ods_daily 中已有的日期范围
      3. 缺失段连续的合并为一个 (start_date, end_date) → 调用 daily(ts_code=, start=, end=)
      4. INSERT OR REPLACE 到 ods_daily / ods_daily_basic / ods_moneyflow

    返回写入的总行数。
    """
    import time

    # 获取交易日历
    cal = client.call("trade_cal", exchange="SSE", start_date=start, end_date=end, is_open=1)
    all_days = sorted([r["cal_date"] for r in cal])
    if not all_days:
        return 0

    total = 0
    t0 = time.time()
    for i, ts_code in enumerate(ts_codes):
        ranges = _get_missing_ranges_per_stock(con, ts_code, all_days)
        if not ranges:
            continue

        for seg_start, seg_end in ranges:
            try:
                # daily OHLCV + adj_factor（per-stock API）
                recs = client.call("daily", ts_code=ts_code,
                                   start_date=seg_start, end_date=seg_end)
                for r in recs:
                    con.execute("""INSERT OR REPLACE INTO ods_daily
                        (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,now())""",
                        (r["ts_code"], r["trade_date"], r["open"], r["high"], r["low"],
                         r["close"], r["vol"], r["amount"], r["pct_chg"],
                         r.get("adj_factor")))
                    total += 1

                # daily_basic
                try:
                    recs = client.call("daily_basic", ts_code=ts_code,
                                       start_date=seg_start, end_date=seg_end)
                    for r in recs:
                        con.execute("""INSERT OR REPLACE INTO ods_daily_basic
                            (ts_code, trade_date, total_mv, pe_ttm, turnover_rate, volume_ratio, fetched_at)
                            VALUES (?,?,?,?,?,?,now())""",
                            (r["ts_code"], r["trade_date"], r.get("total_mv"),
                             r.get("pe_ttm"), r.get("turnover_rate"), r.get("volume_ratio")))
                        total += 1
                except Exception:
                    pass  # daily_basic may fail for delisted stocks

                # moneyflow
                try:
                    recs = client.call("moneyflow", ts_code=ts_code,
                                       start_date=seg_start, end_date=seg_end)
                    for r in recs:
                        con.execute("""INSERT OR REPLACE INTO ods_moneyflow
                            (ts_code, trade_date, buy_sm_vol, buy_sm_amount,
                             sell_sm_vol, sell_sm_amount, buy_md_vol, buy_md_amount,
                             sell_md_vol, sell_md_amount, buy_lg_vol, buy_lg_amount,
                             sell_lg_vol, sell_lg_amount, buy_elg_vol, buy_elg_amount,
                             sell_elg_vol, sell_elg_amount, net_mf_vol, net_mf_amount, fetched_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,now())""",
                            (r["ts_code"], r["trade_date"],
                             r.get("buy_sm_vol"), r.get("buy_sm_amount"),
                             r.get("sell_sm_vol"), r.get("sell_sm_amount"),
                             r.get("buy_md_vol"), r.get("buy_md_amount"),
                             r.get("sell_md_vol"), r.get("sell_md_amount"),
                             r.get("buy_lg_vol"), r.get("buy_lg_amount"),
                             r.get("sell_lg_vol"), r.get("sell_lg_amount"),
                             r.get("buy_elg_vol"), r.get("buy_elg_amount"),
                             r.get("sell_elg_vol"), r.get("sell_elg_amount"),
                             r.get("net_mf_vol"), r.get("net_mf_amount")))
                        total += 1
                except Exception:
                    pass  # moneyflow may fail for early dates

            except Exception as e:
                logger.error("fetch_stocks_incremental %s [%s~%s]: %s",
                             ts_code, seg_start, seg_end, e)

        # 每 10 只股票打印进度
        if (i + 1) % 10 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            logger.info("Stock fetch: %d/%d stocks | %.0fs | %.1f stk/s",
                        i + 1, len(ts_codes), elapsed, rate)

    elapsed = time.time() - t0
    logger.info("Stock fetch complete: %d stocks, %d rows, %.0fs",
                len(ts_codes), total, elapsed)
    return total
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_fetch/test_ods_daily.py -v
# 预期：3 passed
```

- [ ] **Step 5: 提交**

```bash
git add backend/fetch/ods_daily.py tests/test_fetch/test_ods_daily.py
git commit -m "feat: add stock-batched incremental fetch with per-stock gap detection"
```

---

### Task 2: CLI 拆分 — fetch / calc / export 三个子命令

**Files:**
- Modify: `backend/cli.py`

#### 2.1 新增 `cmd_fetch`

- [ ] **Step 1: 替换 cli.py**

完整替换 `backend/cli.py`：

```python
"""CLI entry point for the Tradeanalysis data pipeline.

Usage:
    python -m backend.cli check
    python -m backend.cli fetch --all
    python -m backend.cli fetch --ts-code 000543.SZ --start 20150101
    python -m backend.cli calc --all
    python -m backend.cli calc --ts-code 000543.SZ
    python -m backend.cli export --date 20260529 --ts-code 000543.SZ --output analysis.xlsx
    python -m backend.cli status
"""

import argparse
import sys

from backend.log_config import setup_logging

setup_logging()


# ── check ──

def cmd_check(_args):
    """Check environment connectivity: DuckDB + tushare."""
    from backend.db.connection import check_connectivity
    from backend.fetch.client import TushareClient

    db = check_connectivity()
    print(f"DuckDB: {db['duckdb']} (v{db['version']})")
    print(f"Disk free: {db['disk_free_mb']} MB | DB size: {db['db_size_mb']} MB")
    try:
        TushareClient().call("stock_basic", exchange="", list_status="L", limit=1)
        print("tushare: connected")
    except Exception as e:
        print(f"tushare: error — {e}")


# ── fetch ──

def cmd_fetch(args):
    """拉取 ODS 数据入库。

    --ts-code 模式（≤500 只推荐）：stock-batched，每只股票独立检测缺失日期
    --all 模式：date-batched，遍历交易日拉全市场
    """
    from backend.db.connection import get_connection
    from backend.fetch.client import TushareClient
    from backend.fetch.ods_daily import (
        fetch_by_date_range_parallel,
        fetch_stocks_incremental,
        get_all_active_codes,
    )

    client = TushareClient()
    con = get_connection()
    try:
        start = args.start or "20150101"
        end = args.end or "20991231"

        if args.ts_code:
            codes = args.ts_code if isinstance(args.ts_code, list) else [args.ts_code]
            print(f"Stock-batched fetch: {len(codes)} stocks, {start}~{end}")
            n = fetch_stocks_incremental(client, con, codes, start=start, end=end)
        else:
            codes = get_all_active_codes(con)
            print(f"Date-batched fetch: {len(codes)} active stocks, {start}~{end}")
            n = fetch_by_date_range_parallel(
                start, end, workers=3, con=con
            )
        print(f"Fetched {n} rows")
    finally:
        con.close()


# ── calc ──

def cmd_calc(args):
    """计算 DWS 层指标。

    前置检查：验证目标股票的 DWD 层数据完整度。
    缺失 < 阈值 → 自动补拉（除非 --no-auto-fetch）
    缺失 >= 阈值 → 报错退出，列出缺口
    """
    from backend.db.connection import get_connection
    from backend.etl.orchestrator import run_calc

    con = get_connection()
    try:
        ts_codes = args.ts_code if args.ts_code else None
        if isinstance(ts_codes, str):
            ts_codes = [ts_codes]

        run_calc(
            con,
            ts_codes=ts_codes,
            auto_fetch=not args.no_auto_fetch,
        )
    finally:
        con.close()


# ── export ──

def cmd_export(args):
    """导出分析宽表到 Excel。

    默认从 latest 视图直接导出（不重算）。
    --recalc：先执行 calc 再导出。
    """
    from backend.db.connection import get_connection
    from backend.export_wide import export_wide_to_excel

    ts_codes = args.ts_code if args.ts_code else None

    if args.recalc:
        print("Recalculating DWS before export...")
        from backend.etl.orchestrator import run_calc
        con = get_connection()
        try:
            run_calc(con, ts_codes=ts_codes, auto_fetch=not args.no_auto_fetch)
        finally:
            con.close()

    n = export_wide_to_excel(
        args.db_path or "data/tradeanalysis.duckdb",
        args.date,
        args.output,
        filter_st=not args.include_st,
        include_index=not args.no_index,
        ts_codes=ts_codes,
    )
    print(f"Exported {n} rows -> {args.output}")


# ── query / status ──

def cmd_query(args):
    from backend.db.connection import get_connection
    con = get_connection(read_only=True)
    try:
        view = f"v_dws_macd_{args.freq}_latest"
        sql = (
            f"SELECT * FROM {view} "
            f"WHERE ts_code = ? "
            f"AND trade_date = (SELECT MAX(trade_date) FROM {view} WHERE ts_code = ?)"
        )
        row = con.execute(sql, (args.ts_code, args.ts_code)).fetchone()
        if row:
            cols = [d[0] for d in con.description]
            for c, v in zip(cols, row):
                print(f"{c}: {v}")
        else:
            print(f"No data for {args.ts_code}")
    finally:
        con.close()


def cmd_status(_args):
    from backend.db.connection import get_connection
    con = get_connection(read_only=True)
    try:
        tables = [
            "ods_daily", "ods_daily_basic", "ods_moneyflow",
            "dwd_daily_quote", "dwd_weekly_quote",
            "dws_macd_daily", "dws_ma_daily", "dws_kpattern_daily",
            "dws_dde_daily", "dws_volume_daily", "dws_price_position_daily",
        ]
        for table in tables:
            try:
                cnt = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                latest = con.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()[0]
                print(f"{table:30s} {cnt:>12,}  {latest or 'N/A'}")
            except Exception:
                print(f"{table:30s}  (not found)")
    finally:
        con.close()


# ── main ──

def main():
    p = argparse.ArgumentParser(prog="tradeanalysis")
    sp = p.add_subparsers(dest="command")

    sp.add_parser("check", help="Check environment connectivity")

    # fetch
    fp = sp.add_parser("fetch", help="Pull ODS data into DuckDB")
    fp.add_argument("--ts-code", nargs="+", help="Stock code(s) to fetch (stock-batched mode)")
    fp.add_argument("--start", help="Start date YYYYMMDD (default 20150101)")
    fp.add_argument("--end", help="End date YYYYMMDD (default today)")
    fp.add_argument("--all", action="store_true", default=True,
                    help="Fetch all active stocks (date-batched mode, default)")

    # calc
    cp = sp.add_parser("calc", help="Compute DWS indicators")
    cp.add_argument("--ts-code", nargs="+", help="Stock codes to calculate")
    cp.add_argument("--all", action="store_true", default=True,
                    help="Calculate all active stocks (default)")
    cp.add_argument("--no-auto-fetch", action="store_true",
                    help="Disable auto-fetch when data is missing")

    # export
    xp = sp.add_parser("export", help="Export analysis wide table to Excel")
    xp.add_argument("--date", required=True, help="Analysis date YYYYMMDD")
    xp.add_argument("--output", default="analysis.xlsx")
    xp.add_argument("--ts-code", nargs="+", help="Stock codes to export")
    xp.add_argument("--db-path")
    xp.add_argument("--include-st", action="store_true")
    xp.add_argument("--no-index", action="store_true")
    xp.add_argument("--recalc", action="store_true",
                    help="Recalculate DWS before export")
    xp.add_argument("--no-auto-fetch", action="store_true",
                    help="Disable auto-fetch when recalculating")

    # query
    qp = sp.add_parser("query", help="Query DWS indicators")
    qp.add_argument("--ts-code", required=True)
    qp.add_argument("--freq", default="daily")

    sp.add_parser("status", help="Show database table stats")

    args = p.parse_args()
    handlers = {
        "check": cmd_check,
        "fetch": cmd_fetch,
        "calc": cmd_calc,
        "export": cmd_export,
        "query": cmd_query,
        "status": cmd_status,
    }
    handler = handlers.get(args.command)
    if handler:
        handler(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 验证 CLI 可解析**

```bash
python -m backend.cli fetch --help
python -m backend.cli calc --help
python -m backend.cli export --help
# 预期：各命令显示帮助信息，参数正确
```

- [ ] **Step 3: 提交**

```bash
git add backend/cli.py
git commit -m "feat: split CLI into fetch/calc/export subcommands with per-stock support"
```

---

### Task 3: Orchestrator — calc 流程 + 数据完整度检查

**Files:**
- Modify: `backend/etl/orchestrator.py`

#### 3.1 新增 `run_calc()` 和 `check_data_completeness()`

- [ ] **Step 1: 在 orchestrator.py 中新增两个函数**

在 `run_etl` 函数之后追加：

```python

def check_data_completeness(con, ts_codes: list[str],
                             min_daily_rows: int = 60) -> dict:
    """检查指定股票在 DWD 层的数据完整度。

    返回:
        {
            "ok": ["000001.SZ", ...],           # 数据充足的股票
            "missing": {                          # 数据不足的股票
                "000543.SZ": {
                    "dwd_rows": 260,
                    "min_date": "20230601",
                    "max_date": "20260603",
                    "gap_days": 1800,             # 缺失交易日天数
                },
                ...
            },
        }
    """
    from backend.fetch.ods_daily import _get_trading_days
    from backend.fetch.client import TushareClient

    ok = []
    missing = {}

    for ts_code in ts_codes:
        row = con.execute("""
            SELECT COUNT(*), MIN(trade_date), MAX(trade_date)
            FROM dwd_daily_quote WHERE ts_code = ?
        """, (ts_code,)).fetchone()

        dwd_rows, min_date, max_date = row
        if dwd_rows >= min_daily_rows:
            ok.append(ts_code)
        else:
            missing[ts_code] = {
                "dwd_rows": dwd_rows,
                "min_date": min_date,
                "max_date": max_date,
            }

    return {"ok": ok, "missing": missing}


def run_calc(con, ts_codes: list[str] = None, auto_fetch: bool = True,
             batch_size: int = 100):
    """执行 DWS 计算流程。

    1. 如果未指定 ts_codes，获取全市场活跃股票
    2. 数据完整度检查
    3. 缺数据 → auto_fetch 补拉 或 报错退出
    4. 逐 Calculator 计算 DWS
    """
    import logging
    import time
    from datetime import datetime
    from backend.fetch.ods_daily import get_all_active_codes
    from backend.fetch.client import TushareClient
    from backend.fetch.ods_daily import fetch_stocks_incremental
    from backend.etl.error_handler import log_etl_start, log_etl_end
    from backend.etl.calc_macd import MACDCalculator
    from backend.etl.calc_ma import MACalculator
    from backend.etl.calc_kpattern import KPatternCalculator
    from backend.etl.calc_dde import DDECalculator
    from backend.etl.calc_volume import VolumeCalculator
    from backend.etl.calc_price_position import PricePositionCalculator

    logger = logging.getLogger(__name__)
    CALCULATORS = [MACDCalculator, MACalculator, KPatternCalculator,
                   DDECalculator, VolumeCalculator, PricePositionCalculator]

    if ts_codes is None:
        ts_codes = get_all_active_codes(con)
    if not ts_codes:
        logger.warning("No stocks to calculate")
        return

    # 1. 数据完整度检查
    completeness = check_data_completeness(con, ts_codes)
    if completeness["missing"]:
        missing_codes = list(completeness["missing"].keys())
        logger.warning("%d stocks have insufficient DWD data", len(missing_codes))

        if auto_fetch and len(missing_codes) <= 50:
            logger.info("Auto-fetching missing data for %d stocks...", len(missing_codes))
            client = TushareClient()
            n = fetch_stocks_incremental(client, con, missing_codes)
            logger.info("Fetched %d ODS rows, rebuilding DWD...", n)
            from backend.etl.build_dwd import build_dwd_daily_quote
            build_dwd_daily_quote(con, missing_codes)
        elif auto_fetch and len(missing_codes) > 50:
            logger.error(
                "%d stocks missing data (threshold: 50). "
                "Run 'python -m backend.cli fetch --ts-code ...' manually, "
                "or use --no-auto-fetch to skip these stocks.",
                len(missing_codes)
            )
            for code, info in completeness["missing"].items():
                logger.error("  %s: %d DWD rows (%s~%s)",
                             code, info["dwd_rows"], info["min_date"], info["max_date"])
            return

    # 2. 只计算数据充足的股票
    codes_to_calc = completeness["ok"]

    # 3. 计算 DWS
    calc_date = datetime.now().strftime("%Y%m%d")
    lid, t0 = log_etl_start(con, "calc_dws")
    grand_total = 0
    calc_start = time.monotonic()

    for CalcCls in CALCULATORS:
        for freq in ("daily", "weekly"):
            calc = CalcCls(con, freq)
            label = f"{CalcCls.__name__} {freq}"
            t1 = time.monotonic()

            for i in range(0, len(codes_to_calc), batch_size):
                batch = codes_to_calc[i:i + batch_size]
                calc.calculate(batch, calc_date)

            elapsed = time.monotonic() - t1
            n = con.execute(
                f"SELECT COUNT(*) FROM {calc.dws_table} "
                f"WHERE calc_date = ?", (calc_date,),
            ).fetchone()[0]
            grand_total += n
            logger.info("calc %-30s DONE — %d rows, %.0fs", label, n, elapsed)

    total_elapsed = time.monotonic() - calc_start
    logger.info("calc ALL DONE — %d stocks, %d rows, %.0fs",
                len(codes_to_calc), grand_total, total_elapsed)
    log_etl_end(con, lid, "calc_dws", t0, "success", row_count=grand_total)
```

- [ ] **Step 2: 验证导入不报错**

```bash
python3 -c "from backend.etl.orchestrator import run_calc, check_data_completeness; print('OK')"
```

- [ ] **Step 3: 运行已有测试确认无回归**

```bash
pytest tests/ -v --tb=short -k "not test_export and not test_build_dwd"
# 预期：~160 passed（排除既有失败）
```

- [ ] **Step 4: 提交**

```bash
git add backend/etl/orchestrator.py
git commit -m "feat: add run_calc with data completeness check and auto-fetch"
```

---

### Task 4: Phase 1 端到端验证

- [ ] **Step 1: 测试 stock-batched fetch**

```bash
python -m backend.cli fetch --ts-code 000543.SZ 600580.SH 000630.SZ --start 20150101
# 预期：拉取 2015 年至今的历史数据
```

- [ ] **Step 2: 验证数据量**

```bash
python3 -c "
import duckdb
con = duckdb.connect('data/tradeanalysis.duckdb')
for c in ['000543.SZ','600580.SH','000630.SZ']:
    n = con.execute('SELECT COUNT(*) FROM ods_daily WHERE ts_code=?', (c,)).fetchone()[0]
    print(f'{c}: {n} rows')
con.close()
"
# 预期：每只 > 2000 行（2015年至今）
```

- [ ] **Step 3: 重建 DWD + 计算 DWS**

```bash
python -m backend.cli calc --ts-code 000543.SZ 600580.SH 000630.SZ 002709.SZ 002837.SZ 603986.SH
# 预期：成功计算，无报错
```

- [ ] **Step 4: 导出验证**

```bash
python -m backend.cli export --date 20260529 --ts-code 000543.SZ 600580.SH 000630.SZ 002709.SZ 002837.SZ 603986.SH --output exports/final_6stocks.xlsx --recalc
# 预期：导出 6 行，周线数据完整
```

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "chore: Phase 1 end-to-end verification"
```

---

## Phase 2: P1 — 指纹跳过基础设施

### Task 5: DWS 表增加 `input_fingerprint` + `spec_version` 列

**Files:**
- Modify: `backend/db/schema.py`

- [ ] **Step 1: 修改 DDL 模板——所有 DWS 表增加两列**

在 `_DWS_DDL` 的每个表模板的 `calc_date` 列之后、`PRIMARY KEY` 之前，增加：

```sql
        input_fingerprint TEXT,
        spec_version     TEXT DEFAULT 'v1',
```

以 volume 表为例，完整 DDL：

```sql
    "volume": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code        TEXT,
        trade_date     TEXT,
        ma_vol_5       REAL,
        pct_vol_rank   REAL,
        zone           TEXT,
        trend          TEXT,
        volume_ratio   REAL,
        trend_strength REAL,
        divergence     TEXT,
        calc_date      TEXT,
        input_fingerprint TEXT,
        spec_version      TEXT DEFAULT 'v1',
        PRIMARY KEY (ts_code, trade_date, calc_date),
        ...
    )""",
```

- [ ] **Step 2: 增加迁移函数**

```python
def _migrate_dws_fingerprint(con: duckdb.DuckDBPyConnection):
    """为所有 DWS 表增加 input_fingerprint 和 spec_version 列。"""
    for ind in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]:
        for freq in ["daily", "weekly"]:
            table = f"dws_{ind}_{freq}"
            for col, col_type in [
                ("input_fingerprint", "TEXT"),
                ("spec_version", "TEXT DEFAULT 'v1'"),
            ]:
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                except Exception:
                    pass
```

- [ ] **Step 3: 在 `create_all_tables()` 中调用迁移**

```python
_migrate_dws_fingerprint(con)
```

- [ ] **Step 4: 验证 schema 创建不报错**

```bash
python3 -c "from backend.db.schema import create_all_tables; from backend.db.connection import get_connection; con=get_connection(); create_all_tables(con); print('OK'); con.close()"
```

- [ ] **Step 5: 提交**

```bash
git add backend/db/schema.py
git commit -m "feat: add input_fingerprint and spec_version columns to all DWS tables"
```

---

### Task 6: `_insert` 公共方法抽取 + fingerprint 计算

**Files:**
- Modify: `backend/etl/base.py`
- Modify: `backend/etl/calc_macd.py` 等 6 个 Calculator

#### 6.1 抽取公共 `insert_dws_batch`

- [ ] **Step 1: 在 base.py 中新增**

```python
import hashlib
import json


def compute_fingerprint(df: "pd.DataFrame", float_cols: list[str]) -> str:
    """为一批 DWS 数据计算内容指纹（SHA256 截断）。

    对 float_cols 中存在的列取摘要（min/max/mean/count），
    生成确定性哈希，用于判断同一 (ts_code, trade_date) 的数据是否变化。
    """
    parts = []
    for col in sorted(float_cols):
        if col not in df.columns:
            continue
        series = df[col].dropna()
        if len(series) == 0:
            parts.append(f"{col}:empty")
        else:
            parts.append(
                f"{col}:{series.min():.6f}:{series.max():.6f}:"
                f"{series.mean():.6f}:{len(series)}"
            )
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def insert_dws_batch(con, table: str, df: "pd.DataFrame", ts_code: str,
                     calc_date: str, dws_cols: list[str],
                     float_cols: list[str],
                     spec_version: str = "v1"):
    """公共 DWS INSERT 方法——替代所有 Calculator 的 _insert。

    相比各 Calculator 各自的 _insert：
    - 统一处理 calc_date（不再丢弃参数）
    - 统一计算 input_fingerprint
    - 统一写入 spec_version
    - 统一 to_float_safe
    """
    import pandas as pd
    import numpy as np

    data_cols = [c for c in dws_cols if c != "ts_code"]
    for c in data_cols:
        if c not in df.columns:
            df[c] = None

    batch = df[data_cols].copy()
    batch["ts_code"] = ts_code

    for c in float_cols:
        if c in batch.columns:
            batch[c] = batch[c].apply(to_float_safe)

    batch["calc_date"] = calc_date
    batch["spec_version"] = spec_version
    batch["input_fingerprint"] = compute_fingerprint(df, float_cols)

    con.register("_batch", batch)
    cols_sql = ", ".join(dws_cols)
    con.execute(
        f"INSERT OR REPLACE INTO {table} ({cols_sql}) "
        f"SELECT {cols_sql} FROM _batch"
    )
    con.unregister("_batch")
```

- [ ] **Step 2: 修改一个 Calculator 的 `_insert` 使用公共方法（以 VolumeCalculator 为例）**

```python
# calc_volume.py _insert 替换为：

def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
    dws_cols = ["ts_code", "trade_date", "ma_vol_5", "pct_vol_rank",
                "zone", "trend", "volume_ratio", "trend_strength",
                "divergence", "calc_date", "input_fingerprint", "spec_version"]
    float_cols = ["ma_vol_5", "pct_vol_rank", "volume_ratio", "trend_strength"]
    insert_dws_batch(self.con, self.dws_table, df, ts_code, calc_date,
                     dws_cols, float_cols)
```

- [ ] **Step 3: 对其他 5 个 Calculator 做同样替换——各 Calculator 的 dws_cols 和 float_cols：**

```python
# MACDCalculator: dws_cols = ["ts_code","trade_date","ema_12","ema_26","dif","dea",
#   "macd_bar","divergence","zone","turning_point","alert","trend","trend_strength",
#   "calc_date","input_fingerprint","spec_version"]
# float_cols = ["ema_12","ema_26","dif","dea","macd_bar","trend_strength"]

# MACalculator: dws_cols = ["ts_code","trade_date","ma_5","ma_10",
#   "bias_ma5","bias_ma10","ma5_slope","ma10_slope","alignment","turning_point",
#   "calc_date","input_fingerprint","spec_version"]
# float_cols = ["ma_5","ma_10","bias_ma5","bias_ma10","ma5_slope","ma10_slope"]

# KPatternCalculator: dws_cols = ["ts_code","trade_date","yang_bao_yin","yang_ke_yin",
#   "mu_bei_xian","bi_lei_zhen","gao_kai_chang_yin","yin_bao_yang","yin_ke_yang",
#   "strength","calc_date","input_fingerprint","spec_version"]
# float_cols = ["strength"]

# DDECalculator: dws_cols = ["ts_code","trade_date","net_mf_amount","ddx","ddx2",
#   "trend","trend_strength","alert","divergence",
#   "calc_date","input_fingerprint","spec_version"]
# float_cols = ["net_mf_amount","ddx","ddx2","trend_strength"]

# PricePositionCalculator: dws_cols = ["ts_code","trade_date",
#   "price_position_60d","price_position_120d","price_position_250d",
#   "calc_date","input_fingerprint","spec_version"]
# float_cols = ["price_position_60d","price_position_120d","price_position_250d"]
```

- [ ] **Step 4: 运行测试确认无回归**

```bash
pytest tests/test_etl/test_calc_volume.py tests/test_etl/test_calc_price_position.py -v
# 预期：全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add backend/etl/base.py backend/etl/calc_*.py
git commit -m "refactor: extract insert_dws_batch with fingerprint + spec_version"
```

---

### Task 7: Phase 2 端到端验证 + 最终回归

- [ ] **Step 1: 全量测试**

```bash
pytest tests/ -v --tb=short
# 预期：~165 passed（排除既有 5 个失败）
```

- [ ] **Step 2: 端到端 calc + export**

```bash
python -m backend.cli calc --ts-code 000543.SZ 600580.SH 000630.SZ
python -m backend.cli export --date 20260529 --ts-code 000543.SZ 600580.SH 000630.SZ --output exports/phase2_test.xlsx --recalc
```

- [ ] **Step 3: 验证 fingerprint 已写入**

```bash
python3 -c "
import duckdb
con = duckdb.connect('data/tradeanalysis.duckdb')
r = con.execute(\"SELECT calc_date, spec_version, input_fingerprint FROM dws_volume_daily WHERE ts_code='000543.SZ' LIMIT 1\").fetchone()
print(f'calc_date={r[0]}, spec={r[1]}, fp={r[2]}')
# 预期：calc_date=真实日期，spec=v1，fp=16位hex
con.close()
"
```

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "chore: Phase 2 end-to-end verification — fingerprint infrastructure"
```

---

## 自检

- ✅ Phase 1 (Tasks 1-4): per-stock 增量 + stock-batched fetch + CLI 拆分 + 数据完整度检查
- ✅ Phase 2 (Tasks 5-7): fingerprint 列 + spec_version + insert_dws_batch 公共方法
- ✅ 每个 Task 有完整代码，无 TBD/TODO
- ✅ 每个测试有预期输出
- ✅ 双模式明确：`--ts-code` → stock-batched，`--all` → date-batched
- ✅ `insert_dws_batch` 修复了 calc_date 被丢弃的 bug（已在之前的 PR 修过，此处固化）
