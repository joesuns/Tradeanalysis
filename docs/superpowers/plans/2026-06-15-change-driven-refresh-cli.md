# 变更驱动 Refresh + CLI 场景化（整合定稿）

**Date:** 2026-06-15  
**Status:** **已实施并合入 main** — Wave 1–5（PR #7 `c1b7649`）+ post-merge 验收补洞（PR #8 `8306575`）  
**Acceptance:** [`2026-06-16-post-merge-acceptance-gates.md`](2026-06-16-post-merge-acceptance-gates.md) · 实库证据 [`evidence/2026-06-16-smoke/`](evidence/2026-06-16-smoke/README.md)  
**Grill 定稿:** Q1=B+SOP · Q2=A+C₂ · Q3=A · Q4=A+combo→B · Q5a=A · Q5b=A · refresh=R1

**评审结论:** 方向正确、增量改造；**P0 修订已并入本文**（短路仍 fetch、FetchResult/PipelineContext）。

---

## 0. 文档目的

将 Grill 会话、用户 8 条要求、数据架构评审、系统架构评审 **合并为单一实施基线**，避免 plan / 讨论 / 现网行为三套叙事。

---

## 1. 目标与符合度

### 1.1 用户 8 条要求 → 设计落点

**验收基线（post-merge）：** [`2026-06-16-post-merge-acceptance-gates.md`](2026-06-16-post-merge-acceptance-gates.md) §4 · CI PR #8 绿 · 实库 [`evidence/2026-06-16-smoke/`](evidence/2026-06-16-smoke/README.md)

| # | 要求 | 落点 | 验收 |
|---|------|------|------|
| 1 | 源端变更 → 仅变更 ODS 重入库 | Task 1 比对 + REPLACE | ✅ `test_ods_diff` + `changed_field_events_count`（PR #8） |
| 2 | ODS 变更 → 下游按范围重算 | changed_codes → DWD 窄链 → calc 路由 | ✅ smoke §2 + `test_column_narrow_equivalence` |
| 3 | 指标 logic 变更 → 该指标重算 | spec 入口 + refresh R1 + `calc --refresh-spec` | ✅ smoke §3 + `test_calc_spec_refresh` |
| 4 | 默认只刷指定交易日；全历史显式 | run/refresh 单日；combo→ops SOP | ✅ `test_cli/test_date_range` + smoke dry-run |
| 5 | 历史数据自动补齐 | 复用 G1/G2 auto-fetch | ✅ CI `pytest tests/ -v`（PR #8） |
| 6 | 分层只写相关表 | stale 子集 DWD；按指标 INSERT | ✅ P0-3 narrow 等价 + Wave5 smoke |
| 7 | 质量优先、不重复算 | pipeline_shortcut + `run --force` 穿透 L0 | ✅ smoke #1 + P0-1 force + 三层短路表 |
| 8 | CLI 易用 | run / export / refresh / calc / ops | ✅ `--help` + refresh-spec 接线（PR #8） |

**§10 双架构正式签字：** 见 post-merge plan §10（用户已批准实施与 merge；架构师签字 ⬜）。

### 1.2 现网差距（实施前）

| 能力 | 现网 | 目标 |
|------|------|------|
| ODS 内容比对 | 已 covered 日 skip，无比对 | fetch 必比对，只写变行 |
| 下游触发 | `n_fetch` / etl step mutation | `FetchResult.changed_*` |
| spec 发版同 day | 幂等 skip 不查 spec stale | 查 `has_spec_stale_indicators` |
| refresh | 无；`--refresh-spec` 文档有、CLI 无 | `cli refresh` R1 |
| 同 day run | fetch+calc 空转 | fetch 比对 + skip DWD+calc |
| 日期范围 | 基本单日 | `--from/--to` |

---

## 2. 命令模型（定稿）

### 2.1 分工

