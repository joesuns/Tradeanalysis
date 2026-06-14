# Batch Preflight 静默空窗优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除同日复跑 `calc.batch_preflight: done` 后 ~140s + `batch_append: done` 后 ~178s 两段无日志「假卡死」，将稳态同日复跑墙钟从 **~10min 压向 ≤5min**（不改 DWS 语义、不重算指标）。

**Architecture:** 根因是 **指纹重复计算**（preflight 已算 `state_signature`，skip_refresh 再算一遍）与 **运维日志过量写入**（12×5389 条 `ods_calc_skip_log` + 24 次大 IN `COUNT`）。方案：在 `classify_calc_mode` 单次分类时缓存 `cur_fp`，skip_refresh / chunk fast_skip 复用；全 SKIP 且 `CALC_SKIP_STATE_REFRESH` 命中时整段跳过；skip_log 改摘要写入；`chunk=0` 时跳过冗余 COUNT。

**Tech Stack:** Python 3.9、DuckDB、pytest；依赖现有 `CALC_SKIP_STATE_REFRESH` / `CALC_BATCH_APPEND` / `SIG_WINDOW=245`。

**实库锚点（2026-06-11，5389 股同日复跑）：**

| 段 | 墙钟 | 日志 |
|----|------|------|
| `batch_preflight` | 171s | 有进度 |
| preflight→`skip_refresh` 空窗 | **~140s** | 零输出 |
| `skip_refresh` | 0s（records=0） | 有起止 |
| `batch_append done`→`calc.stocks` 空窗 | **~178s** | 零输出 |
| 总墙钟 | 613s | — |

**前置文档：** `docs/superpowers/plans/2026-06-08-calc-fast-skip-preflight.md`、`docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md`（附录 C 已落地 dde_weekly 37s）

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `backend/etl/calc_router.py` | `classify_calc_mode` 返回/缓存 `cur_fp` |
| `backend/etl/calc_fast_skip.py` | preflight 产出 `fp_cache`；共享 `build_skip_state_records()` |
| `backend/etl/calc_batch_append.py` | 复用 fp_cache；`skip_refresh` 快路径 + 进度 |
| `backend/etl/orchestrator.py` | chunk fast_skip 复用 helper；skip_log 摘要；COUNT 快路径 |
| `backend/config.py` | `CALC_SKIP_LOG_VERBOSE`（默认 0=摘要） |
| `tests/test_etl/test_calc_fast_skip.py` | fp 缓存等价性 |
| `tests/test_etl/test_batch_append_calc.py` | skip_refresh 不重复算 fp |
| `tests/test_etl/test_orchestrator.py` | skip_log 摘要条数 |

---

## Task 1: `classify_calc_mode` 单次返回指纹

**Files:**
- Modify: `backend/etl/calc_router.py`
- Test: `tests/test_etl/test_calc_router.py`（若无则新建）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_etl/test_calc_router.py
import pandas as pd
from backend.etl.calc_router import classify_calc_mode, classify_calc_mode_detail

def test_classify_calc_mode_detail_returns_same_mode_and_fp():
    df = pd.DataFrame({
        "trade_date": ["20260101", "20260102"],
        "close_qfq": [10.0, 10.1],
    })
    state = {
        "last_trade_date": "20260102",
        "history_fp": "deadbeef00000000",
        "spec_version": "v1",
        "updated_calc_date": "20260609",
    }
    mode_plain, _ = classify_calc_mode(df, state, ["close_qfq"])
    mode_det, _, fp = classify_calc_mode_detail(df, state, ["close_qfq"])
    assert mode_plain == mode_det
    assert len(fp) == 16
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
pytest tests/test_etl/test_calc_router.py::test_classify_calc_mode_detail_returns_same_mode_and_fp -v
```

Expected: FAIL `classify_calc_mode_detail` not defined

- [ ] **Step 3: 实现 `classify_calc_mode_detail`**

在 `calc_router.py` 增加：

```python
def classify_calc_mode_detail(
    df, state, sig_cols, sig_window=SIG_WINDOW, expected_spec_version=None,
):
    """Like classify_calc_mode but also returns cur_fp (or None if not computed)."""
    if state is None:
        return "FULL", [], None
    if expected_spec_version is not None:
        stored = state.get("spec_version") or "v1"
        if stored != expected_spec_version:
            return "FULL", [], None
    last_td = state["last_trade_date"]
    cur_fp = state_signature(df, last_td, sig_cols, sig_window)
    if cur_fp != state["history_fp"]:
        return "FULL", [], cur_fp
    new_bars = df[df["trade_date"] > last_td]["trade_date"].astype(str).tolist()
    if not new_bars:
        return "SKIP", [], cur_fp
    return "APPEND", new_bars, cur_fp
