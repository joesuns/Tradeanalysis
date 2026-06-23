# [CLAUDE.md](http://CLAUDE.md)

## 沟通规范

- **默认中文**：所有交互使用中文，代码/术语可中英混杂
- **代码任务，必须遵循：**
  ```markdown
  ### 1. 坚实准确的专业知识
  - 结论必须能追溯到 spec / CLAUDE.md / 代码逻辑 / 学术公式
  - 不确定的判断标注置信度和前提假设
  - 禁止以确定语气陈述未经验证的推测

  ### 2. 客观真实的论据
  - 数值建议：结论前先列出全量证据（所有相关源文件中的硬编码数值）
  - 看到列表不全 → 结论不可信
  - 看到列表完整 → 结论基于事实

  ### 3.诚实不猜测
  - 不确定的模块：直接说"这里我不确定，需要调研"，禁止跳过、猜测或假设

  ### 4.计算约束
  - 质量>速度、最小范围、DWD/Calc 决策树、禁止习惯性全库 rebuild
  ```

## 项目概述

Tradeanalysis — 基于 tushare + DuckDB 的 A 股技术分析数据管道。拉取全市场 OHLCV/资金流/PE 等数据，计算 MACD/MA/K线形态/DDE/量能/价格位置六大类技术指标（日线+周线），通过 FastAPI 查询、CLI 导出 Excel。

- **数据源：** tushare Pro（6200 积分）
- **数据范围：** 全 A 股 5000+ 只，覆盖 2015 年至今
- **Spec：** `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` (v1.12)
- **日常运维 Runbook：** `docs/superpowers/plans/2026-06-09-daily-runbook.md`
- **信号优化方案：** `docs/superpowers/plans/2026-06-02-signal-timing-optimization.md`
- **CLI 重构方案：** `docs/superpowers/plans/2026-06-04-cli-fetch-calc-export-fingerprint.md`

## 知识图谱 (codebase-memory MCP)

项目已索引到知识图谱（4,172 节点 / 13,485 边），**以下场景优先用图谱工具而非文件搜索**：

| 场景 | 工具 | 示例 |
|------|------|------|
| 查找函数/类定义 | `search_graph(query=..., project="Users-joesun-Trae-Tradeanalysis")` | `search_graph(query="MACDCalculator calculate")` |
| 追踪调用链 | `trace_path(function_name=..., project=...)` | `trace_path(function_name="rebuild_dwd_for_stale", depth=3)` |
| 代码文本搜索 | `search_code(pattern=..., project=...)` | `search_code(pattern="adj_factor", file_pattern="*.py")` |
| 架构/热点/簇概览 | `get_architecture(project=..., aspects=['all'])` | 查看热点函数、代码簇、分层 |
| 复杂度/热点查询 | `query_graph(query="MATCH (f:Function) ...", project=...)` | 查 `transitive_loop_depth >= 3` 的函数 |
| 查看源码 | `get_code_snippet(qualified_name=..., project=...)` | 先 `search_graph` 拿到 qualified_name 再查 |

**覆盖范围：** `backend/`, `scripts/`, `tests/`（排除 `.git`, `venv`, `data`, `.pytest_cache`, `.claude`）

> 图谱用于**定位代码**；领域知识（公式、参数、设计意图、硬约束）仍以本文为准。

## 🚫 硬约束（违反必错）

1. 🚫 **禁止日常全库 `rebuild_all_dwd`**（无 `ts_codes` 参数）——日常新日必须走 `rebuild_dwd_for_stale`；全库 rebuild 仅限运维/首次建库
2. 🚫 **禁止单独调用 `build_dwd_*` 函数**——统一通过 `rebuild_all_dwd` 或 `rebuild_dwd_for_stale` 入口
3. 🚫 **所有查询必须走 `v_*_latest` 视图**——禁止直接查 DWS 基表
4. 🚫 **周划分禁用 `strftime('%Y-%W')`**——必须用 `date_trunc('week', dt)`（周一锚点），否则跨年自然周会被切成两段
5. 🚫 **禁止 `multiprocessing.Pool`**——DuckDB 单文件仅允许一个 read-write 进程，用 `ThreadPoolExecutor`
6. 🚫 **禁止 `X | None` 类型语法**——Python 3.9 不支持，必须用 `Optional[X]`
7. 🚫 **周线计算器必须 `JOIN dim_date WHERE is_week_end=1`**——禁止直接查滚动 bar，否则产出 intra-week 幽灵行
8. 🚫 **周线 DDE trend 禁止 `net_mf_amount`/`total_mv` 回退**——必须 `resample('W')` 独立聚合仅 `net_amount_dc`+`circ_mv` 再 inner merge
9. 🚫 **指数 calc 禁止走个股 `classify_calc_mode` 路由**——必须用 `IndexMACDCalculator` 等适配器（`calc_index_pipeline` 入口）

## 高频任务速查


