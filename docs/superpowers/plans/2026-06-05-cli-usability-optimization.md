# CLI 易用性优化 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 加 `run` 命令让交易员一条命令完成日常分析；清理冗余/危险的 CLI 参数（`--all`、`--no-auto-fetch`、`--recalc`）；非交易日输入自动回退。

**Architecture:** 只改 `backend/cli.py` 和 `tests/test_cli.py`。`run` 命令复用现有 `cmd_fetch`/`cmd_calc`/`cmd_export` 逻辑，不引入新函数签名变更。

**Tech Stack:** Python 3.9, argparse, DuckDB, tushare

---

## 文件改动清单

| 文件 | 改动类型 | 职责 |
|------|:----:|------|
| `backend/cli.py` | 修改 | 参数清理 + `run` 命令 + 日期解析 |
| `tests/test_cli.py` | 修改 | `run` 命令测试 + 参数变化测试 |

---

### Task 1: 日期解析工具函数

**Files:**
- Modify: `backend/cli.py`

#### 1.1 RED — 写测试

- [ ] **Step 1: 在 `tests/test_cli.py` 末尾追加测试**

```python
import io
import pytest
from backend.cli import _resolve_trade_date, _ensure_trade_date


def test_resolve_trade_date_with_date():
    """指定日期 → 直接返回。"""
    assert _resolve_trade_date(None, "20260604") == "20260604"


def test_resolve_trade_date_default_today():
    """不指定 → 用今天日期。"""
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    result = _resolve_trade_date(None, None)
    assert result == today


def test_ensure_trade_date_is_trade_day():
    """20260604 是周四，应该原样返回。"""
    result = _ensure_trade_date(None, "20260604")
    assert result == "20260604"


def test_ensure_trade_date_weekend_rollback():
    """20260607 是周日，应回退到 20260605（周五）。"""
    result = _ensure_trade_date(None, "20260607")
    assert result == "20260605"
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_cli.py::test_resolve_trade_date_with_date \
      tests/test_cli.py::test_resolve_trade_date_default_today \
      tests/test_cli.py::test_ensure_trade_date_is_trade_day \
      tests/test_cli.py::test_ensure_trade_date_weekend_rollback -v
# 预期: ImportError — 函数不存在
```

#### 1.2 GREEN — 实现

- [ ] **Step 3: 在 `backend/cli.py` 的 `setup_logging()` 之后插入两个函数**

```python
def _resolve_trade_date(con, date: str = None) -> str:
    """解析分析日期：指定则用指定，不指定则用今天。

    Returns YYYYMMDD string. con is unused but accepted for future use
    (e.g. falling back to DB latest trade_date).
    """
    if date:
        return date
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d")


def _ensure_trade_date(con, date: str) -> str:
    """确保 date 是交易日；不是则往前找最近交易日。

    Queries dim_date to validate and rollback. Prints a warning if
    the original date was not a trading day.
    """
    row = con.execute(
        "SELECT MAX(trade_date) FROM dim_date "
        "WHERE trade_date <= ? AND is_trade_day = 1",
        (date,),
    ).fetchone()
    if not row or not row[0]:
        return date  # dim_date may be empty — trust the caller
    trade_date = row[0]
    if trade_date != date:
        print(f"Warning: {date} is not a trading day, using {trade_date} instead")
    return trade_date
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_cli.py -v
# 预期: 原有 3 个 PASS + 新增 4 个 PASS = 7 passed
```

- [ ] **Step 5: 提交**

```bash
git add backend/cli.py tests/test_cli.py
git commit -m "feat: add _resolve_trade_date and _ensure_trade_date helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 清理冗余参数

**Files:**
- Modify: `backend/cli.py:196-224`

#### 2.1 RED — 写测试

- [ ] **Step 1: 追加测试验证参数变化**

```python
def test_cli_fetch_no_all_param():
    """fetch 不应再有 --all 参数。"""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "fetch", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "--all" not in result.stdout