```

`classify_calc_mode` 改为薄包装：`mode, bars, _ = classify_calc_mode_detail(...); return mode, bars`

- [ ] **Step 4: 跑测试 PASS**

```bash
pytest tests/test_etl/test_calc_router.py -v
```

---

## Task 2: Preflight 产出 per-stock fp 缓存

**Files:**
- Modify: `backend/etl/calc_fast_skip.py`
- Test: `tests/test_etl/test_calc_fast_skip.py`

- [ ] **Step 1: 写失败测试**

```python
def test_preflight_stock_modes_v2_returns_fp_cache_on_skip():
    # 复用 test_calc_fast_skip 里 _minimal_db + _all_states_skip 夹具
    modes, fp_cache = preflight_stock_modes_with_fps(
        TS, state_map, daily[TS], weekly.get(TS), dde_d.get(TS), dde_w.get(TS),
    )
    assert modes is not None
    assert all(m == "SKIP" for m, _ in modes.values())
    assert (TS, "weekly", "macd") in fp_cache or ("macd", "weekly") in fp_cache
```

（实现时统一 key 为 `(indicator, freq)` per stock 嵌套 dict：`fp_cache[ts_code][(indicator, freq)]`）

- [ ] **Step 2: 实现 `preflight_stock_modes_with_fps`**

```python
def preflight_stock_modes_with_fps(ts_code, state_map, daily_q, weekly_q, daily_dde, weekly_dde, specs=CALC_ROUTE_SPECS):
    modes = {}
    fps = {}
    for indicator_name, freq, CalcCls, sig_cols, source in specs:
        state = state_map.get((ts_code, freq, indicator_name))
        spec_ver = getattr(CalcCls, "SPEC_VERSION", "v1")
        out = _classify_indicator_preflight_with_fp(...)  # 内部调 classify_calc_mode_detail
        if out is None:
            return None, {}
        mode, new_bars, cur_fp = out
        modes[(indicator_name, freq)] = (mode, new_bars)
        if cur_fp is not None:
            fps[(indicator_name, freq)] = cur_fp
    return modes, fps