| 任务         | 命令                                                             | 关键说明                             |
| ---------- | -------------------------------------------------------------- | -------------------------------- |
| 新日全市场分析    | `python -m backend.cli run`                                    | 一条命令：fetch → DWD → calc → export |
| 同日复跑（不重算）  | `python -m backend.cli run --date 20260605`                    | L0 快路径自动跳过 DWD+calc              |
| 同日复跑（强制）   | `python -m backend.cli run --date 20260605 --force`            | 穿透 L0，DWD 仅窄重建                   |
| 单指标全量刷新    | `python -m backend.cli refresh --date 20260612 --indicator ma` | 全链路 R1；日常 run/calc 已 auto        |
| 仅导出 Excel  | `python -m backend.cli export --date 20260605`                 | 从 latest 视图直接导出，不重算              |
| 指定股票 fetch | `python -m backend.cli fetch --ts-code 000543.SZ 600580.SH`    | stock-batched per-stock 增量       |
| 全链路健康检查    | `python -m scripts.health_check`                               | 只读，跑批后执行                         |


## 技术栈

- **语言：** Python ≥3.9（不使用 `list[str] | None`，用 `Optional[list[str]]`）
- **存储：** DuckDB ≥1.0（持久化文件 `./data/tradeanalysis.duckdb`）
- **数据源：** tushare Pro（`TUSHARE_TOKEN` 环境变量）
- **API：** FastAPI + uvicorn / **Excel：** openpyxl / **测试：** pytest + httpx
- 包依赖详情：`get_architecture(aspects=['packages'])` 查知识图谱

## 常用命令

```bash
# 测试
pytest tests/ -v

# ===== 每日分析（一条命令） =====
python -m backend.cli run                        # 全市场，最近交易日
python -m backend.cli run --date 20260605              # 同日复跑
python -m backend.cli run --date 20260605 --skip-export  # 同日复跑不导 Excel

# ===== 强制重算（refresh R1）=====
python -m backend.cli refresh --date 20260612                    # 12 路由全 FULL
python -m backend.cli refresh --date 20260612 --indicator ma     # 仅 ma 日+周
python -m backend.cli refresh --from 20260610 --to 20260612 --dry-run  # 范围规模预估
python -m backend.cli refresh --date 20260612 --export           # 重算后导 Excel

# ===== 日期范围（run / export / refresh 共用 --from/--to，与 --date 互斥）=====
python -m backend.cli run --from 20260610 --to 20260612
python -m backend.cli export --from 20260610 --to 20260612       # 多文件 exports/analysis_{date}_*.xlsx
python -m backend.cli run --from 20260610 --to 20260612 --continue-on-error  # 失败继续

# ===== 运维 ops =====
python -m backend.cli ops refresh-state --date 20260609
python -m backend.cli ops backfill-state --date 20260608
python -m backend.cli ops prune --keep 1
python -m backend.cli ops repair-weekly --execute
python -m backend.cli ops backfill-dde-meta --sync-dwd --recalc
python -m backend.cli ops repair-dde-trend --date 20260612
python3 -m backend.cli ops spec-status --date 20260616   # v_dq_spec_freshness @ anchor

# ===== 数据拉取 =====
python -m backend.cli fetch                        # 全市场增量拉取
python -m backend.cli fetch --ts-code 000543.SZ 600580.SH  # 指定股票

# ===== 指标计算 =====
python -m backend.cli calc                         # 全市场（analysis_date=今天）
python -m backend.cli calc --date 20260605         # 指定分析日
python -m backend.cli calc --ts-code 000543.SZ 600580.SH  # 指定股票
python -m backend.cli calc --date 20260612 --refresh-spec ma  # 应急：单指标窄窗 FULL
python3 -m backend.cli calc --refresh-spec macd --date 20260616 --dry-run  # 零 DWS 写，仅报 stale 规模

# ===== Excel 导出 =====
python -m backend.cli export --date 20260605           # 仅导出（最快同日复跑）
python -m backend.cli export --date 20260603
python -m backend.cli export --date 20260603 --ts-code 000543.SZ

# ===== 持仓股导出 =====
python -m backend.cli export --date 20260620                           # 自动加载 持仓股列表.xlsx
python -m backend.cli export --date 20260620 --portfolio-file my_stocks.xlsx  # 指定文件
python -m backend.cli run --portfolio-file 持仓股列表.xlsx              # run + 持仓 sheet

# ===== 查询 =====
python -m backend.cli query --ts-code 000001.SZ --freq daily

# ===== 快照清理 =====
python -m backend.cli prune              # 保留最近 2 次运行（默认）
python -m backend.cli prune --keep 1     # 完全坍缩为纯 latest
python -m backend.cli prune --cleanup-backups --dry-run  # 预览待删除旧备份
python -m backend.cli prune --cleanup-backups --keep-backups 1  # 仅保留最近 1 个备份
python -m backend.cli prune --cleanup-backups  # 清理旧备份（默认保留 2 个）

# ===== calc state 运维 =====
python -m backend.cli backfill-state              # 缺 state 键 → FULL 补缺
python -m backend.cli backfill-state --date 20260608
python -m backend.cli refresh-state --date 20260609   # 指纹过期 → 仅对齐 state（不写 DWS）
python -m backend.cli refresh-state --date 20260609 --dry-run

# ===== 周线历史修复 =====
python -m backend.cli repair-weekly            # dry-run 预览
python -m backend.cli repair-weekly --execute  # 重建 dim_date+dwd_weekly+删孤儿

# ===== B4 周线 DDE 趋势元数据补洞（backfill 期间禁并行 run/calc）=====
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --dry-run
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --date 20260612 \
  --sync-dwd --workers 3 --sync-dwd-batch 50 --recalc
python -m backend.cli backfill-dde-meta --sync-dwd-only              # 中断恢复：仅 ODS→DWD
python -m backend.cli backfill-dde-meta --recalc-only --date 20260612  # ODS/DWD 已就绪

# ===== DDE trend 内容修复 =====
python -m backend.cli ops repair-dde-trend --date 20260612 --freq daily --ts-code 600831.SH
python -m backend.cli ops repair-dde-trend --date 20260612 --freq daily              # 全市场 daily
python -m backend.cli ops repair-dde-trend --date 20260615 --freq daily --purge-history --ts-code 688120.SH
python -m scripts.audit_dde_trend_oracle --date 20260612 --freq daily --sample 500

# 启动 API
uvicorn backend.api.app:app --reload

# 环境检查
python -m backend.cli check
python -m backend.cli status
python -m scripts.health_check

# ===== 结构背离验收 =====
python -m scripts.collect_divergence_golden smoke --indicator macd --count 25 --start 20230101 --end 20251231
python -m scripts.collect_divergence_golden smoke --indicator dde --count 25 --start 20230101 --end 20251231

# ===== 可交易背离 screening（L2 消费层）=====
python -m scripts.screen_divergence_tradable --date 20260612 --freq weekly --indicator macd
python -m scripts.screen_divergence_tradable --date 20260612 --freq daily --indicator macd --tradable-only

# ===== B4 硬门禁 =====
python3 -m scripts.diff_vs_123 --date 20260609 --breakdown --summary
python3 -m scripts.verify_b4_gate
pytest tests/test_b4_gate_regression.py tests/test_b4_gate_columns.py tests/test_b4_gate_diff.py -v

# ===== 管道基准 =====
python scripts/benchmark_run.py --date 20260609
python scripts/benchmark_run.py --date 20260609 --run
# SLA（稳态真新日，含 export）：墙钟≤1800s + health_check

# ===== 指数 =====
python -m backend.cli fetch-index                  # 仅拉取指数数据
python -m backend.cli calc-index --date 20260623   # 仅计算指数指标
```

