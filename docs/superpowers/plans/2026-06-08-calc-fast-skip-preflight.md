# Calc 同日复跑 State 前置短路（Chunk Batch Preflight）

- **日期：** 2026-06-08
- **状态：** 已完成
- **类型：** 性能优化（calc 同日复跑）
- **前置依赖：** CALC_APPEND + `dws_calc_state` 已上线；fetch 停牌缺口跳过已验收（324s→11s）
- **实库基线：** `calc --date 20260602` 第二次同日复跑 calc 本体 **834s**，全部 `0 calculated / fingerprint_match`
- **架构评审：** 2026-06-08 有条件通过；本节修订已吸收评审意见（尾窗上界、缺股 fallthrough、DDE weekly 等价、skip 聚合）

## 一、问题与根因

### 现象

同 `calc_date` 重跑时，APPEND 路由已正确走 **SKIP**（不算、不写 DWS），但 calc 仍要 **~14 分钟**。

### 根因（per-stock 固定开销）

`calc_stock_pipeline` 对**每只股票**无论最终 SKIP/FULL，都先执行：

```
load_quote_groups(daily)    # ~255 bar 窄窗（仅 start_date 下界，无上界）
load_quote_groups(weekly)   # week-end 窄窗
DDE._load_daily_batch       # moneyflow 窄窗
DDE._load_weekly_batch      # 周线聚合窄窗
load_calc_state × 12        # 每指标一次 SELECT
classify_calc_mode × 12     # 内存签名
```

5388 股 × 上述 4 次批量读 + 12 次 state 查询 ≈ **834s**（~7.8 stk/s）。瓶颈是 **I/O 次数**，不是计算量。

### 实库关键事实（影响尾窗设计）

慢路径 `load_quote_groups` **无 `trade_date <= calc_date` 上界**，会加载 DWD 全尾数据。

实库样本 `000001.SZ`：`calc_date=20260602`，但 `dws_calc_state.last_trade_date=20260605`（state 写入时用 `df["trade_date"].max()`，可领先于 `calc_date`）。  
`classify_calc_mode` 签名域锚在 **`state.last_trade_date`**，不是 `calc_date`：

```python
last_td = state["last_trade_date"]
cur_fp = state_signature(df, last_td, sig_cols)   # hist = df[td<=last_td].tail(245)
new_bars = df[df["trade_date"] > last_td]...
```

故 preflight 尾窗 **禁止** 用 `trade_date <= calc_date` 截断，否则签名域不足 → 误判 FULL → fast_skip 失效。

### 不能用的捷径（数据质量红线）

仅用 `updated_calc_date == calc_date` 整股跳过 **不安全**：

- 同日复跑之间若发生 `fetch → rebuild DWD`（除权复权、停牌填充修正），`history_fp` 会变但 `updated_calc_date` 不变 → **误 SKIP → 脏数据**。
- 故：**必须保留与 `_route_calc` 等价的 `classify_calc_mode` 签名判定**，只是把「每股 4 次读」降为「每 chunk 4~5 次读」。

## 二、方案：Chunk Batch Preflight

在 `_calc_stock_chunk` 的 per-stock 循环**之前**，对整块 chunk（~674 股）做一次批量预检：

```
┌─ chunk 入口 ─────────────────────────────────────────────┐
│ 1. batch_load_calc_state(chunk, 12 indicators)  [1 SQL] │
│ 2. batch_load_quote_tails(chunk, daily, 245)    [1 SQL] │
│ 3. batch_load_quote_tails(chunk, weekly, 245)   [1 SQL] │
│ 4. batch_load_dde_tails(chunk, daily, 245)      [1 SQL] │
│ 5. batch_load_dde_tails(chunk, weekly, 245)     [1 SQL] │
└─────────────────────────────────────────────────────────┘
         │
         ▼  per stock（纯内存，无 SQL）
┌─ classify_stock_preflight ────────────────────────────────┐
│  缺股 / 空帧 → 直接 fallthrough（等同慢路径 FULL 入口）    │
│  否则对 12 个 (indicator, freq) 调 classify_calc_mode    │
│  ALL → SKIP ?  fast_skip :  fallthrough                 │
└─────────────────────────────────────────────────────────┘
         │
    fast_skip ──► 12× fingerprint_match 记入 agg_by_key，不调 calc_stock_pipeline
    fallthrough ─► 现有 calc_stock_pipeline（行为不变）
```

### 尾窗加载原则（修订后）