def test_cli_calc_no_no_auto_fetch():
    """calc 不应再有 --no-auto-fetch。"""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "calc", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "--no-auto-fetch" not in result.stdout


def test_cli_export_no_recalc():
    """export 不应再有 --recalc 和 --no-auto-fetch。"""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "export", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "--recalc" not in result.stdout
    assert "--no-auto-fetch" not in result.stdout


def test_cli_fetch_defaults_to_all():
    """fetch 不传 --ts-code → 全市场。"""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "fetch", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    # --ts-code 应该标记为可选 (not required)
    assert "--ts-code" in result.stdout
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_cli.py::test_cli_fetch_no_all_param \
      tests/test_cli.py::test_cli_calc_no_no_auto_fetch \
      tests/test_cli.py::test_cli_export_no_recalc -v
# 预期: FAIL — --all / --no-auto-fetch / --recalc 仍然存在
```

#### 2.2 GREEN — 实现

- [ ] **Step 3: 修改 `backend/cli.py` 参数定义**

把第 196-224 行（`main()` 函数中的参数定义）改为：

```python
    # fetch
    fp = sp.add_parser("fetch", help="Pull ODS data into DuckDB")
    fp.add_argument("--ts-code", nargs="+", help="Stock codes to fetch (omitted = all stocks)")
    fp.add_argument("--start", help="Start date YYYYMMDD (default 20150101)")
    fp.add_argument("--end", help="End date YYYYMMDD (default today)")

    # calc
    cp = sp.add_parser("calc", help="Compute DWS indicators")
    cp.add_argument("--ts-code", nargs="+", help="Stock codes to calculate (omitted = all stocks)")

    # export
    xp = sp.add_parser("export", help="Export analysis wide table to Excel")
    xp.add_argument("--date", required=True, help="Analysis date YYYYMMDD")
    xp.add_argument("--output", default=None,
                    help="Output Excel path. Default: analysis_{date}_gen{now}.xlsx")
    xp.add_argument("--ts-code", nargs="+", help="Stock codes to export")
    xp.add_argument("--db-path")
    xp.add_argument("--include-st", action="store_true")
    xp.add_argument("--no-index", action="store_true")
```

- [ ] **Step 4: 同步修改 `cmd_fetch` 注释**

```python
def cmd_fetch(args):
    """Pull ODS data into DuckDB.

    No --ts-code: date-batched mode for full market.
    --ts-code: stock-batched mode, per-stock incremental.
    """
```

- [ ] **Step 5: 修改 `cmd_calc`**

```python
def cmd_calc(args):
    """Compute DWS indicators.

    Auto-fetches missing data before calculating.
    No --ts-code: calculate all active stocks.
    """
    from backend.db.connection import get_connection
    from backend.etl.orchestrator import run_calc

    con = get_connection()
    try:
        ts_codes = args.ts_code if args.ts_code else None
        run_calc(con, ts_codes=ts_codes, auto_fetch=True)
    finally:
        con.close()
```

- [ ] **Step 6: 修改 `cmd_export`（去 `--recalc` 和 `auto_fetch`）**

```python
def cmd_export(args):
    """Export analysis wide table to Excel.

    Reads DWS data directly from the database. No recalculation.
    Use 'calc' then 'export' separately if fresh data is needed.
    """
    from backend.export_wide import export_wide_to_excel

    if args.output is None:
        from datetime import datetime
        gen_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"analysis_{args.date}_gen{gen_ts}.xlsx"

    ts_codes = args.ts_code if args.ts_code else None

    n = export_wide_to_excel(
        args.db_path or "data/tradeanalysis.duckdb",
        args.date,
        args.output,
        filter_st=not args.include_st,
        include_index=not args.no_index,
        ts_codes=ts_codes,
    )
    print(f"Exported {n} rows -> {args.output}")
```

- [ ] **Step 7: 运行测试确认通过**

```bash
pytest tests/test_cli.py -v
# 预期: 11 passed
```

- [ ] **Step 8: 提交**

```bash
git add backend/cli.py tests/test_cli.py
git commit -m "refactor: remove --all, --no-auto-fetch, --recalc from CLI