## 项目结构

```
backend/
  config.py              # 环境变量加载
  cli.py                 # CLI 入口（check/fetch/calc/export/query/status/ops 等子命令）
  export_wide.py         # Excel 导出（中文列名、分组着色、日线+周线水平合并）
  export_column_comments.py  # 表头注释加载
  db/                    # DuckDB 连接管理 + 完整 DDL（26 表 + 16 视图 + 48 索引）
  fetch/                 # tushare API 封装（限流/重试）+ ODS 拉取（daily/moneyflow/plate/stock_basic/trade_cal）
  etl/                   # ETL 核心：DIM/DWD 构建 + 6 大指标计算器 + calc 路由/状态/编排 + 背离
    vector/              # 向量化 batch 计算（macd/dde/volume）— M2 性能层
  api/                   # FastAPI（5 端点：health/analysis/history/screening）
  b4_gate/               # B4 123 对标门禁（diff/extract/verify/columns）
  backtest/              # 回测引擎 + K线形态/组合评估
  kpattern_params.py     # K线形态参数
  log_config.py          # 日志配置
```

> 模块内具体文件见知识图谱 `get_architecture(aspects=['file_tree'])` 或 `search_graph` 按需定位。

## 数据流

```
tushare API → ODS(10表) → DIM(4表) + DWD(3表) → DWS(12表) → ADS(视图) → Excel/API
```

- **ODS：** stock_basic / daily / daily_basic / moneyflow / trade_cal / concept_detail / etl_log
- **DIM：** dim_stock / dim_date / dim_concept / dim_concept_stock
- **DWD：** dwd_daily_quote / dwd_weekly_quote / dwd_daily_moneyflow
  - 🚫 重建入口仅两个：`rebuild_all_dwd(con, ts_codes)` — 运维/首次建库；`rebuild_dwd_for_stale(con, stale_codes, trade_date)` — 日常新日。禁止单独调 `build_dwd`_*
- **DWS（12 表）：** macd / ma / kpattern / dde / volume / price_position（各 daily + weekly）
  - 所有 DWS 表含 `input_fingerprint`（SHA256）和 `spec_version`，写入由 `insert_dws_batch()` 统一处理
  - 🚫 查询必须走 `v_dws_*_latest` 视图，禁止直接查基表
- **ADS 视图：** `v_dws_*_latest`（12 个）+ `v_ads_analysis_wide`_*（4 个）
  - 视图锚点为 DWD 层：`dwd_daily_quote` / `dwd_weekly_quote` 为主表，所有 DWS LEFT JOIN。即使某类指标缺失，股票仍存在于导出中
  - `v_dws_*_latest` 用 `QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code, trade_date ORDER BY calc_date DESC)=1`

### CLI 三层架构

```
run（一站式入口）→ fetch（当日 ODS）→ rebuild DWD → refresh-state（条件）→ calc → export
```

**run 分步短连接：** 解析 dim_date → fetch+rebuild → refresh-state → calc（skip_stale_fetch）→ export。