| 规则 | 说明 |
|---|---|
| **无 calc_date 上界** | 每股取 DWD **全历史最新** `SIG_WINDOW(245)` 根（`ORDER BY trade_date DESC` + `rn <= 245`） |
| daily quote | `dwd_daily_quote`，`is_suspended=0` |
| weekly quote | `dwd_weekly_quote` + `JOIN dim_date is_week_end=1` |
| DDE daily | 复用 `_load_daily_batch` 同源 JOIN，再对每股 `tail(245)` |
| DDE weekly | **必须**复用 `_load_weekly_batch` 的 CTE 聚合结果，再对每股 `tail(245)`；禁止对日线表简单 ROW_NUMBER 替代 |
| 列集 | quote 用各指标 `SIGNATURE_COLS` 并集；DDE 用其 7 列 |

### Fallthrough 条件（与慢路径 `_route_calc` 对齐）

以下任一成立 → **禁止 fast_skip**，走 `calc_stock_pipeline`：

1. `CALC_FAST_SKIP=0` 或 `CALC_APPEND=0` 或 `incremental=False`
2. 该股在 batch 结果中**缺失**（slow path：`df is None`）
3. 任一 source 帧 `len(df)==0`（slow path：走 FULL）
4. 任一指标 `state is None`（slow path：FULL）
5. 任一指标 `classify_calc_mode` 返回非 `SKIP`（APPEND / FULL）

### 数据质量保障（与现路径等价）

| 保障 | 说明 |
|---|---|
| 签名逻辑零改动 | 仍用 `classify_calc_mode` + `state_signature` + `SIG_WINDOW=245` |
| 尾窗语义对齐 | 无 `calc_date` 上界；覆盖 `last_td > calc_date` 实库场景 |
| 周线/DDE 过滤一致 | 与 `load_quote_groups` / `_load_*_batch` 相同 JOIN 与聚合 |
| 不等价即 fallthrough | 误判方向为「多算」而非「漏算」，安全 |
| Golden 测试锁定 | fast 路径 skip 集合 == 慢路径 skip 集合 |

### 预期性能

| 场景 | 修复前 | 修复后（估） |
|---|---|---|
| 同日复跑（全 SKIP）calc 本体 | ~834s | **20–60s**（8 chunk × 5 batch SQL + 内存 classify） |
| 全链路 `cli calc` | fetch+completeness+calc | calc 段达标即可；fetch 已 11s |
| 新交易日（全 APPEND） | 分钟级 | **不变**（几乎全部 fallthrough） |
| 混合（部分 SKIP） | — | 仅全 SKIP 股受益 |

> 「秒级」指 **calc 本体**；DDE weekly batch 仍是 chunk 内最重 SQL，20–40s 为合理目标，<10s 需二期（跨 chunk 合并 DDE weekly）。

## 三、实施步骤（TDD）

### Task 1 — 指标注册表 `calc_indicators.py`

集中导出 12 个 `(indicator_name, freq, CalcCls, SIGNATURE_COLS, source)` 元数据。

```python
DDE_SIG_COLS = [
    "buy_lg_vol", "sell_lg_vol", "buy_elg_vol", "sell_elg_vol",
    "total_vol", "net_mf_amount", "close_qfq",
]

CALC_ROUTE_SPECS = [
    ("macd", "daily", MACDCalculator, ["close_qfq"], "quote"),
    ("macd", "weekly", MACDCalculator, ["close_qfq"], "quote"),
    ("ma", "daily", MACalculator, ["close_qfq"], "quote"),
    ("ma", "weekly", MACalculator, ["close_qfq"], "quote"),
    ("kpattern", "daily", KPatternCalculator,
     ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol"], "quote"),
    ("kpattern", "weekly", KPatternCalculator,
     ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol"], "quote"),
    ("volume", "daily", VolumeCalculator, ["close_qfq", "vol"], "quote"),
    ("volume", "weekly", VolumeCalculator, ["close_qfq", "vol"], "quote"),
    ("priceposition", "daily", PricePositionCalculator, ["close_qfq"], "quote"),
    ("priceposition", "weekly", PricePositionCalculator, ["close_qfq"], "quote"),
    ("dde", "daily", DDECalculator, DDE_SIG_COLS, "dde"),
    ("dde", "weekly", DDECalculator, DDE_SIG_COLS, "dde"),
]

def quote_sig_col_union() -> list[str]:
    """SIGNATURE_COLS 并集，供 batch_load_quote_tails 选列。"""
```