--all removed: no --ts-code means all stocks (simpler, no ambiguity).
--no-auto-fetch removed: data incompleteness should error, not silently skip.
--recalc removed: calc and export decoupled — run them separately.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 新增 `run` 命令

**Files:**
- Modify: `backend/cli.py`

#### 3.1 RED — 写测试

- [ ] **Step 1: 追加 `run` 命令测试**

```python
def test_cli_run_help_shows():
    """run 命令应有帮助信息。"""
    env = {**os.environ, "TUSHARE_TOKEN": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "backend.cli", "run", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "--date" in result.stdout
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/test_cli.py::test_cli_run_help_shows -v
# 预期: FAIL — 'run' 子命令不存在
```

#### 3.2 GREEN — 实现

- [ ] **Step 3: 新增 `cmd_run` 函数**

在 `cmd_export` 之后插入：

```python
# ── run ──

def cmd_run(args):
    """One-command daily analysis: fetch → calc → export.

    Resolves the target trading date, auto-fetches any missing ODS data,
    rebuilds DWD, runs all DWS calculators, and exports the Excel report.
    """
    import time
    from backend.db.connection import get_connection
    from backend.export_wide import export_wide_to_excel

    con = get_connection()
    try:
        # 1. Resolve date
        date = _resolve_trade_date(con, args.date)
        date = _ensure_trade_date(con, date)

        # 2. Fetch — always full market, with per-stock incremental detection
        print(f"=== Step 1/3: Fetching data for {date} ===")
        cmd_fetch(args)

        # 3. Calc — auto-fetches missing data before computing
        print(f"=== Step 2/3: Computing indicators for {date} ===")
        cmd_calc(args)

        # 4. Export
        print(f"=== Step 3/3: Exporting analysis for {date} ===")
        if args.output is None:
            from datetime import datetime
            gen_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            args.output = f"analysis_{date}_gen{gen_ts}.xlsx"
        args.date = date  # ensure export uses resolved date

        ts_codes = args.ts_code if args.ts_code else None
        n = export_wide_to_excel(
            args.db_path or "data/tradeanalysis.duckdb",
            date,
            args.output,
            filter_st=not args.include_st,
            include_index=not args.no_index,
            ts_codes=ts_codes,
        )
        print(f"Exported {n} rows -> {args.output}")
        print("Done.")
    finally:
        con.close()
```

- [ ] **Step 4: 在 `main()` 中注册 `run` 命令和 handler**

在参数定义区（`status` parser 之前）插入：

```python
    # run
    rp = sp.add_parser("run", help="One-command daily analysis: fetch → calc → export")
    rp.add_argument("--date", help="Analysis date YYYYMMDD (default: today)")
    rp.add_argument("--ts-code", nargs="+", help="Stock codes (omitted = all stocks)")
    rp.add_argument("--output", default=None,
                    help="Output Excel path. Default: analysis_{date}_gen{now}.xlsx")
    rp.add_argument("--include-st", action="store_true")
    rp.add_argument("--no-index", action="store_true")
```

在 handlers 字典中加入：

```python
    handlers = {
        "check": cmd_check,
        "fetch": cmd_fetch,
        "calc": cmd_calc,
        "export": cmd_export,
        "run": cmd_run,
        "query": cmd_query,
        "status": cmd_status,
    }
```

- [ ] **Step 5: 更新模块文档字符串**

```python
"""CLI entry point for the Tradeanalysis data pipeline.

Usage:
    python -m backend.cli run
    python -m backend.cli run --date 20260604
    python -m backend.cli check
    python -m backend.cli fetch [--ts-code 000543.SZ] [--start 20150101]
    python -m backend.cli calc [--ts-code 000543.SZ]
    python -m backend.cli export --date 20260529 [--ts-code 000543.SZ]
    python -m backend.cli query --ts-code 000001.SZ
    python -m backend.cli status
"""
```

- [ ] **Step 6: 运行测试确认通过**

```bash
pytest tests/test_cli.py -v
# 预期: 12 passed
```

