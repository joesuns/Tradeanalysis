# 日志系统优化实施计划

> **状态：** 待审批
> **日期：** 2026-06-05
> **基于：** 两轮评估结论（原始评估 + 系统架构师复审）
> **预计总工作量：** ~60 分钟（P0+P1 ~35min, P2 ~25min）

---

## 一、改动总览

| 优先级 | 文件 | 改动类型 | 风险 | 预计时间 |
|--------|------|---------|------|---------|
| P0 | [error_handler.py](../../../backend/etl/error_handler.py) | 重构重复日志 | 低 | 10min |
| P0 | [ods_daily.py](../../../backend/fetch/ods_daily.py) | 4处 error→exception | 无 | 5min |
| P1 | [log_config.py](../../../backend/log_config.py) | 新增 trace ID 机制 | 低 | 15min |
| P1 | [cli.py](../../../backend/cli.py) | 6处 print→logger + set_run_id | 无 | 10min |
| P2 | [router.py](../../../backend/api/router.py) | 新增业务日志 | 无 | 10min |
| P2 | [ods_daily.py](../../../backend/fetch/ods_daily.py) | 异常聚合日志 | 无 | 10min |
| — | [test_log_config.py](../../../tests/test_log_config.py) | 新增 2 个测试 | 无 | 10min |

---

## 二、P0 — 立即修复

### 2.1 `error_handler.py` — 消除重复日志 + DB 存完整栈

**问题：** `log_etl_error()` 同时调用 `logger.exception()`（输出完整栈到日志文件）和 `log_etl_end(status=failed)`（输出截断栈到 WARNING）。同一个错误产生两条日志，且 DB 中 error_msg 被截断到 500 字符。

**精确改动：**

**A. `log_etl_error()` 函数（line 67-78）：**

```python
# 改前：
def log_etl_error(con, log_id: str, step_name: str, start_time: float,
                  row_count: int, exception: Exception,
                  min_trade_date: Optional[str] = None,
                  max_trade_date: Optional[str] = None):
    """Convenience: log a step as 'failed' with full traceback via logger.exception()."""
    tb = traceback.format_exc()
    logger.exception(f"ETL {step_name} — FAILED")
    log_etl_end(
        con, log_id, step_name, start_time, "failed",
        row_count=row_count,
        error_msg=f"{type(exception).__name__}: {exception}\n{tb[-500:]}",
    )

# 改后：
def log_etl_error(con, log_id: str, step_name: str, start_time: float,
                  row_count: int, exception: Exception,
                  min_trade_date: Optional[str] = None,
                  max_trade_date: Optional[str] = None):
    """Log a step as 'failed'. One-line ERROR to logger, full traceback to DB."""
    tb = traceback.format_exc()
    duration_ms = round((time.monotonic() - start_time) * 1000)
    logger.error("ETL %s — FAILED: %s (%dms)",
                 step_name, exception, duration_ms)
    log_etl_end(
        con, log_id, step_name, start_time, "failed",
        row_count=row_count,
        error_msg=f"{type(exception).__name__}: {exception}\n{tb}",
    )
```

关键变化：
- `logger.exception()` → `logger.error()`（一行概要，不带栈）
- DB 中 `error_msg` 的 `tb[-500:]` → `tb`（完整 traceback，不截断）
- 新增 `duration_ms` 在概要行中输出

**B. `log_etl_end()` 函数（line 61-62）：**

```python
# 改前（line 61-64）：
    if status in ("failed", "degraded"):
        logger.warning(f"ETL {step_name}: {status} ({duration_ms}ms) — {error_msg}")
    else:
        logger.info(f"ETL {step_name}: {status} ({duration_ms}ms, {row_count} rows)")

# 改后：
    if status == "failed":
        # 概要已由 log_etl_error 输出，这里输出带 error_msg 摘要的 ERROR
        err_summary = error_msg.split("\n")[0] if error_msg else ""
        logger.error("ETL %s: failed (%dms) — %s",
                     step_name, duration_ms, err_summary)
    elif status == "degraded":
        logger.warning("ETL %s: degraded (%dms) — %s",
                       step_name, duration_ms, error_msg[:200] if error_msg else "")
    else:
        logger.info("ETL %s: %s (%dms, %d rows)",
                    step_name, status, duration_ms, row_count)
```