指标名与 orchestrator 一致（`priceposition` 无下划线）。

### Task 2 — `calc_state.py` 批量读

```python
def load_calc_state_batch(con, ts_codes, specs) -> dict:
    """{(ts_code, freq, indicator): {last_trade_date, history_fp, quote_latest_adj, updated_calc_date}}"""
```

一次 SQL：`WHERE ts_code IN (...) AND (freq, indicator) IN specs 展开`。  
`updated_calc_date` 仅日志/诊断，**不作跳过条件**。

### Task 3 — `calc_fast_skip.py` 尾窗批量加载

```python
def batch_load_quote_tails(con, ts_codes, freq, columns, window=245) -> dict:
    """{ts_code: DataFrame}，每股 DWD 最新 window 根，无 calc_date 上界。"""

def batch_load_dde_tails(con, ts_codes, freq, window=245) -> dict:
    """daily: 复用 DDE _load_daily_batch 后 tail(window)
       weekly: 复用 DDE _load_weekly_batch 后 tail(window)"""

def preflight_stock_modes(ts_code, state_map, daily_q, weekly_q,
                          daily_dde, weekly_dde, specs) -> Optional[dict]:
    """返回 {(indicator,freq): (mode, new_bars)}；缺帧/空帧返回 None → fallthrough。"""

def stock_can_fast_skip(modes: dict) -> bool:
    return all(m == "SKIP" for m, _ in modes.values())
```

**尾窗 SQL 要点（quote daily，修订后）：**

```sql
WITH ranked AS (
  SELECT ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, pct_chg,
         ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
  FROM dwd_daily_quote
  WHERE ts_code IN (...) AND is_suspended = 0
  -- 注意：无 trade_date <= calc_date
)
SELECT * FROM ranked WHERE rn <= 245 ORDER BY ts_code, trade_date
```

weekly quote 同理，加 `JOIN dim_date ... is_week_end=1`。  
DDE weekly：**调用现有 `_load_weekly_batch` 逻辑**（或抽取共享 SQL 函数），对返回帧 `tail(245)`。

### Task 4 — 接入 `_calc_stock_chunk`（`orchestrator.py`）

```python
from backend.config import CALC_FAST_SKIP, CALC_APPEND

fast_on = CALC_FAST_SKIP and CALC_APPEND and incremental
fast_skip_count = 0

if fast_on:
    state_map = load_calc_state_batch(con, chunk, CALC_ROUTE_SPECS)
    sig_cols = quote_sig_col_union()
    daily_tails = batch_load_quote_tails(con, chunk, "daily", sig_cols)
    weekly_tails = batch_load_quote_tails(con, chunk, "weekly", sig_cols)
    dde_daily = batch_load_dde_tails(con, chunk, "daily")
    dde_weekly = batch_load_dde_tails(con, chunk, "weekly")

for ts_code in chunk:
    modes = None
    if fast_on:
        modes = preflight_stock_modes(
            ts_code, state_map,
            daily_tails.get(ts_code), weekly_tails.get(ts_code),
            dde_daily.get(ts_code), dde_weekly.get(ts_code),
            CALC_ROUTE_SPECS,
        )
    if modes is not None and stock_can_fast_skip(modes):
        for indicator_name, freq, _, _, _ in CALC_ROUTE_SPECS:
            agg = agg_by_key[(indicator_name, freq)]
            agg.add_skip(SkipReason.FINGERPRINT_MATCH, ts_code,
                         "fast_skip: preflight")
        fast_skip_count += 1
        _report_calc_progress()
        continue

    for indicator_name, freq, result in calc_stock_pipeline(
            con, ts_code, calc_date, daily_recalc, weekly_recalc):
        # 原逻辑不变
        ...

if fast_on:
    logger.info("calc fast_skip: %d/%d stocks in chunk", fast_skip_count, len(chunk))
```

### Task 5 — 配置 `config.py`

```python
# CALC_FAST_SKIP: chunk batch preflight for same-day SKIP (default on with CALC_APPEND)
CALC_FAST_SKIP = os.getenv("CALC_FAST_SKIP", "1").strip() != "0"
```

`CALC_APPEND=0` 或 `CALC_FAST_SKIP=0` 时回退现路径。

### Task 6 — 测试 `tests/test_etl/test_calc_fast_skip.py`

**路由逻辑：**