- **同日复跑快路径（L0 pipeline_shortcut）：** fetch 无 calc 影响写入 + 无 stale DWD + 已有 prior calc → 跳过 DWD+calc，仍 export。`turnover_rate`/`pe_ttm`/`total_mv`/`amount` 等无指标映射列的 ODS 漂移不计入阻断（`fetch_blocks_dwd_calc`）；`adj_factor`/新 bar/quote·moneyflow 变更仍阻断。`--force` 穿透 L0（仍 fetch；DWD 仅 calc-affecting 窄重建；calc 走 `run_calc(force=True)`）。Step 1 fetch 若 0 rows 且无 stale DWD → 跳过 rebuild。运维：仅需 Excel 时用 `cli export --date X`。
- **新日 DWD 增量（`DWD_INCREMENTAL=1` 默认）：** stale 子集走 `rebuild_dwd_for_stale` → `rebuild_dwd_incremental`。**禁止日常全库 rebuild。**
  - **daily 三分支：** adj 变 / qfq 漂移 → SQL UPDATE 四列 qfq（非 DELETE）；无 DWD 历史（新股）→ 全量 build；其余 tail 股 → 仅当日 INSERT
  - **daily_basic 就绪门禁（`DWD_DAILY_BASIC_MIN_COVERAGE=0.80`）：** tail INSERT 前检测 `total_mv` 覆盖率；低于阈值推迟 INSERT，下次 fetch+DWD 自动补齐
  - **weekly 双路径：** tail 股 → 仅删/插含 `trade_date` 的周分区；qfq/insert 股 → 该股 full weekly rebuild
  - **moneyflow：** stale 子集 tail INSERT
  - 停牌填充 gap 检测 DWD vs 日历（已填充股不再重复）
- **DWD rebuild 后自动 refresh-state（`DWD_REBUILD_REFRESH_STATE=1`）：** 对 stale 子集调用 `refresh_calc_state_fingerprints`，对齐 `history_fp` 再进 calc。`dwd_result.changed_codes` 将 refresh scope 窄化到实际 DWD 写入子集（qfq ∪ insert ∪ tail）。
- **CalcPreflightContext（`CALC_REUSE_REFRESH_CTX=1`）：** `cli run` Step1 refresh 产出 tails+modes+fp_cache，Step2 calc 热路径复用，跳过 `batch_tails`/`batch_preflight`。独立 `cli calc` 走冷路径。G3/auto-fetch 二次 DWD rebuild 后 `merge_context_patch` 仅 merge patch 子集。
- **列→指标收窄（Wave 5，`CALC_COLUMN_NARROW=1`）：** ODS 列级 diff 经 `column_indicator_deps` 映射，有 mutation 时仅跑受影响指标 batch（如仅 `circ_mv`/`net_amount_dc` → `dde` 日+周）。`adj_factor`/新 PK INSERT/qfq refresh/结构性 stale_dwd → fallback 全 12 路由。refresh R1 不受影响。
- **calc 新日追算（`CALC_APPEND=1`，详见关键技术细节 §calc 路由）**
- **run 观测：** `ods_etl_log` 记录 `run_fetch` / `run_rebuild_dwd` / `run_refresh_state` / `run_export`。

### 指数数据流（平行体系）

```
tushare index_basic/index_daily/index_dailybasic → ODS(3) → DIM(1) + DWD(2) → DWS(6) → ADS(视图) → Excel/CLI
```

- **ODS(3)：** `ods_index_basic` / `ods_index_daily` / `ods_index_dailybasic`
- **DIM(1)：** `dim_index`
- **DWD(2)：** `dwd_index_daily` / `dwd_index_weekly`
  - `close_qfq = close`（指数无需复权，列名对齐计算器兼容）
  - `is_suspended = 0`（指数无停牌，列名对齐 `load_quote_groups` 日线过滤）
  - 周线聚合与个股同：`date_trunc('week', dt)` Monday anchor
- **DWS(6)：** `dws_index_macd_{freq}` / `dws_index_ma_{freq}` / `dws_index_volume_{freq}`（日+周）
  - 🚫 无 DDE（指数无资金流）、无 kpattern、无 price_position
- **ADS 视图：** `v_ads_market_index_daily` / `v_ads_market_index_weekly`
- **计算器复用：** `IndexMACDCalculator` / `IndexMACalculator` / `IndexVolumeCalculator` 继承个股计算器，仅覆盖 `src_table` 和 `dws_table`
- **指数列表配置：** `config/indices.yaml`，分 core/sector 组，修改后下次 run 生效
- **拉取策略：** 渐进式回填（首次 250 bar warmup，后续增量）; `cli run` 末尾低优先级，失败降级

## 关键技术细节

### 数据基础

- **前复权公式：** `price_qfq = price × adj_factor / latest_adj_factor`（latest_adj_factor 取每只股票最晚交易日对应的 adj_factor）
- 🚫 **周划分用 `date_trunc('week', dt)`**（周一锚点），统一用于 `dim_date.is_week_end` 与 `dwd_weekly_quote`。禁用 `strftime('%Y-%W')`
- **DWS 用 INSERT-only 快照模式**，calc_date 区分批次
- **DWS 快照保留：** `prune --keep N`（默认 2），只删被新 calc_date 覆盖的 superseded 行，绝不删每个 `(ts_code, trade_date)` 的 `MAX(calc_date)` 行
- **停牌填充**到每只股票 ODS 的 max(trade_date)，非全局 dim_date。仅内部缺口填充（按交易日历判断），尾部缺口不填
- **ods_etl_log 用 UUID 主键**，避免并发写冲突
- **DuckDB 不支持 AUTOINCREMENT**，用 INTEGER PRIMARY KEY 默认自增
- **tushare 限流：** 按接口独立限流（`PER_API_LIMIT=480`），进程级共享滑动窗口
- **daily API 不返回 adj_factor**——必须单独调 `adj_factor` 接口
- **BSE 股票（.BJ）无 moneyflow 数据**——DDE 指标为空属正常