| | **`run`** | **`refresh`** | **`export`** |
|---|-----------|---------------|--------------|
| **意图** | 日常日更 | 指定范围 **必须重算** | 只读 Excel |
| **fetch** | **始终执行**（比对）；0 变行仍调 API | 同左 | 无 |
| **DWD+calc** | 有变更/stale/spec 才算；否则 **短路** | **强制**（R1） | 无 |
| **指标** | 路由：SKIP/APPEND/FULL | 未指定 `--indicator` → **12 路由全 FULL**；指定则仅该指标（日+周） |
| **日期** | `--date` 或 `--from/--to` | 同左 | 同左 |

**12 路由** = `CALC_ROUTE_SPECS` 共 12 项（**6 类指标 × daily/weekly**），对应 12 张 DWS 表。

### 2.2 用户心智

```text
每日报告              →  run [--date D]
再导 Excel / 确定无变  →  export --date D  （或 run 短路后 export）
发版 / 源端疑义 / 修史  →  refresh [--date|--from/--to] [--indicator] [--ts-code]
combo 全历史一致        →  ops repair-* / backfill（显式 SOP，非 run 默认）
```

### 2.3 refresh 存在的理由（相对 run）

`run` **不会**在「无 diff、无 spec stale、无 stale DWD」时重算；以下场景需 `refresh`：

- 检测盲区（如 state spec 已对、DWS 列仍错 — DDE 教训）
- 不信检测、强制重算 scoped 范围
- 历史日期/范围 repair（`--from/--to`）
- 运维 SOP：删 ODS 后强制拉算

---

## 3. 管道架构

### 3.1 PipelineContext（Wave 1 核心契约）

单次 `run` / `refresh` / 范围内每一日均构造 **同一上下文**，各 Step 只读不写语义：

```python
# 概念结构（实施时 dataclass / TypedDict）
PipelineContext:
    analysis_date: str              # 当前处理的交易日 / calc_date
    ts_codes: List[str]             # 目标股集合
    mode: "run" | "refresh"

    # fetch 产出（Task 1）
    api_rows: int
    rows_written: int
    rows_unchanged: int
    changed_pairs: List[Tuple[str, str]]   # (ts_code, trade_date)
    changed_codes_by_date: Dict[str, List[str]]

    # 下游决策
    skip_dwd_calc: bool             # run：True = 跳过 DWD+calc，仍 export
    force_scope: bool               # refresh：True = R1 强制 FULL
    indicator_filter: Optional[List[str]]  # None = 12 路由

    # 观测（写入 ods_etl_log.data_completeness）
    pipeline_shortcut: bool
    changed_codes_count: int
```

**禁止：** cli / orchestrator / calc_gate 各自用 `n_fetch` 或 step 有无 **独立** 推断 mutation。

### 3.2 状态机

```text
PREFLIGHT
  → FETCH（run/refresh 必达；比对 ODS）
  → [skip_dwd_calc?] ──yes──→ EXPORT? → DONE
  → no ↓
  DWD（rebuild_dwd_for_stale(changed_codes, date)）
  → REFRESH_STATE?（DWD 有写入时，现有 maybe_refresh_state）
  → CALC（run=路由；refresh=force_scope + indicator_filter）
  → EXPORT? → DONE
```

**短路定义（P0-1A，定稿）：**

- **仍执行 FETCH**（发现 tushare silent 改数）
- `rows_written==0` 且 `not spec_stale` 且 `stale_dwd 空` 且 `has_prior_calc_snapshot(date)` → `skip_dwd_calc=True`
- **不 skip export**（除非 `--skip-export`）

### 3.3 run 流程

```
preflight_run(con, date, ts_codes)  # 仅检查 prior calc 等，不替代 fetch
→ fetch_with_diff → PipelineContext
→ if ctx.skip_dwd_calc: log pipeline_shortcut=true → export → DONE
→ rebuild_dwd_for_stale(changed_codes, date)
→ refresh_state?（现有逻辑）
→ run_calc（路由；spec stale 不 idempotent skip — Task 3）
→ export
```

### 3.4 refresh 流程（R1）