- [ ] **Step 7: 提交**

```bash
git add backend/cli.py tests/test_cli.py
git commit -m "feat: add 'run' command — one-command daily analysis pipeline

run = fetch → calc → export, with auto-fetch and date validation.
Non-trading-day input auto-rolls back to nearest trading day.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 全量测试回归 + CLAUDE.md 更新

- [ ] **Step 1: 运行全量测试**

```bash
pytest tests/ -v --tb=short
# 预期: 新增测试 PASS，既有 5 个失败不受影响
```

- [ ] **Step 2: 更新 CLAUDE.md 常用命令**

将 `## 常用命令` 区块更新为：

```bash
# ===== 每日分析（一条命令） =====
python -m backend.cli run                          # 全市场，最近交易日
python -m backend.cli run --date 20260604          # 全市场，指定日期

# ===== 数据拉取 =====
python -m backend.cli fetch                        # 全市场增量拉取
python -m backend.cli fetch --ts-code 000543.SZ 600580.SH  # 指定股票

# ===== 指标计算 =====
python -m backend.cli calc                         # 全市场
python -m backend.cli calc --ts-code 000543.SZ 600580.SH  # 指定股票

# ===== Excel 导出 =====
python -m backend.cli export --date 20260603       # 全市场，默认文件名
python -m backend.cli export --date 20260603 --ts-code 000543.SZ  # 指定股票

# ===== 查询 + 环境检查 =====
python -m backend.cli query --ts-code 000001.SZ --freq daily
python -m backend.cli check
python -m backend.cli status
```

更新 CLI 三层架构描述：

```
run（一站式入口）→ fetch → calc → export

fetch（数据拉取层）
├── 不传 --ts-code → date-batched 全市场并行拉取
├── --ts-code → stock-batched per-stock 增量拉取
└── per-stock 增量：每只股票独立查询 ods_daily 已有日期，只补缺

calc（计算层）
├── 不传 --ts-code → 全市场计算
├── auto_fetch=True：缺数据自动补拉（warmup=250 tdays）
├── DWD 重建 → fingerprint 检查 → 计算变化股票
└── 熔断器：连续 5 次失败中止

export（导出层）
├── 从 latest 视图直接导出（不重算）
└── 文件名: analysis_{date}_gen{now}.xlsx
```

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for simplified CLI with 'run' command

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 端到端验证 — 用 `run` 命令跑 20260604 分析

- [ ] **Step 1: 运行 run 命令**

```bash
python -m backend.cli run --date 20260604
# 预期:
# === Step 1/3: Fetching data for 20260604 ===
# (增量跳过已有数据)
# === Step 2/3: Computing indicators for 20260604 ===
# (计算 DWS)
# === Step 3/3: Exporting analysis for 20260604 ===
# Exported N rows -> analysis_20260604_gen{timestamp}.xlsx
# Done.
```

- [ ] **Step 2: 验证输出**

```bash
ls -lh analysis_20260604_gen*.xlsx
# 预期: 文件存在，大小 > 1MB（全市场数据）
```

- [ ] **Step 3: 验证非交易日回退**

```bash
python -m backend.cli run --date 20260607
# 预期: Warning: 20260607 is not a trading day, using 20260605 instead
```

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "chore: end-to-end verification — 'run' command"
```

---

## 自检

- ✅ 只改 `backend/cli.py` + `tests/test_cli.py`，不改底层函数签名
- ✅ `run` 复用现有的 `cmd_fetch`/`cmd_calc`/`export_wide_to_excel`
- ✅ 去掉 3 个冗余参数（`--all`、`--no-auto-fetch`、`--recalc`），加 1 个命令（`run`）
- ✅ 每个 Task 有 RED→GREEN TDD 循环
- ✅ 非交易日回退基于 `dim_date` 查询
- ✅ 向后兼容：`fetch`/`calc`/`export`/`query`/`check`/`status` 全部保留
- ✅ 输出文件名不变（`analysis_{date}_gen{now}.xlsx`）