### calc 路由：SKIP / APPEND / FULL

`classify_calc_mode()` 按 `dws_calc_state`（PK `(ts_code, freq, indicator)`）+ 各计算器 `SIGNATURE_COLS` 路由：

- **无 state → FULL**（建基线）
- **强签名变**（除权/填充/修正）→ **FULL**
- **无新 bar + 同签名 → SKIP**
- **有新 bar + 同签名 → APPEND**

**APPEND：** 仅算/写新 bar（EMA/ddx2 用种子递推、Volume 迟滞用 zone 种子），INSERT-only 不改写历史。
**强签名 `state_signature()`：** 对 last_td 前固定 245 根尾窗输入值序列做 SHA256，跨运行稳定。误判只多一次安全 FULL，绝不误 APPEND。
**等价性：** APPEND vs FULL `atol=1e-9`，`tests/test_etl/test_append_calc.py` 锁定。

**跨股 batch APPEND（`CALC_BATCH_APPEND=1`）：** 全市场按 `(indicator,freq)` 批处理（共享 tail load + EMA/zone 种子），SKIP 直接记 skip，FULL 余量进 chunk worker。

**跨股 batch FULL（`CALC_BATCH_FULL=1`）：** APPEND 后按 `group_by_indicator(full_items)` 批处理 mass 单指标 FULL（共享尾窗 + 窄写），余量进 `_calc_full_work_chunk`。

**同日复跑短路（`CALC_FAST_SKIP=1`）：** chunk 级 batch preflight（state + 245 尾窗），12 指标全 SKIP 则整股跳过 `calc_stock_pipeline`。同 day `--force` 若数据未变，跳过 tail SQL 仅读 state 路由 SKIP。

**断点恢复（`CALC_RECOVER_STATE=1`）：** 冷路径 preflight 前从 DWS 数据表恢复缺失的 `dws_calc_state` 记录。

**calc_date 门禁（`CALC_STRICT_DATE=1`）：** `calc_date > MAX(ods_daily)` 时拒绝 calc（防假新日空跑）。

**双指纹门（`CALC_DWD_FP_GATE=1`）：** `history_fp`（路由）与 DWS `input_fingerprint`（写跳过）分叉时：preflight FULL 且无 new_bars → 降级 SKIP 并写 state。

**M2 Vector Append（`CALC_VECTOR_APPEND=1`）：** `vector/macd_batch.py` / `dde_batch.py` / `volume_batch.py`；结构背离 O(n) cross。Volume trend APPEND 仅算新 bar；MACD weekly B4 APPEND 仅算 `target_indices`。

**Batch FULL 三层域：** 读域 tail 245 bar → 算域 `[recalc_start, calc_date]` → 写域同算域。禁止写窄窗、算全窗 expanding。

### calc 准入与数据完整性

- **G1 warmup：** `check_data_completeness()` — calc 准入 `dwd_rows ≥ 250`
- **G2 fresh：** `find_stale_ods_codes()` — ODS max < calc_date → 补 tail（run 内 skip）
- **G3 sync：** `find_stale_dwd_codes()` — ODS 有当日但 DWD 落后 → `rebuild_dwd_for_stale`
- **双轨 warmup：** 成熟股（available week-end ≥ 120）且 `week_end_bars < 120` → `weekly_fetch` 桶触发 auto-fetch；上市不足 120 周除外
- **auto-fetch：** 策略选择器 date/stock-batched，熔断器 5 次，weekly 负缓存（0 行按月记忆）
- **退市股过滤：** `delist_date < calc_date` 且 DWS 已有 → 跳过
- **数据质量门禁：** `_validate_ods_batch` 校验 OHLC 逻辑（high >= low）和必需字段非空
- **adj_factor 护栏：** `build_dwd_daily_quote` 排除 `adj_factor IS NULL` 或 `latest_adj IS NULL/0` 的行

### 环境变量汇总