```
resolve scope: dates × ts_codes × indicators（None=12 路由）
for each trade_date in dates:
    fetch_with_diff
    → DWD（changed_codes）
    → run_calc(force_scope=True, indicator_filter=..., calc_date=trade_date)
    → export?（每 day 独立文件）
```

**Q4 语义：**

- **单日 run / refresh `--date X`：** DWS 写入 `calc_date=X` 批次；修正 T<X 的 ODS 只影响 **X 截面** latest，不回写历史 calc_date 批次。
- **范围 refresh `--from A --to B`：** 每个交易日 **各自 calc_date=该日**（显式重算历史截面）；combo 全历史优先 **ops repair + oracle**。

---

## 4. 数据层设计

### 4.1 ODS 比对（Q1=B）

**粒度:** `(ts_code, trade_date)` × 业务列。

| 表 | 比对列 |
|----|--------|
| `ods_daily` | open, high, low, close, vol, amount, pct_chg, adj_factor |
| `ods_daily_basic` | circ_mv 等写入列（与 `_daily_basic_record` 一致） |
| `ods_moneyflow` | 与 `_moneyflow_record` 一致（含 net_amount_dc 路径） |

**规则:**

1. PK 不存在 → INSERT（计入 `rows_written` + `changed_pairs`）
2. PK 存在、列有 diff → REPLACE
3. 浮点：项目 `safe_float` / `atol=1e-9` golden
4. **不比对** `fetched_at`

**SOP 兜底:** 删 ODS 行 → `refresh`；health_check 抽样（二期）。

### 4.2 DWD 窄链（Q2=A）

- `changed_codes_by_date[date]` → `rebuild_dwd_for_stale(codes, date)`（批量合并，非 per-code 循环调 API）
- adj 变 → `find_stocks_needing_qfq_refresh` → **`refresh_qfq_prices(ts_codes)` 仅变更股全历史 qfq UPDATE**
- **禁止** 日常无 `ts_codes` 的 `rebuild_all_dwd()`

**二期 C（Task 7）：** `ods_changed_columns` → `INDICATOR_COLUMN_DEPS` 缩小 **run** 路径 calc；**refresh R1 不受 C 影响**。

### 4.3 calc

| 路径 | 行为 |
|------|------|
| **run** | `classify_calc_mode` + spec check；`_should_skip_calc_idempotent` 含 `has_spec_stale_indicators` |
| **refresh** | bypass `try_force_same_day_batch_shortcut`；范围内 **强制 FULL**；仍 **窄窗 recalc_start（255）**，非全历史 DWS 扫库 |

**DWS 写入:** INSERT-only，`calc_date` = 当前分析日；`insert_dws_batch` 不变。

### 4.4 两类变更（正交）

| 变更 | 发现 | 刷新 |
|------|------|------|
| 源数据 | ODS fetch diff | DWD 窄链 → 指纹路由 |
| 指标 spec | `SPEC_VERSION` vs `dws_calc_state` | 该指标窄窗 FULL（run）；refresh 按 R1 |

---

## 5. CLI 设计（5b）

### 5.1 命令面

```bash
# L1 默认
python -m backend.cli run [--date D] [--from A --to B] [--ts-code ...] [--skip-export]
python -m backend.cli export [--date D] [--from A --to B] [--ts-code ...]
python -m backend.cli refresh [--date D] [--from A --to B] [--indicator ma[,dde,...]] [--ts-code ...] [--export] [--dry-run] [--confirm]

# L2 调试（保留顶层）
python -m backend.cli fetch | calc | query | check | status

# ops（迁入 subparser；顶层别名保留 1 release + DeprecationWarning）
python -m backend.cli ops repair-dde-trend ...
python -m backend.cli ops backfill-dde-meta ...
python -m backend.cli ops backfill-state | refresh-state | repair-weekly | prune
# 兼容: python -m backend.cli repair-dde-trend ...  →  alias
```

### 5.2 日期范围