关键变化：
- `failed` 用 `logger.error()` 而非 `logger.warning()`——失败是 ERROR 级别的
- `degraded` 保持 `logger.warning()`
- `error_msg` 中的 traceback 不再输出到日志（完整栈在 DB 中可查）

**受影响调用方：** 无 API 变化。`log_etl_start`、`log_etl_end`、`log_etl_error` 的函数签名不变。

---

### 2.2 `ods_daily.py` — 4 处 `logger.error()` → `logger.exception()`

**问题：** 异常被捕获后只记录异常消息，丢失调用栈，排查时无法定位异常发生的精确代码行。

**精确改动：** 每处只改函数名 + 去掉 `{e}`（`logger.exception()` 自动附加异常信息）

| # | 行号 | 当前位置 | 改动 |
|---|------|---------|------|
| 1 | 132 | `fetch_by_date_range` 内 per-date 异常 | `logger.error(f"Failed trade_date={trade_date}: {e}")` → `logger.exception("Failed trade_date=%s", trade_date)` |
| 2 | 293 | `fetch_stocks_incremental` 内 trade_cal API 失败 | `logger.error("...trade_cal API failed — %s", e)` → `logger.exception("fetch_stocks_incremental: trade_cal API failed")` |
| 3 | 327-328 | `fetch_stocks_incremental` 内 per-stock daily 异常 | `logger.error("fetch_stocks_incremental %s [%s~%s]: %s", ...)` → `logger.exception("fetch_stocks_incremental %s [%s~%s]", ts_code, seg_start, seg_end)` |
| 4 | 560 | `_fetch_chunk` 线程内 per-date 异常 | `logger.error(f"Thread failed trade_date={trade_date}: {e}")` → `logger.exception("Thread failed trade_date=%s", trade_date)` |