| 变量                             | 默认值              | 作用                                   | 设为 0 的后果                        |
| ------------------------------ | ---------------- | ------------------------------------ | ------------------------------- |
| `DWD_INCREMENTAL`              | 1                | 新日 DWD 增量重建                          | 回退 stale 子集全量 `rebuild_all_dwd` |
| `DWD_REBUILD_REFRESH_STATE`    | 1                | DWD rebuild 后自动 refresh-state        | 跳过 refresh-state，fp 漂移→全股 FULL  |
| `DWD_DAILY_BASIC_MIN_COVERAGE` | 0.80             | daily_basic total_mv 覆盖率门禁           | 关闭门禁，延后发布的列可能永久 NULL            |
| `CALC_APPEND`                  | 1                | 新日追算（仅算新 bar）                        | 回退窄窗 FULL（255 窗重写）              |
| `CALC_BATCH_APPEND`            | 1                | 跨股 batch APPEND                      | 回退逐股 APPEND                     |
| `CALC_BATCH_FULL`              | 1                | 跨股 batch FULL                        | 回退 chunk-only FULL              |
| `CALC_VECTOR_APPEND`           | 1                | M2 向量化 batch 计算                      | 回退逐股 scalar 计算                  |
| `CALC_FAST_SKIP`               | 1                | 同日复跑 chunk 级短路                       | 每股进入 pipeline 逐指标判断             |
| `CALC_RECOVER_STATE`           | 1                | 断点恢复 calc_state                      | DWS 有数据但 state 缺失→FULL 重算       |
| `CALC_STRICT_DATE`             | 1                | calc_date > ODS max 拒绝 calc          | 自动 cap 到 ODS max                |
| `CALC_DWD_FP_GATE`             | 1                | 双指纹门（history_fp ≠ input_fingerprint） | 仅用单指纹                           |
| `CALC_REUSE_REFRESH_CTX`       | 1                | run 热路径复用 refresh 产出的 tails+modes    | calc 冷路径独立加载                    |
| `CALC_COLUMN_NARROW`           | 1                | Wave 5 列→指标收窄                        | 回退全 12 路由                       |
| `CALC_INCREMENTAL`             | 1                | 增量读/写（255 窗）                         | 回退全量读/写                         |
| `CALC_B4_WEEKLY_FAST`          | 1                | MACD weekly B4 快路径                   | 回退 expanding oracle             |
| `CALC_AUTO_SPEC_REFRESH`       | 1                | batch APPEND 前 auto spec refresh     | 跳过，仅手动 `--refresh-spec`         |
| `CALC_SKIP_STATE_REFRESH`      | 1                | SKIP 路径跳过冗余 state UPSERT             | SKIP 仍写 state                   |
| `CALC_FORCE_BATCH_REUSE`       | 1                | 同 day --force batch 短路               | force 时重走全量 preflight           |
| `CALC_WORKERS`                 | min(cpu-1,8)     | calc 多线程数                            | —                               |
| `REFRESH_STATE_PARALLEL`       | 1                | refresh-state 并行 tail load           | 串行加载                            |
| `EXPORT_SPEC_GATE`             | 0                | export 前检测 spec 落后并 WARNING          | 静默导出                            |
| `DUCKDB_MAX_MEMORY_MB`         | 4096             | DuckDB 内存上限（MiB）                     | —                               |
| `DUCKDB_TEMP_DIRECTORY`        | tmp（相对 data_dir） | DuckDB 溢出目录                          | —                               |
| `MIN_DISK_FREE_MB`             | 5120             | 磁盘空间门禁（MiB）                          | —                               |
| `DWS_PRUNE_KEEP_RUNS`          | 2                | prune 保留快照数                          | —                               |
| `PRUNE_KEEP_BACKUPS`           | 2                | prune 保留备份数                          | —                               |
| `LOG_PROGRESS_HEARTBEAT_SEC`   | 30               | 进度心跳间隔                               | —                               |


### 各指标说明

#### MACD 信号

- **参数：** (12, 26, 9)，非 (10, 20, 7)。最小数据要求 27 条
- **趋势方向：** 5-bar 指数加权回归（decay=0.15），阈值 0.001
- **趋势强度：** `trend_strength` 列，加权斜率/均值去量纲
- **背离：** `divergence_structure.compute_macd_structure_divergence` — 通达信 Level 2 顶底结构（金叉/死叉锚点、直接+隔峰 MDIF 线背、柱背、TG 日标注，dedup=10）
- **可交易背离（L2）：** `divergence_tradable.classify_tradable` — 三条硬门槛生成 `*_tradable` / `*_reject`
- **B4 `macd_zone`：** 123 `identify_macd_crossover` + `identify_near_crossover`；日线 (10,20,7) ewm；周线日线 resample 后 (12,26,9)
- **B4 `macd_trend`：** 123 `get_macd_trend` — 5 柱柱体加权回归 + `eps=0.001`；与 `trend_strength` 口径分离
- **警惕（`macd_alert`）：** 123 `trend_reversal_signals._hist_turn_up/down` — 3 柱 MACD 柱拐点

#### MA 信号

- **斜率：** 5-bar 线性回归归一化 %/日，alignment 阈值 0.08%/日
- **alignment：** 10 值 DWS 枚举（8 方向 + tangle + sideways）；一走平一趋势过渡态 Layer 3 八格查表 fallback。B4 软层：不对 123 `ma_regime` 硬比
- **MACalculator.SPEC_VERSION=v2**（Layer 3 fallback 八格语义）

#### K线形态

- **7 种形态 + 强度评分（0.0~1.0）**，向量化 NumPy 实现

#### DDE 信号