- `--date` 与 `--from/--to` 互斥
- 经 `dim_date.is_trade_day=1` 展开；端点各 `_ensure_trade_date` 一次
- `run --from A --to B`：逐日顺序 PipelineContext；**fail-fast**；可选 `--continue-on-error`
- `export --from/--to`：多文件 `exports/analysis_{date}_gen{now}.xlsx`

### 5.3 refresh 护栏（系统 mandatory）

| 机制 | 说明 |
|------|------|
| `--dry-run` | 输出 `{dates, n_stocks, indicators, est_route_count}`，无 API/写库 |
| 规模 WARN | 如 `len(dates)*len(stocks)*12 > 阈值` 需 `--confirm` |
| cron | **仅绑** `run --date today`；范围 refresh 禁止默认定时 |

---

## 6. 观测契约（ods_etl_log）

`data_completeness` 统一字段（run_fetch / run_rebuild_dwd / calc_dws / run_refresh）：

| 字段 | 含义 |
|------|------|
| `ods_api_rows` | API 返回行数 |
| `ods_rows_written` | 实际写入 |
| `ods_rows_unchanged` | 比对相同 |
| `changed_codes_count` | 触发 DWD 股数 |
| `pipeline_shortcut` | true = 跳过 DWD+calc |
| `date_range_progress` | 范围模式 `{ok:[], failed:[]}` |

---

## 7. 受影响模块

| 模块 | 变更 |
|------|------|
| `backend/fetch/ods_daily.py` | diff、`FetchResult`、三表比对 |
| `backend/etl/pipeline_context.py` | **新建** PipelineContext |
| `backend/etl/orchestrator.py` | preflight、refresh runner、idempotent spec、calc 接线 |
| `backend/etl/calc_gate.py` | mutation 读 context / rows_written |
| `backend/etl/calc_batch_append.py` | force_scope bypass shortcut |
| `backend/etl/calc_spec_refresh.py` | 并入 refresh（thin wrapper 可选） |
| `backend/cli.py` | run/refresh/export 范围、ops、别名 |
| `backend/export_wide.py` | 范围 export 循环 |
| `tests/test_fetch/test_ods_diff.py` | **新建** |
| `tests/test_etl/test_pipeline_context.py` | **新建** |
| `tests/test_etl/test_orchestrator.py` | spec、shortcut、refresh |
| `tests/test_cli/test_refresh_run.py` | **新建** |
| `CLAUDE.md` / `daily-runbook.md` / data-model spec | CLI、SOP、combo B |

**不改:** Calculator 算法、schema DDL、export 列定义。

---

## 8. 实施波次（评审后顺序）

### Wave 1 — 核心契约（P0）

**Task 1 — ODS diff + FetchResult**

- [x] `diff_ods_*` 三表 + 浮点 golden
- [x] `fetch_by_date_range_parallel` / `fetch_stocks_incremental` 返回 `FetchResult`
- [x] pytest：同内容 0 write；变更 write；新行 INSERT

**Task 1b — PipelineContext + 下游触发**

- [x] `pipeline_context.py`；cli/orchestrator 构造 context
- [x] DWD：`changed_codes_by_date`，**不再**用 `n_fetch`  alone
- [x] `data_mutated_since_last_calc` 或 run 内闸门：`rows_written>0 | spec_stale | stale_dwd | 新 bar`

**Task 3 — spec stale 入口**

- [x] `_should_skip_calc_idempotent` 非 force 也查 `has_spec_stale_indicators`
- [x] pytest

### Wave 2 — 日常 run（P0）

**Task 2 — run 智能短路（P0-1A）**

- [x] 仍 fetch；`skip_dwd_calc` 时 skip DWD+calc
- [x] `ods_etl_log` 观测字段
- [x] pytest：同 day 0 diff → 无 DWD/calc log，有 export

### Wave 3 — refresh（P0）

**Task 4 — cli refresh + R1**

- [x] `run_refresh_pipeline`；`force_scope` + `indicator_filter`
- [x] `--dry-run` / `--confirm`
- [x] pytest：仅 ma；12 路由