1. `test_stock_can_fast_skip_all_skip`
2. `test_fallthrough_on_append` / `test_fallthrough_on_full`
3. `test_fallthrough_on_missing_stock`（batch 无该股 → None → 不 fast_skip）
4. `test_fallthrough_on_empty_dde_frame`（BSE 等空帧 → fallthrough）

**尾窗等价（golden）：**

5. `test_batch_quote_tails_tail_matches_slow_path`（尾 245 行 == `load_quote_groups` 结果 `.tail(245)`，同过滤）
6. `test_dde_weekly_tail_matches_load_weekly_batch`（DDE 周线聚合帧 tail 一致）
7. `test_fast_skip_state_ahead_of_calc_date`（`last_td=20260605, calc_date=20260602` 仍正确 SKIP）

**端到端：**

8. `test_fast_skip_equivalent_to_slow_path`（同 fixture，fast/slow 的 per-stock per-indicator mode 一致）
9. `test_fast_skip_unsafe_on_dwd_change`（改 `close_qfq` → 非 SKIP → fallthrough 重算）

### Task 7 — 实库验收

```bash
python3 -m backend.cli calc --date 20260602   # 第三次同日复跑
```

| 检查项 | 预期 |
|---|---|
| 日志 `calc fast_skip` | ~5388 股（或接近，扣除缺 state 新股） |
| calc 本体 | **< 60s** |
| `0 calculated` + `fingerprint_match` | 与修复前一致 |
| DWS 行数 | 不新增 `calc_date=20260602` 快照 |

回归：

```bash
CALC_FAST_SKIP=0 python3 -m backend.cli calc --date 20260602   # 仍 ~14min
```

新日 APPEND 不受影响（可选）：

```bash
python3 -m backend.cli fetch
python3 -m backend.cli calc --date 20260608
```

### Task 8 — 文档

- `CLAUDE.md`：calc 段补充 `CALC_FAST_SKIP` + chunk preflight + 尾窗无 calc_date 上界说明
- `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`：配置项表
- 本 plan 状态 → 已完成

## 四、风险与边界

| 风险 | 缓解 |
|---|---|
| 尾窗 SQL 与慢路径列/过滤不一致 | Task 6 #5/#6 golden；含 `last_td > calc_date` 用例 |
| 缺股/空帧误 fast_skip | `preflight_stock_modes` 返回 None → fallthrough |
| DDE weekly 聚合路径不一致 | 强制复用 `_load_weekly_batch`，单测 #6 |
| 部分指标 SKIP、部分 APPEND 仍整股 fallthrough | 接受；新日场景本就需要 |
| DDE weekly batch 仍重 | 每 chunk 1 次；全 SKIP 场景主要成本，可接受 |
| 内存 674×245×列 | ~数 MB/chunk，可接受 |

## 五、不做（本期范围外）

- 指标级 partial skip（11 SKIP + 1 APPEND 仍整股 fallthrough）
- `updated_calc_date` 纯元数据跳过
- 跨 chunk 全局 batch / DDE weekly 懒加载
- 修改 `classify_calc_mode` / `state_signature` 语义
- 重构 `calc_stock_pipeline` 改用 `CALC_ROUTE_SPECS`（可选后续，本期仅 chunk 入口接入）

## 六、验收标准

- [x] Golden：fast == slow 路由结果（含 `last_td > calc_date`）
- [x] DWD 变更后不误 SKIP（`test_fast_skip_unsafe_on_dwd_change`）
- [x] 缺股/空帧 fallthrough（`test_fallthrough_on_missing_stock`）
- [x] 全量 `pytest tests/ -v` 通过（386 passed）
- [x] 实库 calc 630s（未达 <60s，触发 v2）
- [x] fast_skip 日志可见（349/674 ~ 59/674 per chunk）

## 七、架构评审修订记录

| 修订项 | 原稿问题 | 修订 |
|---|---|---|
| 尾窗上界 | `trade_date <= calc_date` 与慢路径不等价 | 去掉上界，取 DWD 最新 245 根 |
| 缺股处理 | 未写明 | 缺失/空帧 → fallthrough |
| DDE weekly | 暗示简单 ROW_NUMBER | 强制 `_load_weekly_batch` + tail |
| skip 聚合 | 伪代码 `agg.add_skip` 结构错误 | 写入 `agg_by_key[(indicator,freq)]` |
| 性能表述 | 「秒级」易误解全链路 | 明确 calc 本体 20–60s |