- **DDX：** (大单+超大单净买入量) / 总成交量
- **DDX2：** EMA(DDX, 5)
- **背离：** 与 MACD 同构，`CROSS(DDX,DDX2)` 锚点 + 隔峰柱背 + TG 日标注（dedup=10）；顶区 DDX 峰邻域尖刺过滤
- **警惕（`dde_alert` / `w_dde_alert`，B4 软层）：** TA-native 相邻 **3-bar** DDX2 线性回归斜率拐点（`eps=0`）；不对齐 123 5-bar。相邻窗口重叠 2/3（新信息占比 33%），在噪声过滤和时效性之间平衡（原 2-bar 噪声过大致信号与趋势矛盾，2026-06-21 `SPEC_VERSION=v4`）。枚举命名遵循「描述被反转的旧趋势」设计本意：`downturn_reverse`=下降趋势反弹（V 形，斜率由负转正，看多）；`upturn_reverse`=上升趋势回落（Λ 形，斜率由正转负，看空）。Excel 标签：上升趋势回落 / 下降趋势反弹。存量需 `cli refresh --indicator dde`
- **趋势（B4 hard `dde_trend`）：** 日线 `moneyflow_dc`+`circ_mv` → DDX1/DDX3 + 5 日 polyfit；缺 dc/circ 日线可回退 `net_mf_amount`/`total_mv`。🚫 周线禁止 `net_mf_amount`/`total_mv` 回退：必须 `resample('W')` 独立聚合仅 `net_amount_dc`+`circ_mv` 再 inner merge
- **趋势强度（`dde_trend_strength`）：** DDX2 5-bar 指数加权回归斜率 / mean(|DDX2|)（decay=0.20）
- **Tier-0 内容 DQ：** `scripts/audit_dde_trend_oracle.py` 为 oracle（全历史重算），`health_check` Section K 抽样 200 股。`net_amount_dc`/`circ_mv` ODS patch + DWD rebuild 后自动 `maybe_invalidate_dde_after_column_patch`
- **数据源限制：** BSE 股票（.BJ）tushare 不提供 moneyflow，DDE 不可用

#### 量能信号（Volume）

- **volume_ratio：** vol / MA5_vol
- **trend_strength：** ln(vol) 10-bar 加权回归，decay=0.20，正值放量/负值缩量
- **trend（B4 `vol_trend`）：** 123 同构 `volume_trend_v2` 分位生态法（日 anchor=60 / 周 anchor=30）。周线 trend 输入为 `vol * active_days / 5`（还原周成交总量）。`VolumeCalculator.SPEC_VERSION=v2`
- **量价背离：** `base.compute_price_signal_divergence`（60 日 rolling 窗 + 确认日 + 5 日去重）
- **区域：** 120 日百分位 + 迟滞（P90 进/P75 出，P10 进/P25 出）

#### Price Position（价格位置）

- **日线窗口：** 60 / 120 / 250；**周线窗口：** 60 / 120 / 250
- **公式：** `(close - N_bar_low) / (N_bar_high - N_bar_low) × 100`，值域 [0, 100]
- **纯价格特征，独立 DWS 模块**，不依赖其他 DWS 表

#### 板块/概念/题材数据

- **板块数据源：**
  - TDX 行业板块：`tdx_index` → `tdx_member`（约 60-80 个行业板块）
  - DC 概念板块：`dc_index` → `dc_member`（约 400-500 个概念板块）
  - DC 题材：`dc_concept` → `dc_concept_cons`（约 600+ 个题材，编码 000XXX.DC）
- **题材 vs 概念：** 两套独立分类体系，编码规则不同（概念 BKXXXX.DC vs 题材 000XXX.DC），同一只股票可同时属于多概念+多题材
- **缓存策略：** TDX 行业 7 天 TTL / DC 概念 3 天 TTL / DC 题材 7 天 TTL，`ods_plate_snapshot` 记录 fetch 时间
- **导出列：** `tdx_industry_board`（通达信行业）、`dc_concept_board`（概念板块）、`dc_theme_board`（所属题材），逗号分隔，空值显示 `N/A`
- **时间锚点：** 以分析日 `trade_date` 为准，通过 `ods_plate_member` 查询
- **管道位置：** `cli run` fetch 末尾低优先级步骤，失败降级不阻断
- **旧 concept 管线：** `ods_concept_detail` + `dim_concept` + `dim_concept_stock` 已废弃，DDL 保留但不再写入

### 其他技术要点

- **dwd_weekly_quote 没有 is_suspended 列**，周线查询不要加此过滤
- **前复权公式：** `price_qfq = price × adj_factor / latest_adj_factor`（与数据基础章节一致，此处强调 DWD/DWS 层面均适用）
- **DuckDB 资源管理：** temp_directory 默认 `data/tmp/`；`atexit` + `SIGTERM/SIGINT` handler 优雅关闭；首次 `get_connection(read_only=False)` 自动清理残留 `.tmp`
- **磁盘门禁：** 实际阈值 `max(MIN_DISK_FREE_MB, DB_SIZE/3)`；temp 目录阈值 `max(threshold/2, 1024 MB)`
- **polyfit 向量化：** `base.weighted_window_slopes(y, window, decay)` 闭式 WLS（权重为 `w²`），替代逐 bar `np.polyfit`。等价性 `atol=1e-9` golden-master 锁定
- **tushare 限流：** `client._InterfaceRateLimiter` — 按接口名滑动窗口 + `threading.Lock`，`PER_API_LIMIT=480`
- **交易日历：** `dim_date` 覆盖区间内本地查 `is_trade_day=1`，否则回退 API
- **多线程 calc：** `ThreadPoolExecutor` + `resolve_calc_workers()`（默认 `min(cpu-1, 8)`）。🚫 禁用 `multiprocessing.Pool`
- **周线计算器：** 一律只采样真周末 bar — `JOIN dim_date WHERE is_week_end=1`，禁止直接查滚动 bar
- **DWD rebuild 结果字典：** 含 `changed_codes` 键，需用 `_dwd_rebuild_row_count()` 计算总行数（非裸 `sum(result.values())`）
- **ods_etl_log 用 UUID 主键**，避免并发写冲突；DuckDB 不支持 AUTOINCREMENT，用 INTEGER PRIMARY KEY 默认自增