### Wave 4 — 体验（P1）

**Task 5 — 日期范围** run/export/refresh + fail-fast

- [x] `--from/--to` 与 `--date` 互斥；dim_date 展开
- [x] run/refresh fail-fast；`--continue-on-error` 可选
- [x] export 范围多文件
- [x] `date_range_progress` 观测

**Task 6 — ops 子命令 + 顶层别名 + 文档**

- [x] `cli ops` 子命令（backfill-state / refresh-state / prune / repair-weekly / backfill-dde-meta / repair-dde-trend）
- [x] 顶层别名 + DeprecationWarning
- [x] CLAUDE.md 更新

### Wave 5 — 优化（P2）

**Task 7 — 列→指标映射（run 路径）**

> 详细实施计划：[`2026-06-15-wave5-column-indicator-deps.md`](2026-06-15-wave5-column-indicator-deps.md)

- [x] Task 1–3：`ods_diff` 列事件 + `FetchResult` + D2 patch 事件
- [x] Task 4：`column_indicator_deps.py` 映射 + 保守 fallback G1–G8
- [x] Task 5：`PipelineContext` / run→calc handoff
- [x] Task 6：`run_batch_append_phase` + `run_calc` 路由收窄
- [x] Task 7：config / 观测 / CLAUDE.md
- [x] Task 8：smoke #7 + 全量 pytest

**refresh R1 不受 Task 7 影响。**

---

## 9. 测试与验收

```bash
pytest tests/test_fetch/test_ods_diff.py tests/test_etl/test_pipeline_context.py \
       tests/test_etl/test_orchestrator.py tests/test_cli/test_refresh_run.py -v
pytest tests/ -v
```

**实库 smoke:**

1. 同 day 二次 `run`：有 fetch log，`pipeline_shortcut=true`，export OK  
2. 手动改 ODS 一行 → `rows_written=1` → DWD+calc 连锁  
3. bump ma SPEC_VERSION → `run` 仅 ma FULL  
4. `refresh --date X --indicator ma` → 仅 dws_ma_*  
5. `refresh --date X`（无 indicator）→ 12 路由均有写入  
6. `refresh --dry-run --from A --to B` → 规模预估，无写库  

---

## 10. 风险

| 风险 | 缓解 |
|------|------|
| 短路 skip fetch（旧 plan） | **已废止**；定稿 P0-1A |
| refresh 范围墙钟 | dry-run + confirm；combo 用 ops |
| 浮点误 diff | atol golden |
| ops 迁移断脚本 | 顶层别名 1 release |
| state 对 DWS 错 | refresh force；专用 repair + oracle |

---

## 11. 决策日志（Grill + 评审）

| 决策 | 来源 |
|------|------|
| ODS 比对只写变行 + SOP | Grill Q1 |
| DWD 窄链；二期列→指标 | Grill Q2 |
| spec stale 破 idempotent skip | Grill Q3 |
| 默认只 calc_date 截面；combo ops B | Grill Q4 |
| 短路 skip DWD+calc **不 skip fetch** | 数据+系统架构 P0-1A |
| FetchResult / PipelineContext | 数据+系统架构 P0-2 |
| run / export / refresh / ops | Grill Q5b |
| refresh R1：无 indicator → 12 路由 FULL | Grill R1 |
| refresh 不分 source/spec，整条链路 | 用户补充 |
| 日期范围 run/export/refresh | 用户补充 |

---

## 12. 审批

- [x] 用户批准本整合定稿（2026-06-15）
- [x] Wave 1–5 实施并合入 main — **PR #7** `c1b7649`
- [x] Post-merge 验收补洞合入 main — **PR #8** `8306575`（[`2026-06-16-post-merge-acceptance-gates.md`](2026-06-16-post-merge-acceptance-gates.md)）
- [ ] 双架构 §10 正式签字（不阻塞 main 代码；见 post-merge plan）

**Explicitly out of scope:** combo 全历史自动 backfill；content-aware mutation v2（etl_log 级，非 ODS diff）。