**不改的位置：**
- [client.py:60-62](../../../backend/fetch/client.py#L60-L62)：retry exhausted 后 `logger.error()` + `raise`，合理（紧接着 re-raise 给上层处理）
- [ods_daily.py:343](../../../backend/fetch/ods_daily.py#L343)：`logger.warning("...daily_basic skipped...")` — 非致命降级，用 warning 合理
- [ods_daily.py:370](../../../backend/fetch/ods_daily.py#L370)：`logger.warning("...moneyflow skipped...")` — 同上

---

## 三、P1 — 架构改进

### 3.1 `log_config.py` — 新增 trace ID 机制

**问题：** 无请求级别的唯一标识符，多线程并发时日志交错无法关联。

**方案：** `contextvars.ContextVar` + `logging.Filter`，零侵入，所有现有 `logger.info()` 自动携带 run_id。

**精确改动：**

```python
# === 文件顶部新增 import ===
import contextvars

# === 新增 ContextVar（放在 _FORMAT 定义前） ===
_run_id: contextvars.ContextVar[str] = contextvars.ContextVar('run_id', default='-')


class _RunIdFilter(logging.Filter):
    """Inject run_id into every log record from context variable."""
    def filter(self, record):
        record.run_id = getattr(record, 'run_id', _run_id.get())
        return True


def set_run_id(run_id: str):
    """Set the run ID for the current execution context.

    Call at CLI entry point before any work begins.
    Thread-safe: each thread inherits the parent's run_id at creation time,
    but modifications in child threads do not propagate back.
    """
    _run_id.set(run_id)
```

**格式字符串改动（line 21）：**

```python
# 改前：
_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"

# 改后：
_FORMAT = "%(asctime)s %(levelname)-8s [%(run_id)s][%(name)s] %(message)s"
```

**`setup_logging()` 改动：** 在 handler 添加之后、return 之前加一行：

```python
root.addFilter(_RunIdFilter())
```

**影响分析：**
- 现有日志格式从 `2026-06-05T20:44:00 INFO     [backend.etl.orchestrator] ...` 变为 `2026-06-05T20:44:00 INFO     [a1b2c3d4][backend.etl.orchestrator] ...`
- 不设置 run_id 时显示 `[-]`，不影响未调用 `set_run_id()` 的场景（如测试）
- `test_log_format_includes_module` 测试需更新：检查 `[test_module]` → 检查 `[-][test_module]` 或添加 `run_id` 字段检查

---

### 3.2 `cli.py` — print() 转 logger + 设置 run_id

**问题：** CLI 关键操作标记用 `print()`，不进日志文件，事后回溯缺失高层执行流程。

**精确改动：**

**A. 文件头部新增 import（line 14-17）：**

```python
import argparse
import logging
import sys
import uuid

from backend.log_config import setup_logging, set_run_id

logger = logging.getLogger(__name__)  # 新增模块级 logger
setup_logging()
```

**B. `_ensure_trade_date` 函数（line 48）：**

```python
# 改前：
print(f"Warning: {date} is not a trading day, using {trade_date} instead")

# 改后：
logger.warning("%s is not a trading day, using %s instead", date, trade_date)
```

**C. `cmd_fetch` 函数（line 93, 97）：**

```python
# 改前（line 93）：
print(f"Stock-batched fetch: {len(codes)} stocks, {start}~{end}")

# 改后：
logger.info("Stock-batched fetch: %d stocks, %s~%s", len(codes), start, end)

# 改前（line 97）：
print(f"Date-batched fetch: {len(codes)} active stocks, {start}~{end}")

# 改后：
logger.info("Date-batched fetch: %d active stocks, %s~%s", len(codes), start, end)
```

> **保留不变**（line 101）：`print(f"Fetched {n} rows")` — 用户直接消费的最终结果

**D. `cmd_run` 函数（line 172, 176, 192）：**

```python
# 改前（line 172）：
print(f"=== Step 1/2: Computing indicators for {date} ===")

# 改后：
logger.info("=== Step 1/2: Computing indicators for %s ===", date)

# 改前（line 176）：
print(f"=== Step 2/2: Exporting analysis for {date} ===")

# 改后：
logger.info("=== Step 2/2: Exporting analysis for %s ===", date)

# 改前（line 192）：
print("Done.")

# 改后：
logger.info("Done.")
```

> **保留不变**（line 191）：`print(f"Exported {n} rows -> {args.output}")` — 最终结果

**E. `main` 函数 — 设置 run_id：**

在 `handlers = handlers.get(args.command)` 调用前插入（line 304 附近）：

```python
args = p.parse_args()

# 为非 help 命令设置 trace ID
if args.command:
    set_run_id(uuid.uuid4().hex[:8])

handlers = {
```

这样 `python -m backend.cli run`、`python -m backend.cli fetch`、`python -m backend.cli calc` 等所有子命令自动获得唯一 run_id。

---

## 四、P2 — 增强改进

### 4.1 `router.py` — API 业务日志

**问题：** API 路由层无 logger，中间件只记 HTTP 状态码。查询无结果时（404），中间件看到 200 或 404，不知道内部发生了什么。

**精确改动：**

**A. 文件头部新增（line 1 之后）：**

```python
import logging

logger = logging.getLogger(__name__)
```

**B. 各端点关键位置：**

`/health` — 不需要额外日志（总是 200）

`/analysis/{ts_code}` — line 92 之前（404 场景）：

```python
if row is None:
    logger.info("analysis/%s %s: not found", freq, ts_code)
    raise HTTPException(...)
```

`/analysis/{ts_code}/history` — line 163 之前（结果返回时）：

```python
logger.info("analysis/%s/history %s: %d rows (fields=%s)",
            freq, ts_code, len(data), fields)
```

`/screening` — line 235 之前（结果返回时）：

```python
logger.info("screening/%s: %d results (macd_zone=%s, ma_alignment=%s, min_ddx=%s)",
            freq, len(results), macd_zone, ma_alignment, min_ddx)
```

---

### 4.2 `ods_daily.py` — per-date fetch 异常结束时聚合

**问题：** `fetch_by_date_range` 中每个失败的 trade_date 单独记录 error，但结束时无汇总。需要 grep 才能知道有多少日期失败。

**精确改动：**

`fetch_by_date_range` 函数（line 24-134），在循环外新增 `failed_dates` 列表，循环内收集，循环后在 return 前输出聚合：

```python
# line 30 附近，t0 之后新增：
failed_dates = []

# line 131-132 改：
except Exception as e:
    failed_dates.append(trade_date)
    logger.exception("Failed trade_date=%s", trade_date)

# line 133 return 前新增聚合：
if failed_dates:
    logger.warning("fetch_by_date_range: %d/%d dates failed: %s",
                   len(failed_dates), len(days),
                   ", ".join(failed_dates[:5]) +
                   ("..." if len(failed_dates) > 5 else ""))
```

---

## 五、测试更新

### 5.1 `test_log_config.py` — 新增测试

**新增测试 1：`test_trace_id_injected`**

```python
def test_trace_id_injected():
    """When set_run_id is called, log records include the run_id."""
    import io
    from backend.log_config import setup_logging, set_run_id

    root = logging.getLogger()
    old_handlers = list(root.handlers)
    root.handlers.clear()
    old_level = root.level
    root.setLevel(logging.DEBUG)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(levelname)s [%(run_id)s][%(name)s] %(message)s"
    ))

    try:
        root.addHandler(handler)
        # Re-run setup to add the filter (idempotent, but filter needs adding)
        from backend.log_config import _RunIdFilter
        root.addFilter(_RunIdFilter())

        set_run_id("test123")
        logger = logging.getLogger("test.module")
        logger.info("hello")

        output = stream.getvalue()
        assert "[test123][test.module]" in output, (
            f"run_id missing from: {output}"
        )
    finally:
        for f in root.filters:
            root.removeFilter(f)
        root.handlers.clear()
        for h in old_handlers:
            root.addHandler(h)
        root.setLevel(old_level)
```

**新增测试 2：`test_default_run_id_is_dash`**

```python
def test_default_run_id_is_dash():
    """Without set_run_id, run_id defaults to '-'."""
    import io
    from backend.log_config import _RunIdFilter

    root = logging.getLogger()
    old_handlers = list(root.handlers)
    root.handlers.clear()
    old_level = root.level
    root.setLevel(logging.DEBUG)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(levelname)s [%(run_id)s][%(name)s] %(message)s"
    ))

    try:
        root.addHandler(handler)
        root.addFilter(_RunIdFilter())

        logger = logging.getLogger("test.default")
        logger.info("no run id set")

        output = stream.getvalue()
        assert "[-][test.default]" in output, (
            f"Expected default '-' run_id, got: {output}"
        )
    finally:
        for f in root.filters:
            root.removeFilter(f)
        root.handlers.clear()
        for h in old_handlers:
            root.addHandler(h)
        root.setLevel(old_level)
```

**现有测试 `test_log_format_includes_module` 的兼容性：**

该测试创建自己的 handler 和 formatter，不使用 `setup_logging()`，因此不受 `_FORMAT` 改动影响。**无需修改。**

---

## 六、不改清单（明确排除）

以下位置上轮评估中提到但本轮复审确定不需要改：

| 位置 | 原因 |
|------|------|
| `client.py:60-62` | retry exhausted 后 re-raise，`error()` 合理 |
| `cmd_check` 全部 print() | 即时交互输出，无事後保留价值 |
| `cmd_query` 全部 print() | 即时查询结果 |
| `cmd_status` 全部 print() | 即时查看统计 |
| `cmd_fetch:101` print("Fetched N rows") | 最终用户可消费的摘要 |
| `cmd_export:150` print("Exported N rows") | 同上 |
| `cmd_run:191` print("Exported N rows") | 同上 |
| `build_dim.py` | orchestrator 层 `log_etl_start/end` 已覆盖 |
| `rebuild_all_dwd` 子步骤 | 同上 |
| `_choose_fetch_strategy` | 已有日志（`orchestrator.py:518-519`） |
| `ods_moneyflow.py` | 死代码，建议单独 PR 删除 |

---

## 七、验证清单

实施完成后逐项确认：

- [ ] `pytest tests/test_log_config.py -v` 全部通过（3 旧 + 2 新 = 5 个测试）
- [ ] `pytest tests/ -v` 全量测试通过（确保无回归）
- [ ] 手动执行 `python -m backend.cli check` — 输出正常，日志文件有新行
- [ ] 手动执行 `python -m backend.cli status` — 同上
- [ ] 检查 `tradeanalysis.log` 中新增的 `[run_id]` 字段出现
- [ ] 检查 `tradeanalysis.log` 中不再有重复的 traceback 日志
- [ ] 检查 `tradeanalysis.log` 中 `=== Step` 标记出现（不再只在 stdout）
- [ ] CLAUDE.md 更新（日志格式变更）

---

## 八、回滚方案

所有改动是加性的（新增字段/trace ID、升级日志级别、增加日志行），不改变任何函数签名、数据结构或业务逻辑。回滚只需 `git revert`。

唯一需要关注的是 `_FORMAT` 字符串变更——如果有外部日志解析脚本依赖旧格式 `[%(name)s]`，需要更新为 `[%(run_id)s][%(name)s]`。当前项目无此类依赖。