```

`preflight_stock_modes_v2` 保留，内部调用 `with_fps` 并丢弃 fps（向后兼容）。

- [ ] **Step 3: 跑 fast_skip 测试**

```bash
pytest tests/test_etl/test_calc_fast_skip.py -v
```

---

## Task 3: 共享 `build_skip_state_records` + batch_append 复用

**Files:**
- Modify: `backend/etl/calc_fast_skip.py`（helper）
- Modify: `backend/etl/calc_batch_append.py:479-530`
- Test: `tests/test_etl/test_batch_append_calc.py`

- [ ] **Step 1: 写失败测试（mock state_signature 调用次数）**

```python
def test_run_batch_append_skip_refresh_reuses_preflight_fp(monkeypatch):
    calls = {"n": 0}
    orig = calc_router.state_signature
    def counted(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    monkeypatch.setattr(calc_router, "state_signature", counted)
    # 跑 run_batch_append_phase 小盘 2 股全 SKIP 夹具
    assert calls["n"] <= 24  # 仅 preflight 一次/指标，skip_refresh 不再翻倍
```

- [ ] **Step 2: 实现 `build_skip_state_records`**

```python
def build_skip_state_records(
    stock_modes, fp_cache_by_stock, state_map, calc_date,
    daily_tails, weekly_tails, dde_daily, dde_weekly,
):
    """Build UPSERT rows for SKIP indicators using preflight fp cache."""
    records = []
    for ts_code, modes in stock_modes.items():
        fps = fp_cache_by_stock.get(ts_code, {})
        for (indicator_name, freq), (mode, _) in modes.items():
            if mode != "SKIP":
                continue
            fp = fps.get((indicator_name, freq))
            if fp is None:
                continue  # fallthrough 不应出现；保守跳过
            st = state_map.get((ts_code, freq, indicator_name))
            if st is None:
                continue
            spec = ...  # CALC_ROUTE_SPECS lookup
            if not CALC_SKIP_STATE_REFRESH or should_refresh_calc_state(st, calc_date, fp):
                records.append((ts_code, freq, indicator_name, st["last_trade_date"], fp, calc_date, None, spec_ver))
    return records
```

- [ ] **Step 3: 改 `run_batch_append_phase`**

```python
fp_cache_by_stock = {}
for ts_code in codes:
    modes, fps = preflight_stock_modes_with_fps(...)
    ...
    fp_cache_by_stock[ts_code] = fps

state_records = build_skip_state_records(
    stock_modes, fp_cache_by_stock, state_map, calc_date,
    daily_tails, weekly_tails, dde_daily, dde_weekly,
)
log_timed_step("calc.batch_state", "skip_refresh", ...)
```

删除原 496-525 双重循环。

- [ ] **Step 4: 加进度（兜底）**

若 `len(state_records) > 1000`，在 `build_skip_state_records` 内每 500 股 `logger.info("progress calc.batch_state: building records %d/%d", ...)`

- [ ] **Step 5: pytest**

```bash
pytest tests/test_etl/test_batch_append_calc.py tests/test_etl/test_calc_fast_skip.py -v
```

---

## Task 4: Orchestrator chunk fast_skip 去重 + skip_log 摘要

**Files:**
- Modify: `backend/etl/orchestrator.py:1027-1063, 1452-1454, 1503-1516`
- Modify: `backend/config.py`
- Test: `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: `CALC_SKIP_LOG_VERBOSE` 配置**

```python
# backend/config.py
# CALC_SKIP_LOG_VERBOSE: 1=每股写入 skip_log；0=同批 fingerprint_match 只写摘要行
CALC_SKIP_LOG_VERBOSE = os.getenv("CALC_SKIP_LOG_VERBOSE", "0").strip() != "0"
```

- [ ] **Step 2: `_write_skip_log_batch` 摘要模式**

```python
def _write_skip_log_batch(con, calc_date, indicator, freq, classified, *, verbose=True):
    if not verbose:
        items = classified.get(SkipReason.FINGERPRINT_MATCH, [])
        if items and len(items) > 100 and len(classified) == 1:
            con.execute(
                """INSERT OR REPLACE INTO ods_calc_skip_log
                   (calc_date, ts_code, indicator, freq, reason, detail)
                   VALUES (?, '__batch__', ?, ?, ?, ?)""",
                [calc_date, indicator, freq, SkipReason.FINGERPRINT_MATCH.value,
                 f"batch_skip={len(items)}"],
            )
            return
    # 原 executemany 路径
```

`run_calc` 调用处传入 `verbose=CALC_SKIP_LOG_VERBOSE`。

- [ ] **Step 3: chunk worker 复用 `build_skip_state_records`**

`_calc_stock_chunk` 内 preflight 后收集 `fp_cache_by_stock`（单 chunk 规模），替换 1058-1063 的 `state_signature` 重算。

- [ ] **Step 4: `chunk=0` COUNT 快路径**

```python
if not chunk_codes and batch_ctx:
    grand_total = sum(
        agg.calculated for agg in batch_ctx.get("agg_by_key", {}).values()
    )
    # 全 SKIP 时 calculated=0；日志行用 batch_ctx 已有 skip 计数，跳过 12×大 IN COUNT
else:
    # 现有 _count_calc_rows 路径
```

- [ ] **Step 5: 测试**

```bash
pytest tests/test_etl/test_orchestrator.py -k "skip_log or batch_append" -v
```

---

## Task 5: 实库验收 + 文档

**Files:**
- Modify: `CLAUDE.md`（进度说明 + `CALC_SKIP_LOG_VERBOSE`）
- Modify: `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md`（附录 D 或 §1.3 预算更新）

- [ ] **Step 1: 全量 pytest**

```bash
pytest tests/ -v
```

- [ ] **Step 2: 实库同日复跑**

```bash
python3 -m backend.cli run --date 20260609 --skip-export
```

**验收标准：**

| 指标 | 优化前 | 目标 |
|------|--------|------|
| preflight→skip_refresh 空窗 | ~140s | **≤5s** |
| batch_append done→calc.stocks 空窗 | ~178s | **≤30s** |
| 总墙钟 | 613s | **≤300s** |
| chunk | 0 | 0 |
| calculated | 0 | 0 |

- [ ] **Step 3: 更新 CLAUDE.md**

在 calc 进度心跳段补充：`batch_preflight` 后不再重复算 fp；`CALC_SKIP_LOG_VERBOSE=0` 默认摘要 skip_log。

- [ ] **Step 4: 更新 pipeline plan**

记录 2026-06-11 实测对比与剩余瓶颈（auto_fetch weekly_fetch ~30s、五步尾窗 ~63s）。

---

## Task 6: M4 真新日 SLA 签字（本 plan 完成后立即接上）

**前置：** Task 1–5 全部 PASS；`dws_calc_state` 已对齐（若期间改过 `calc_dde` 签名路径，先 `refresh-state`）。

**目标日期：** `ODS max + 1` 交易日。实库锚点（2026-06-11）：`ods_max=20260609` → M4 日 **`20260610`**（`dim_date` 下一交易日；非 ODS 已有日）。

**Files:**
- 只读验收：`scripts/benchmark_run.py`、`scripts/health_check.py`
- 更新：`docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md` 附录 B

- [ ] **Step 1: 验收前备份（强制）**

```bash
cp data/tradeanalysis.duckdb "data/tradeanalysis.pre-$(date +%Y%m%d).duckdb"
```

- [ ] **Step 2: 解析 M4 日**

```bash
python3 -c "
from backend.db.connection import get_connection
from backend.etl.calc_gate import get_ods_max_trade_date
con = get_connection()
ods = get_ods_max_trade_date(con)
m4 = con.execute('''
  SELECT MIN(trade_date) FROM dim_date
  WHERE is_trade_day=1 AND trade_date > ?
''', [ods]).fetchone()[0]
print('ods_max', ods, 'm4_date', m4)
con.close()
"
```

- [ ] **Step 3: 实跑 SLA 门禁（真新日 = fetch 会有新 ODS 行）**

```bash
python3 scripts/benchmark_run.py --date <m4_date> --run --skip-export
```

**PASS 条件：**

| 项 | 标准 |
|----|------|
| exit code | 0 |
| 墙钟 | `elapsed ≤ 1800s`（脚本 stderr 无 SLA FAIL） |
| calc | `chunk_stocks < 400`（`ods_etl_log.calc_dws` completeness） |
| 增量路径 | log grep：`dwd.rebuild_incremental` 或 `skipped=true`；**无**全库 rebuild |
| 不做多余算 | `calculated` 以 APPEND 为主；非全市场 FULL chunk |

**监控门禁（实跑中）：** `chunk_stocks≥400` 或 `progress calc.stocks` 持续爬升 → 中止排查；`dde_weekly` 有 chunk 进度即可。

- [ ] **Step 4: 质量体检**

```bash
python3 scripts/health_check.py
```

无 CRITICAL；Section I 成熟股 week-end 截面正常。

- [ ] **Step 5: 同日复跑快路径（M4 日第二次，可选）**

```bash
python3 -m backend.cli run --date <m4_date> --skip-export
```

验证本 plan 优化后墙钟（目标 ≤300s，仍可能 >60s SLA——见 Task 5 注）。

- [ ] **Step 6: 更新附录 B 签字**

在 `2026-06-09-pipeline-30min-optimization.md` 附录 B 填入：`m4_date`、墙钟、`chunk_stocks`、`batch_only`、health_check 结果 → M4 **✅ / ❌**。

```bash
# 签字后只读复核（不重新跑 pipeline）
python3 scripts/benchmark_run.py --date <m4_date>
```

---

## 端到端执行顺序（总览）

```
Task 1–4  代码（fp 缓存 + skip_log 摘要）
    ↓
Task 5    pytest + 同日复跑 20260609 压测（chunk=0）
    ↓
Task 6    ODS max+1 benchmark_run --run --skip-export + health_check
    ↓
附录 B    M4 ✅ 签字 → pipeline 30min plan 闭环
```

---

## 风险与约束

| 风险 | 缓解 |
|------|------|
| fp_cache 与 classify 不一致 | 单函数 `classify_calc_mode_detail` 产出；测试锁定 |
| 摘要 skip_log 丢失逐股审计 | 默认摘要；`CALC_SKIP_LOG_VERBOSE=1` 恢复旧行为 |
| COUNT 快路径日志 row 数不准 | 仅 `chunk=0 and calculated=0` 启用；真写路径仍 COUNT |

**不在本 plan：** `run` 级 fetch 短路（同日复跑 ≤60s SLA）、`weekly_fetch` 门禁减负——另立项。

---

## Self-Review

| Spec 要求 | 对应 Task |
|-----------|-----------|
| 消除 preflight 后 140s 假卡死 | Task 1–3 |
| 消除 batch_append 后 178s 假卡死 | Task 4 |
| 不改 DWS 语义 | 仅路由/日志优化，golden append 测试保留 |
| 可观测 | Task 3 进度 + 现有 StageProgress |
| 同日复跑墙钟下降 | Task 5 实库验收 |
| M4 真新日 ≤30min 签字 | Task 6（接上 pipeline 附录 B） |

**Placeholder 扫描：** 无 TBD。

---

**Plan complete.** 执行顺序：**Task 1–5（silent-gap 代码+同日复跑）→ Task 6（M4 `20260610` benchmark + health_check）→ 附录 B 签字**。

实施方式任选：**Subagent-Driven**（每 Task 子 agent）或 **Inline**（本会话连续）。回复 **「可以，Inline」** 或 **「可以，Subagent」** 后开始 Task 1。