## 已知问题和注意事项

- `concept_detail` 接口需要 `id` 或 `ts_code` 参数，空参报错
- Python 3.9 不支持 `X | None` 类型语法，必须用 `Optional[X]`
- MACD 参数为 (12, 26, 9)，非 (10, 20, 7)
- `build_dwd_daily_quote` 依赖 `ods_daily.adj_factor`——stock-batched 拉取时必须单独调 `adj_factor` API
- BSE 股票无 moneyflow 数据（DDE 不可用）
- 上市不足 1 年的股票周线数据可能不足（MACD 需 ≥27 条，price_position 需 ≥60 条）
- **DDE Alert 存量迁移（2026-06-21）：** 枚举语义修正后，存量 DWS 中 alert 值与新语义相反。部署后需 `cli refresh --indicator dde --date <latest_calc_date>`
- `ema()` 函数在 `total_valid < min(period, 5)` 时返回全 NaN——极短历史股票无法计算 EMA
- **空数据处理：** `run_calc()` 自动补拉缺失数据（warmup=250 tdays，熔断器 5 次）。补拉失败按根因分 5 类写入 `ods_calc_skip_log`。退市股首次计算仍执行，已有 DWS 则跳过。`v_indicator_availability` 视图提供 full/partial/missing/unavailable/historical 五态
- **Date-batched per-stock 增量检测：** 仅当全部目标股已有该日 ODS 才跳过，避免 partial day 误跳过
- **导出语义：** `-`=当日无事件信号；`N/A`=不可算或源端无数据（板块/概念列在源数据不可用时同样默认 `N/A`）
- **Fetch 覆盖率：** `_compute_fetch_range` 要求 100% ODS 覆盖才跳过
- **停牌缺口跳过（stock-batched）：** 落在 ODS `[first_ods, last_ods]` 内部的缺失日视为停牌，不再发 API；head/tail 缺口仍拉取
- **同日复跑幂等闸门：** 全市场同 `calc_date` 已成功 calc 且无 stale ODS → `run_calc` 秒级退出
- **周线 kpattern 历史残留：** 修复前曾遗漏 `is_week_end` 过滤产出幽灵行，修复后须 `DELETE FROM dws_kpattern_weekly` 再重算

## 日志系统

- **配置入口：** `backend/log_config.py` — `setup_logging()` 统一配置（轮转文件 + stderr）
- **日志格式：** `ISO8601 LEVEL [run_id][module] message`
- **文件轮转：** `./data/tradeanalysis.log`，10MB × 5 备份（`LOG_MAX_BYTES`/`LOG_BACKUP_COUNT` 可配）
- **双通道：** 文件记 DEBUG（事后排查），stderr 记 INFO（实时观察）
- **Trace ID：** `set_run_id()` 注入请求级唯一标识，基于 `contextvars`（线程安全）
- **数据库审计：** `ods_etl_log` — ETL 步骤耗时/行数/状态；`ods_calc_skip_log` — 跳过计算的 6 种根因分类
- **异常日志：** `logger.exception()` 保留完整调用栈；`log_etl_error()` 将完整栈存入 DB
- **级别规范：** DEBUG=跳过详情 / INFO=进度+完成 / WARNING=降级 / ERROR=异常
- **统一进度前缀：** `progress {stage}:` — `fetch.ods` / `fetch.stocks` / `dwd.`* / `calc.batch_`* / `calc.chunk`* / `calc.stocks` / `export` / `export: tradable enrich` / `screening.tradable`
- **节流：** 默认每 5 交易日 / 5 股票一条；`LOG_PROGRESS_HEARTBEAT_SEC=30` 无输出则打 `still running`

## 工作流程

修改代码必须遵循以下流程，不可跳步（与 `/engineering-protocol` 对齐；涉及 DWD/calc/rebuild 时须先走 skill 内**决策树**，默认增量路径，禁止习惯性全量）：

1. **分析原因** — 先解释问题根因，不做任何修改
2. **制定方案** — 提出修改方案，等待用户审核
3. **用户同意** — 用户明确说"好"/"可以"/"同意"后，才进入下一步
4. **制定计划** — 写实施计划到 plan file
5. **用户审核** — 用户审批计划
6. **落地实施** — 按计划修改代码
7. **更新文档** — 修改完成后立即更新 CLAUDE.md 和相关 spec，无需等用户提醒

**禁止：** 在用户同意方案前直接改代码。先问、后改。
**禁止：** 完成修改后不更新文档。
**禁止：** 跨任务时忘记更新 CLAUDE.md 中的命令、架构、注意事项。