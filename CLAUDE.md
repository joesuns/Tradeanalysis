# CLAUDE.md

## 沟通规范

- **默认中文**：所有交互使用中文，代码/术语可中英混杂
- **代码任务前加载 `/engineering-protocol`** — 5 步协议（全量解析→诚实判断→全量审计→等待审批→收尾核对）；**计算约束**（质量>速度、最小范围、DWD/Calc 决策树、禁止习惯性全库 rebuild）见 `.claude/skills/engineering-protocol/SKILL.md`，细节索引 `reference.md`

## 项目概述

Tradeanalysis — 基于 tushare + DuckDB 的 A 股技术分析数据管道。拉取全市场 OHLCV/资金流/PE 等数据，计算 MACD/MA/K线形态/DDE/量能/价格位置六大类技术指标（日线+周线），通过 FastAPI 查询、CLI 导出 Excel。

- **数据源：** tushare Pro（6200 积分）
- **数据范围：** 全 A 股 5000+ 只，覆盖 2015 年至今
- **Spec：** `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` (v1.12)
- **日常运维 Runbook：** `docs/superpowers/plans/2026-06-09-daily-runbook.md`
- **信号优化方案：** `docs/superpowers/plans/2026-06-02-signal-timing-optimization.md`
- **CLI 重构方案：** `docs/superpowers/plans/2026-06-04-cli-fetch-calc-export-fingerprint.md`

## 技术栈

- **语言：** Python ≥3.9（不使用 `list[str] | None`，用 `Optional[list[str]]`）
- **存储：** DuckDB ≥1.0（持久化文件 `./data/tradeanalysis.duckdb`）
- **数据源：** tushare Pro（`TUSHARE_TOKEN` 环境变量）
- **API：** FastAPI + uvicorn
- **Excel：** openpyxl
- **测试：** pytest + httpx

## 常用命令

```bash
# 测试
pytest tests/ -v

# ===== 每日分析（一条命令） =====
python -m backend.cli run                        # 全市场，最近交易日
python -m backend.cli run --date 20260605              # 同日复跑（rebuild 可能跳过）
python -m backend.cli run --date 20260605 --skip-export  # 同日复跑不导 Excel

# ===== change-driven refresh smoke（Wave 1–5）=====
# ANALYSIS_DATE=20260612 ./scripts/smoke_change_driven_refresh.sh --run-all
# ANALYSIS_DATE=20260612 SMOKE_TS_CODE=000543.SZ ./scripts/smoke_change_driven_refresh.sh --run-wave5

# ===== 强制重算（refresh R1）=====
python -m backend.cli refresh --date 20260612                    # 12 路由全 FULL
python -m backend.cli refresh --date 20260612 --indicator ma     # 仅 ma 日+周
python -m backend.cli refresh --from 20260610 --to 20260612 --dry-run  # 范围规模预估
python -m backend.cli refresh --date 20260612 --export           # 重算后导 Excel

# ===== 日期范围（run / export / refresh 共用 --from/--to，与 --date 互斥）=====
python -m backend.cli run --from 20260610 --to 20260612
python -m backend.cli export --from 20260610 --to 20260612       # 多文件 exports/analysis_{date}_*.xlsx
python -m backend.cli run --from 20260610 --to 20260612 --continue-on-error  # 失败继续

# ===== 运维 ops（顶层命令仍可用，会 DeprecationWarning）=====
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
python -m backend.cli calc --date 20260612 --refresh-spec ma  # 应急：单指标窄窗 FULL（日常 run/calc 已 auto）
python3 -m backend.cli calc --refresh-spec macd --date 20260616 --dry-run  # 零 DWS 写，仅报 stale 规模

# ===== Excel 导出 =====
# 从数据库直接导出（不重算），默认 exports/analysis_{date}_gen{now}.xlsx
python -m backend.cli export --date 20260605           # 仅导出（最快同日复跑）
python -m backend.cli export --date 20260603
python -m backend.cli export --date 20260603 --ts-code 000543.SZ

# ===== 查询 =====
python -m backend.cli query --ts-code 000001.SZ --freq daily

# ===== 快照清理 =====
python -m backend.cli prune              # 保留最近 5 次运行（默认）
python -m backend.cli prune --keep 1     # 完全坍缩为纯 latest，最省空间

# ===== calc state 运维（一次性）=====
python -m backend.cli backfill-state              # 缺 state 键 → FULL 补缺（慢）
python -m backend.cli backfill-state --date 20260608
python -m backend.cli refresh-state --date 20260609   # 指纹过期 → 仅对齐 state（~15min，不写 DWS）
python -m backend.cli refresh-state --date 20260609 --dry-run

# ===== 周线历史修复（date_trunc 周划分变更后一次性运维）=====
python -m backend.cli repair-weekly            # dry-run 预览：错误周末数 + 各周线表孤儿行数
python -m backend.cli repair-weekly --execute  # 重建 dim_date+dwd_weekly+删孤儿，之后再跑 calc 刷新

# ===== B4 周线 DDE 趋势元数据一次性补洞（net_amount_dc + circ_mv，Plan C 按日）=====
# backfill 期间禁止并行 run/calc（DuckDB 单写）
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --dry-run
# 推荐：一条命令 ODS→DWD→refresh→DDE weekly 重算
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --date 20260612 \
  --sync-dwd --workers 3 --sync-dwd-batch 50 --recalc
python -m backend.cli backfill-dde-meta --sync-dwd-only              # 中断恢复：仅 ODS→DWD
python -m backend.cli backfill-dde-meta --recalc-only --date 20260612  # ODS/DWD 已就绪，仅 calc 闭环

# ===== DDE trend 内容修复（Tier-0 DQ；repair 窗口禁并行 run/calc）=====
python -m backend.cli ops repair-dde-trend --date 20260612 --freq daily --ts-code 600831.SH  # 六股 pilot
python -m backend.cli ops repair-dde-trend --date 20260612 --freq daily              # 全市场 daily 修复
python -m backend.cli ops repair-dde-trend --date 20260615 --freq daily --purge-history --ts-code 688120.SH  # 深度修复：清除该股全部 dde/daily 快照后重算
python -m scripts.audit_dde_trend_oracle --date 20260612 --freq daily --sample 500  # stored vs recompute

# 启动 API
uvicorn backend.api.app:app --reload

# 环境检查
python -m backend.cli check
python -m backend.cli status
python -m scripts.health_check   # 跑批后全链路质量体检（只读，Section I=成熟股最新 week-end 截面）

# ===== 结构背离验收（无人工标注时用 smoke + pytest；有通达信时用 worksheet/import/diff）=====
python -m scripts.collect_divergence_golden smoke --indicator macd --count 25 --start 20230101 --end 20251231
python -m scripts.collect_divergence_golden smoke --indicator dde --count 25 --start 20230101 --end 20251231
# 可选人工 golden：worksheet → 通达信填 tdx 列 → import → diff（见 scripts/collect_divergence_golden.py 头注释）

# ===== 可交易背离 screening（L2 消费层，不改 DWS）=====
python -m scripts.screen_divergence_tradable --date 20260612 --freq weekly --indicator macd
python -m scripts.screen_divergence_tradable --date 20260612 --freq daily --indicator macd --tradable-only

# ===== B4 硬门禁（10 列，不含 ma_alignment / dde_alert 软层；过渡期对 123 diff）=====
python3 -m scripts.diff_vs_123 --date 20260609 --breakdown --summary
python3 -m scripts.verify_b4_gate   # golden 冻结后
pytest tests/test_b4_gate_regression.py tests/test_b4_gate_columns.py tests/test_b4_gate_diff.py -v

# ===== 管道基准（只读观测 / 可选真跑）=====
python scripts/benchmark_run.py --date 20260609
python scripts/benchmark_run.py --date 20260609 --run
# SLA（稳态真新日，含 export）：benchmark_run --run 墙钟≤1800s + health_check；见 pipeline §1.1 / 附录 D
# 迁移日：不卡 chunk；记录 full_by_indicator + L3 spot-check
# P4 实施：docs/superpowers/plans/2026-06-12-p4-indicator-chunk-impl.md（Task 5 + 5b）
```

## 项目结构

```
backend/
├── config.py              # 环境变量加载（TUSHARE_TOKEN/DUCKDB_PATH/LOG_LEVEL）
├── cli.py                 # CLI 入口（check/fetch/calc/export/query/status 6 子命令）
├── export_wide.py         # Excel 导出（中文列名、分组着色、日线+周线水平合并、表头列注释）
├── export_column_comments.py  # 从 docs/export/export-column-comments.yaml 加载表头注释
├── db/
│   ├── connection.py      # DuckDB 连接、自检、WAL checkpoint
│   └── schema.py          # 完整 DDL（26 表 + 16 视图 + 31 索引，含 input_fingerprint/spec_version）
├── fetch/
│   ├── client.py          # tushare API 封装（限流 600/min、指数退避重试）
│   ├── ods_daily.py       # 双模式拉取：date-batched（全市场）+ stock-batched（per-stock 增量）
│   ├── ods_stock_basic.py # 个股基础信息
│   ├── ods_trade_cal.py   # 交易日历
│   ├── ods_concept.py     # 概念板块（>100 股自动切 per-concept 策略）
│   └── ods_moneyflow.py   # 资金流向
├── etl/
│   ├── base.py            # EMA/SMA/线性回归/安全浮点转换/compute_fingerprint/insert_dws_batch
│   ├── b4_macd.py         # B4 MACD zone/trend（123 10,20,7 日线 + resample-W 周线）
│   ├── divergence_structure.py  # MACD/DDE Level 2 结构背离（通达信顶底结构：隔峰柱背+TG标注）
│   ├── divergence_tradable.py   # L2 可交易背离消费层（三条硬门槛；export/combo/screening enrich）
│   ├── build_dim.py       # 维度表构建（stock/date/concept，事务保护）
│   ├── build_dwd.py       # DWD 三层：rebuild_all_dwd() 全量入口 + rebuild_dwd_for_stale() 增量入口
│   ├── dwd_weekly_sql.py  # dwd_weekly_quote 滚动周线 SQL（week_trunc + WTD 窗口聚合）
│   ├── calc_macd.py       # MACD(12,26,9)：EMA/趋势(加权+强度)/结构背离(TG日+dedup=10)/near/警惕
│   ├── calc_ma.py         # MA5/MA10：乖离率/5日回归斜率/10值alignment(sideways)/near(est_days)
│   ├── calc_kpattern.py   # 7种K线形态 + 分形态强度评分（Doji双路径/零体兜底/MA10趋势上下文）
│   ├── calc_dde.py        # DDX/DDX2：结构背离(DDX/DDX2交叉+尖刺过滤)/趋势/警惕 周线假期感知
│   ├── calc_volume.py     # 量能：volume_ratio/百分位/爆量地量滞回/趋势+强度/量价背离
│   ├── calc_price_position.py  # 价格位置：60/120/250日滚动分位（纯价格特征，DWS 独立模块）
│   ├── orchestrator.py    # 编排器：run_etl（旧全流程）+ run_calc（新 calc 流程+完整度检查+auto-fetch）
│   └── error_handler.py   # 5级错误分级（FATAL/ERROR/DEGRADED/WARN/INFO）
└── api/
    ├── app.py             # FastAPI app
    ├── router.py          # 5 端点（health/analysis/history/screening）
    └── models.py          # Pydantic 模型
```

## 数据流

```
tushare API → ODS(7表) → DIM(4表) + DWD(3表) → DWS(12表) → ADS(视图) → Excel/API
```

- **ODS：** stock_basic / daily / daily_basic / moneyflow / trade_cal / concept_detail / etl_log
- **DIM：** dim_stock / dim_date / dim_concept / dim_concept_stock
- **DWD：** dwd_daily_quote / dwd_weekly_quote / dwd_daily_moneyflow
  - **重建入口：** `rebuild_all_dwd(con, ts_codes)` — 统一重建全部 3 张 DWD 表，禁止单独调用 build_dwd_*
- **DWS（12 表）：** macd / ma / kpattern / dde / volume / price_position（各 daily + weekly）
  - 所有 DWS 表含 `input_fingerprint`（SHA256 内容指纹）和 `spec_version`（默认 'v1'）
  - `_insert` 统一由 `insert_dws_batch()` 处理（base.py）
- **ADS 视图：** v_dws_*_latest（12 个）+ v_ads_analysis_wide_*（4 个，含 vol_signal 复合信号）
  - **视图锚点为 DWD 层：** `v_ads_analysis_wide_daily` 以 `dwd_daily_quote` 为主表，`v_ads_analysis_wide_weekly` 以 `dwd_weekly_quote` 为主表。所有 DWS 表 LEFT JOIN。即使某类指标缺失，股票仍存在于导出中（DWD 在 ETL 流程中先于 DWS 构建，数据最完整）

### CLI 三层架构

```
run（一站式入口）→ fetch（当日 ODS）→ rebuild DWD → calc → export

run 分步短连接：解析 dim_date → fetch+rebuild → **refresh-state（条件）** → calc（skip_stale_fetch）→ export。
calc/export 共用同一 analysis_date（`--date`）；calc 收尾 run_checkpoint。
- **同日复跑快路径（L0 pipeline_shortcut）：** fetch **无 calc 影响写入** + 无 stale DWD + 已有 prior calc → **跳过 DWD+calc**，仍 export。`turnover_rate`/`pe_ttm`/`total_mv`/`amount` 等无指标映射列的 ODS 漂移 **不计入** L0 阻断（`fetch_blocks_dwd_calc`）；`adj_factor`/新 bar/quote·moneyflow 变更仍阻断。`run_rebuild_dwd.data_completeness.pipeline_shortcut=true`。**`run --force` 穿透 L0**（仍 fetch；DWD 仅 **calc-affecting changed∪stale** 窄重建，禁止全库 rebuild；calc 走 `run_calc(force=True)`）。L0 失败时日志 `pipeline L0 gate: skip_dwd_calc=false reason=...`（fetch_rows_written / spec_stale / stale_dwd / no_prior_calc）；纯装饰列 drift 打 `cosmetic ODS drift ignored`。验收：`tests/test_etl/test_column_narrow_equivalence.py`（Wave5 narrow vs full，`atol=1e-9`）。
- **同日复跑快路径（续）：** Step 1 fetch 若 0 rows 且 `find_stale_dwd_codes` 空 → **跳过 rebuild**；无 force 时 Step 2 calc 幂等秒退；`--skip-export` 跳过 Excel。运维：仅需 Excel 时用 `cli export --date X`。
- **新日 DWD 增量（`DWD_INCREMENTAL=1` 默认）：** stale 子集走 `rebuild_dwd_for_stale` → `rebuild_dwd_incremental`（`n_fetch>0` 与 `n_fetch==0` 同路径）；`find_stale_dwd_codes` 比对 daily/weekly/moneyflow 三张 DWD（moneyflow 仅当 ODS 有当日资金流）。**禁止日常全库 rebuild**（`rebuild_all_dwd` 无 `ts_codes` 仅限运维/首次建库）。`=0` 回退 stale 子集全量 `rebuild_all_dwd`。calc G3 / auto-fetch 亦走 `rebuild_dwd_for_stale`。
  - **daily 三分支：** adj 变 / qfq 漂移 → `refresh_qfq_prices` SQL **UPDATE** 四列 `open/high/low/close_qfq`（非 DELETE）；无 DWD 历史（新股）→ 全量 `build_dwd_daily_quote`；其余 tail 股 → 仅 `trade_date` 当日 INSERT（`mode=tail=`）
  - **daily_basic 就绪门禁（`DWD_DAILY_BASIC_MIN_COVERAGE=0.80` 默认）：** tail INSERT 前检测 `ods_daily_basic.total_mv` 覆盖率；低于阈值时推迟 INSERT（打 WARNING），避免 tushare daily_basic API 延后发布导致 `total_mv`/`circ_mv`/`pe_ttm`/`turnover_rate` 永久 NULL。下次 fetch+DWD 自动补齐。`=0` 关闭门禁。
  - **weekly 双路径：** tail 股 → `mode=week=` 仅删/插含 `trade_date` 的周分区（`dwd_weekly_sql`）；qfq/insert 股 → 该股 full weekly rebuild
  - **moneyflow：** stale 子集 tail INSERT（`mode=tail=`）
  - 停牌填充 gap 检测 **DWD vs 日历**（已填充股不再重复 LATERAL）
  - 运维手册：`docs/superpowers/plans/2026-06-09-daily-runbook.md`
- **DWD rebuild 后自动 refresh-state（`DWD_REBUILD_REFRESH_STATE=1` 默认）：** `run_rebuild_dwd` 实际写入后，对 **stale 子集** 调用 `refresh_calc_state_fingerprints`（`run_refresh_state` 审计步），对齐 `history_fp` 再进 calc；防止真新日 fp 漂移 → 全股 FULL/chunk。`return_artifacts=True`（run 热路径）仍算 preflight modes；**`cli refresh-state` 跳过 post-preflight** + **isolated 并行 tail load**（`REFRESH_STATE_PARALLEL=1`）；upsert 后内存 patch state（免 reload）。`run_calc` 内 auto-fetch / G3 stale DWD rebuild 同理。`=0` 回退旧行为（仅运维 `cli refresh-state`）。**2026-06-18 优化：** `dwd_result.changed_codes` 将 refresh scope 从全量 stale 窄化到实际 DWD 写入子集（qfq ∪ insert ∪ tail），同日复跑 qfq-only 场景 ~454s→~8s。
- **CalcPreflightContext（M1+M1.1，`CALC_REUSE_REFRESH_CTX=1` 默认，计划 `docs/superpowers/plans/2026-06-13-calc-preflight-context-p0.md` + `2026-06-13-calc-preflight-merge-m1.1.md`）：** `cli run` Step1 refresh 产出 tails+modes+fp_cache，Step2 calc **热路径**复用，跳过 `batch_tails`/`batch_preflight`；APPEND/FULL state 改 `upsert_calc_state_batch` 批写。独立 `cli calc` 走冷路径。G3/auto-fetch 二次 DWD rebuild 后 **`merge_context_patch`** 仅 merge patch 子集（非整包 cold）；batch append 对缺失股走 `_merge_cold_tails_and_preflight`（含 `progress calc.batch_preflight` 心跳）。观测：`preflight_source=refresh|cold`、`tails_load_skipped`、`cold_merge_stocks`/`cold_merge_elapsed_sec`、`state_upsert_mode=batch`（`ods_etl_log.calc_dws`）。
- **双指纹门（`CALC_DWD_FP_GATE=1` 默认，计划 `docs/superpowers/plans/2026-06-14-calc-routing-dual-fp-export-opt.md`）：** `history_fp`（路由）与 DWS `input_fingerprint`（写跳过）分叉时：preflight FULL 且无 new_bars → `check_dwd_unchanged` 真 → 降级 SKIP 并写 state；batch_full fingerprint_match 兜底 upsert `history_fp`。**batch_append + chunk fallthrough** 均传 `dwd_fp_cache`。B4 元数据回填后一次性 `cli refresh-state --date X` 运维补洞。
- **chunk_codes 重算（同上 plan）：** batch_append 收尾 `_compute_chunk_codes`（preflight 失败 ∪ 剩余 FULL），热路径 cold merge 成功不再 fallthrough chunk（修复 7c090546：5290 股空转）。
- **run 列→指标收窄（Wave 5，`CALC_COLUMN_NARROW=1` 默认，计划 `docs/superpowers/plans/2026-06-15-wave5-column-indicator-deps.md`）：** ODS 列级 diff 事件经 `column_indicator_deps` 映射；`cli run` 在 **有 mutation 且满足 G1–G8** 时仅跑受影响指标 batch（如仅 `circ_mv`/`net_amount_dc` → `dde` 日+周），跳过 quote tail + 无关 10 路由。**refresh R1 不受影响**（仍 12 路由或 `--indicator`）。`adj_factor`/新 PK INSERT/qfq refresh/结构性 stale_dwd → fallback 全 12 路由。观测：`calc_dws.data_completeness.run_indicator_filter` / `calc_routes_narrowed` / `active_routes`。`=0` 回退现网全路由。
- **M2 Vector Append（`CALC_VECTOR_APPEND=1` 默认，计划 `docs/superpowers/plans/2026-06-13-calc-vector-append-m2.md`）：** `vector/macd_batch.py` / `dde_batch.py` / `volume_batch.py`；结构背离 O(n) cross。**M2c+ volume trend：** APPEND `require_trend_target_indices` fail-fast + `compute_volume_trend_series(..., target_indices=new_bars)` 仅算新 bar（~184×）；**P2+ FULL** `batch_full_volume` 经 `resolve_compute_indices` 对齐写窗（plan `2026-06-17-batch-full-compute-domain-optimization.md`）。Profiling：`python3 scripts/profile_volume_trend_v2.py`。**M2d MACD weekly B4：** APPEND `require_b4_weekly_target_indices` + `b4_weekly_series_from_daily(..., target_indices=new_bars)`；**P2+ FULL** `b4_weekly_series_from_daily_fast`（`CALC_B4_WEEKLY_FAST=1` 默认，`=0` 回退 expanding oracle）。`batch_full_macd` weekly 传 `b4_target_indices`。Profiling：`python3 scripts/profile_macd_b4_weekly.py --mode fast_write_window`。
- **Batch FULL 三层域（2026-06-17 ADR）：** 读域 tail 245 bar → 算域 `[recalc_start,calc_date]`（`resolve_compute_indices`）→ 写域同算域（`insert_dws_batch_multi` narrow write）。禁止写窄窗、算全窗 expanding。
- **run 观测：** `ods_etl_log` 新增 `run_fetch` / `run_rebuild_dwd` / **`run_refresh_state`** / `run_export`；`run_rebuild_dwd.data_completeness.skipped=true` 表示未重建。
- **边界：** 除权/adj 回填后若未触发 fetch，须 `fetch` + `calc --force` 或手动 rebuild；同日复跑不覆盖此场景。

fetch（数据拉取层）
├── 不传 --ts-code → date-batched 全市场并行拉取（3线程），传 active codes 做 per-stock 增量
├── --ts-code → stock-batched per-stock 增量拉取
├── per-stock 增量：某日仅当**全部**目标股已有 ODS 才跳过（partial day 会重拉）
├── per-stock 缺口收口：`_drop_suspension_gaps` 仅拉 head(<first_ods)/tail(>last_ods)，跳过交易区间内部停牌缺口（避免反复 API 空转；内部缺口由 DWD 停牌填充处理）
├── stock-batched 元数据补洞：`circ_mv IS NULL` 补 `daily_basic`；`net_amount_dc IS NULL` 仅补 `moneyflow_dc`（不重拉整段 moneyflow）
└── `fetch_by_date_range_parallel` 在 ts_codes=None 时自动 resolve active codes

calc（计算层）
├── `--date` 指定 analysis_date（默认 today），与 export/run 对齐
├── **`--refresh-spec ma[,volume]`** — 应急窄窗 FULL（state∪DWS stale 子集）；日常 `run/calc` 在 batch APPEND 前 **`CALC_AUTO_SPEC_REFRESH=1`（默认）** 自动执行同等逻辑。**不走** rebuild DWD。加 **`--dry-run`** 仅报 stale 规模、零 DWS 写。与 `cli refresh --indicator` 分工：refresh=全链路 R1；`cli ops spec-status --date` 读 `v_dq_spec_freshness`（见 runbook「算法 SPEC_VERSION 发布」）
├── 退市股过滤：delist_date < calc_date 且 DWS 已有 → 跳过
├── G1 warmup：`check_data_completeness()` — calc 准入 `dwd_rows≥250`；成熟股 `week_end_bars`（120 周窗口内）不足 → `weekly_fetch` 仅 fetch 不拦 calc
├── G2 fresh：find_stale_ods_codes() — ODS max < calc_date → date/stock-batched 补 tail（run 内 skip）
├── G3 sync：find_stale_dwd_codes() — ODS 有当日但任一张 DWD 落后 → rebuild_dwd_for_stale
├── 缺 warmup → auto-fetch（策略选择器 + 熔断器 5 次 + **weekly 负缓存**：0 行结果按月记忆，同进程不重试）
├── 补拉后 rebuild_all_dwd() → re-check → 分类原因 → 写 ods_calc_skip_log
├── **增量优化（`CALC_INCREMENTAL=1` 默认，`=0` 回退全量读/写）：**
│   ├── `RecalcSpec` 注册表（`recalc_spec.py`）— 各 Calculator 声明 `RECALC_SPEC_DAILY/WEEKLY`，`resolve_recalc_bars()` 聚合重算宽度（当前 daily **255** = max(lookback+seed+event_tail)+5）
│   ├── 三层窗口：`load_start`（种子）→ `recalc_start`（指纹+窄写起点）→ `calc_date`（窄写终点）
│   ├── P0.5 保守域指纹（策略 A）：`last_trade_date` 同 **且** `[recalc_start, last_td]` 子集 SHA256 同 → skip
│   ├── P1 单股单管线：`calc_stock_pipeline()` 每股一次窄读，5 个 quote 计算器共享 `quote_groups`
│   ├── P2 多线程：`ThreadPoolExecutor` + `resolve_calc_workers()`（默认 `min(cpu-1, 8)`，`CALC_WORKERS` 可覆盖）。DuckDB 单文件禁跨进程写，故线程池共享同一实例
│   └── P3 热路径：PP `rolling_window_minmax_deque`；MACD/DDE `resolve_ema_seeds` 递推；MACD/DDE 背离 `divergence_structure` 结构法；Volume 背离 `compute_price_signal_divergence` 向量化
├── **新日追算（`CALC_APPEND=1` 默认，`=0` 回退 `CALC_INCREMENTAL` 窄窗）：** 每股每 freq 每指标走双路径
│   ├── 路由 `classify_calc_mode()`（`calc_router.py`）按 `dws_calc_state` + 各计算器 `SIGNATURE_COLS`：
│   │   无 state→**FULL**（建基线）；强签名变（除权/填充/修正）→**FULL**；无新 bar 同签名→**SKIP**；有新 bar 同签名→**APPEND**
│   ├── APPEND `append_calculate()`：仅算/写新 bar（EMA/ddx2 用 `resolve_ema_seeds` 种子递推、Volume 迟滞用 zone 种子、滚动类复用全尾窗算法），INSERT-only 不改写历史
│   ├── 强签名 `state_signature()`：对 last_td 前 **固定 245 根尾窗**关键输入列做值序列 SHA256（替弱指纹），跨运行稳定；误判只多一次安全 FULL，绝不误 APPEND（新 bar 始终全窗重算）
│   ├── 状态 `dws_calc_state` PK `(ts_code, freq, indicator)`，写入仅在实际写了行时；`_route_calc()` 在 `calc_stock_pipeline` 内统一路由（DDE 自载 moneyflow 尾窗）
│   ├── 等价性硬约束：各指标 APPEND 与 FULL 逐值 `atol=1e-9`（`tests/test_etl/test_append_calc.py` 锁定）
│   └── **同日复跑短路（`CALC_FAST_SKIP=1` 默认，需 `CALC_APPEND`）：** `_calc_stock_chunk` 入口 chunk 级 batch preflight（state + 245 尾窗 quote/DDE），12 指标全 SKIP 则整股跳过 `calc_stock_pipeline`；尾窗**无 calc_date 上界**（对齐 `last_trade_date` 可领先 `calc_date`）；缺帧/空帧/DWD 签名变 → fallthrough
├── **跨股 batch APPEND（`CALC_BATCH_APPEND=1` 默认，需 `CALC_APPEND`）：** 全市场新日 `run_calc` 在 ThreadPool 前走 `run_batch_append_phase()`——按 `(indicator,freq)` 批处理 APPEND 股（共享 `batch_load_quote_tails` + batch EMA/zone 种子），SKIP 直接记 skip 并刷新 state；FULL 余量进 chunk worker。`--ts-code` 子集或 `=0` 回退逐股 APPEND。设计见 `docs/superpowers/plans/2026-06-08-cross-stock-batch-append.md`。
├── **跨股 batch FULL（`CALC_BATCH_FULL=1` 默认，需 `CALC_BATCH_APPEND`）：** `run_batch_append_phase` APPEND 后按 `group_by_indicator(full_items)` 批处理 mass 单指标 FULL（共享尾窗 + `insert_dws_batch_multi` 窄写）；完成后重建 `full_items`，仅余量进 `_calc_full_work_chunk` / `_calc_stock_chunk`。`=0` 回退 chunk-only。设计见 `docs/superpowers/plans/2026-06-12-p4-indicator-chunk-impl.md` Task 5b。
│   - **batch 尾窗列：** `quote_tail_columns(freq)` 与 `quote_pipeline_columns(freq)` 相同（含 `pct_chg`，供 kpattern 涨跌停过滤）；≠ 各指标 `SIGNATURE_COLS` 并集 alone。
├── **性能专项（batch write）：** `insert_dws_batch_multi` 按 `(indicator,freq)` 一次窄写；MACD/DDE/Volume 种子批载；SKIP 路径 `CALC_SKIP_STATE_REFRESH` 跳过冗余 `dws_calc_state` UPSERT；`batch_ctx` chunk 零重复 tail SQL。
├── **calc_date 门禁（`CALC_STRICT_DATE=1` 默认）：** `calc_date > MAX(ods_daily)` 时拒绝 calc（防假新日 72min 空跑）；`=0` 时自动 cap 到 ODS max。
└── export 行数 << 预期（<80% 活跃股）→ WARNING

export（导出层）
├── 从 latest 视图直接导出（不重算）
├── 默认 filter_st 排除 ST；`--include-st` 含 ST
├── 日线+周线水平合并（无 freq 参数）；周线为空时（无 week-end≤date）仅输出日线，不崩溃
├── **Sheet：**「综合分析」（信号速览）+「个股分析」（全列）；`vol_signal` 与量能组相邻（`vol_zone→vol_trend→vol_divergence→量价信号`）；综合分析含可交易背离 + 量价信号，不含 MACD/DDE 结构/剔除背离
├── **样式：** 基础信息表头 `#1A1A1A`；Sheet 标签不设 tabColor（未选中为 Excel 默认灰，选中白底高亮）
├── 表头列注释：悬停表头查看指标说明；文案维护于 `docs/export/export-column-comments.yaml`（经 `export_column_comments.py` 加载）；改列须同步 spec §9 + YAML
├── **Export-E1（2026-06-17）：** 单次 `_build_merged_display_df` transform + 批量写值 + CF 斑马纹；5271 行 building sheets ~140s→**27s**（plan `2026-06-14-export-sheet-perf.md`）
├── **`EXPORT_SPEC_GATE=1`（默认 0）：** export 前检测 state/DWS spec 落后并打 WARNING（非阻断）
└── 默认路径: exports/analysis_{date}_gen{now}.xlsx
```

## 关键技术细节

- **前复权公式：** `price_qfq = price × adj_factor / latest_adj_factor`（latest_adj_factor 取每只股票最晚交易日对应的 adj_factor）
- **dwd_weekly_quote 没有 is_suspended 列**，周线查询不要加此过滤
- **周划分用 `date_trunc('week', dt)`（周一锚点）**，统一用于 `dim_date.is_week_end`（build_dim）与 `dwd_weekly_quote` 滚动周线分区（build_dwd）。**禁用 `strftime('%Y-%W')`**：`%Y` 是日历年、年初不满一周记第 00 周，会把跨年自然周切成两段（每年元旦附近 OHLC/pct_chg 错误 + 多出一个假周末 bar）
- **DWS 用 INSERT-only 快照模式**，calc_date 区分批次
- **所有查询必须走 v_*_latest 视图**，禁止直接查 DWS 基表
- **latest 视图取最新快照（A5）：** `v_dws_*_latest` 用 `QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code, trade_date ORDER BY calc_date DESC)=1`（单次扫描），替代旧的逐行关联子查询。语义等价但随快照数稳定提速。⚠️ 改 DDL 后**实库需重跑 schema 初始化**（`CREATE OR REPLACE VIEW`）才会生效
- **DWS 快照保留策略：** 快照无界增长，用 `prune_dws_snapshots(con, keep_runs=N)`（CLI `prune --keep N`，默认 5）清理。只删被新 calc_date 覆盖的 superseded 行，**绝不删每个 `(ts_code, trade_date)` 的 `MAX(calc_date)` 行**，故 `v_*_latest` 结果不变；`keep_runs=1` 为纯 latest。逻辑删除 + CHECKPOINT，不挂进 run_calc，运维显式执行
- **停牌填充到每只股票 ODS 数据的 max(trade_date)**，而非全局 dim_date。填充触发条件：gap 检测按**交易日历**判断内部缺口（该股 ODS 实际行数 < 其 [min,max] 区间内 dim_date 交易日天数），而非 `dwd.n < ods.n`（step1 为 1:1 插入恒等，旧逻辑导致停牌永不填充）。仅内部缺口填充，尾部缺口（max ODS 之后）不填
- **ods_etl_log 用 UUID 主键**，避免并发写冲突
- **DuckDB 不支持 AUTOINCREMENT**，用 INTEGER PRIMARY KEY 默认自增
- **tushare 6200 分限流实测：** ~645 次/分钟无节流
- **daily API 不返回 adj_factor**——必须单独调 `adj_factor` 接口获取。date-batched 和 stock-batched 都需要
- **DWD 重建入口：** 日常新日走 `rebuild_dwd_for_stale(con, stale_codes, trade_date)`（`DWD_INCREMENTAL=1` 默认）；运维/首次建库走 `rebuild_all_dwd(con, ts_codes)`。禁止单独调 `build_dwd_*` 或日常无 `ts_codes` 全库 rebuild，否则部分 DWD 表遗漏或破坏增量路径
- **BSE 股票（.BJ）无 moneyflow 数据**——tushare 不支持，DDE 指标为空属正常
- **周线数据充足性：** MACD 需 ≥27 条周线，price_position 需 ≥60 条。上市不足 1 年的股票周线指标为空属正常

### Price Position（价格位置）

- **日线窗口：** 60 / 120 / 250（季度/半年/年度滚动分位）
- **周线窗口：** 60 / 120 / 250（同上，60 周 ≈ 14 个月）
- **公式：** `(close - N_bar_low) / (N_bar_high - N_bar_low) × 100`，值域 [0, 100]
- **纯价格特征，独立 DWS 模块**。不依赖任何其他 DWS 表
- **Excel 列名：** "60日价格滚动分位(%)"，位于 K线形态右侧（价格类指标就近排列）

### 量能信号（Volume）

- **volume_ratio：** vol / MA5_vol（量比，基础量能信号）
- **trend_strength：** 加权斜率/均值去量纲，正值放量/负值缩量，横截面可比（ln(vol) 10-bar 加权回归，decay=0.20）
- **trend（B4 `vol_trend` / `w_vol_trend`）：** 123 同构 `volume_trend_v2` 分位生态法（日 anchor=60 / 周 anchor=30；方向映射 expanding/shrinking/flat）。**周线** trend 输入为 `vol * active_days / 5`（还原周成交总量，对齐 123 tushare weekly 原始量；DWD 存 5 日等效归一化 vol）。`trend` 与 `trend_strength` 口径分离。`VolumeCalculator.SPEC_VERSION=v2`：DWD 指纹未变但 DWS 仍为 v1 时强制重算（`check_dwd_unchanged` + `classify_calc_mode` 双门禁）；算法升级后须跑一次 volume calc 刷新存量
- **量价背离：** 仍用 `base.compute_price_signal_divergence`（60 日 rolling 窗 + 确认日 + 5 日去重；MACD/DDE 已改结构法）。顶背离（价格高位+量缩），底背离（价格低位+量回升>10%）
- **区域：** 120 日百分位 + 迟滞（P90 进/P75 出，P10 进/P25 出）

### MACD 信号

- **趋势方向：** 5-bar 指数加权回归（decay=0.15），阈值 0.001
- **趋势强度：** `trend_strength` 列，加权斜率/均值去量纲
- **背离：** `divergence_structure.compute_macd_structure_divergence` — 通达信 Level 2 顶底结构（金叉/死叉锚点、直接+隔峰 MDIF 线背、柱背、**结构形成 TG 日**标注，dedup=10）；非 60 窗 rolling。`RECALC_SPEC` lookback=250、event_tail=10
- **可交易背离（L2 消费层）：** `divergence_tradable.classify_tradable` — 在 L1 不变前提下，export/combo/screening 经三条硬门槛生成 `*_tradable` / `*_reject`；Excel 结构列改名「MACD/DDE结构背离」。export 打 `progress export: tradable enrich`；`cli run` 的 `run_export.data_completeness.tradable_enrich` 含日/周统计
- **B4 `macd_zone` / `w_macd_zone`（DWS `turning_point`）：** 123 `identify_macd_crossover` + `identify_near_crossover`；日线 MACD **(10,20,7)** ewm；周线日线 `resample('W')` 后 **(12,26,9)**。`backend/etl/b4_macd.py`
- **B4 `macd_trend` / `w_macd_trend`（DWS `trend`）：** 123 `get_macd_trend` — 5 柱柱体加权回归 + `eps=0.001`；与 `trend_strength`（12,26,9 柱）口径分离
- **警惕（B4 `macd_alert` / `w_macd_alert`）：** 123 `trend_reversal_signals._hist_turn_up/down` — 3 柱 MACD 柱拐点（无 flat）；`backend/etl/b4_alerts.py`。`MACDCalculator.SPEC_VERSION=v3`（v2=alert；v3=zone/trend）
- **最小数据要求：** 27 条（EMA26 种子 = 前 26 日 SMA）

### MA 信号

- **斜率：** 5-bar 线性回归归一化 %/日，alignment 阈值 0.08%/日
- **alignment：** 10 值 DWS 枚举（8 方向 + tangle + sideways）；一走平一趋势过渡态 Layer 3 **八格查表** fallback 归入 8 类，不扩枚举。**B4 软层**：保留 TA 算法，不对 123 `ma_regime` 硬比
- **MACalculator.SPEC_VERSION=v2：** v2=Layer 3 fallback 八格语义（2026-06-14）；算法升级后须 `calc --force` 或待新日窄窗 FULL 刷新存量 alignment

### K线形态

- **7 种形态 + 强度评分（0.0~1.0）**，向量化 NumPy 实现

### DDE 信号

- **DDX：** (大单+超大单净买入量) / 总成交量（`moneyflow` 量口径，背离/alert 仍用此列）
- **DDX2：** EMA(DDX, 5)
- **背离：** `divergence_structure.compute_dde_structure_divergence` — 与 MACD 同构，`CROSS(DDX,DDX2)` 锚点 + 隔峰柱背 + TG 日标注（dedup=10）；顶区 DDX 峰邻域尖刺过滤；`RECALC_SPEC` lookback=250
- **可交易背离：** 同 MACD L2 门槛；DDE 为辅信号，`.BJ` 无数据
- **警惕（`dde_alert` / `w_dde_alert`，B4 软层）：** TA-native 相邻 **2-bar** DDX2 线性回归斜率拐点（`eps=0`）；`b4_alerts.compute_ddx2_slope_alerts`；**不对齐** 123 5-bar。仍 export/Excel。
- **趋势（B4 hard `dde_trend` / `w_dde_trend`）：** 日线 123 同构 `analyze_moneyflow_trend_optimized` — `moneyflow_dc`+`circ_mv` → DDX1/DDX3 + 5 日 polyfit；缺 dc/circ 日线可回退 `net_mf_amount`/`total_mv`。周线 B4 对齐 `analyze_weekly_dde_trend`：`resample('W')` 独立聚合 **仅 `net_amount_dc`** 与 `circ_mv` 再 inner merge（禁止日级 join 后 resample、周线 trend **禁止** `net_mf_amount`/`total_mv` 回退）、`ddx3.tail(4)` 在全序列 `iloc[-1]`（非 dim_date 周界）；`ddx3` 尾窗含 NaN → flat（不回退 `ddx`）。`ods_moneyflow.net_amount_dc` + `ods_daily_basic.circ_mv` 落库。**2026-06-18 向量化：** 逐 bar `np.polyfit` 循环 → `weighted_window_slopes(decay=0)` 闭式 OLS，等价性 `atol=1e-9` 锁定（`test_append_calc.py`）。
- **趋势强度（`dde_trend_strength`，非 B4）：** DDX2 **5-bar** 指数加权回归斜率 / mean(|DDX2|)（decay=0.20）；与 MACD `trend_strength` 同窗。`DDECalculator.SPEC_VERSION=v3`（v3=alert 2-bar + strength 5-bar；v2=123 5-bar alert）
- **Tier-0 内容 DQ：** 日线 `dde_trend` 须与 B4 moneyflow 重算一致；`scripts/audit_dde_trend_oracle.py` 为 oracle（**全历史** `_compute_indicators`，非 tail255 — EMA60 暖机），`health_check` Section K 抽样 200 股（mismatch >0.1% WARN、>1% FAIL）；异常时 `ops repair-dde-trend` 窄窗 invalidate+`CALC_FORCE_HARD` 重算；指定 `--ts-code` 时可加 `--purge-history` 清除该股全部 dde/daily 历史快照（修复 v_*_latest 中旧 calc_date 脏 trend）。**P0 防复发（已接线）：** `net_amount_dc`/`circ_mv` ODS patch + DWD rebuild 后，`cli run`/`refresh` 在 `refresh_state` 之后自动 `maybe_invalidate_dde_after_column_patch`（删 dde/daily @ calc_date DWS + state，再 calc FULL）；见 `docs/superpowers/plans/2026-06-17-dde-content-invalidation-p0.md`
- **数据源限制：** BSE 股票（.BJ）tushare 不提供 moneyflow，DDE 不可用
- **warmup 周期：** 系统级 warmup = 250 个交易日（Price Position 250d 窗口，所有指标最大值）。补拉时按此窗口计算起始日期，不拉无用历史数据

## 已知问题和注意事项

- `concept_detail` 接口需要 `id` 或 `ts_code` 参数，空参报错
- Python 3.9 不支持 `X | None` 类型语法，必须用 `Optional[X]`
- MACD 参数为 (12, 26, 9)，非 (10, 20, 7)
- `build_dwd_daily_quote` 依赖 `ods_daily.adj_factor`——stock-batched 拉取时必须单独调 `adj_factor` API（daily 接口不返回此字段）
- BSE 股票无 moneyflow 数据（DDE 不可用）
- 上市不足 1 年的股票周线数据可能不足（MACD 需 ≥27 条，price_position 需 ≥60 条）
- `ema()` 函数在 `total_valid < min(period, 5)` 时返回全 NaN——极短历史股票无法计算 EMA
- **空数据处理：**
  - `run_calc()` 自动补拉缺失数据（策略选择器 date/stock-batched，per-target-stock 增量检测，100% 覆盖率阈值，warmup=250 tdays，熔断器：连续 5 次 fetch 异常或 5 次空返回则中止）
  - 补拉范围 = `[max(上市日, analysis_end - 250tdays), min(calc_date, 退市日)]`，warmup=250 由 Price Position 250d 窗口决定
  - 补拉失败按根因分 5 类写入 `ods_calc_skip_log`：source_unavailable / insufficient_rows / no_dwd_data / fetch_failed / delisted
  - 退市股：delist_date < calc_date 且 DWS 已有 → 跳过并记 DELISTED。首次计算退市股仍执行
  - 所有 Calculator.calculate() 返回 `CalcResult`（calculated + skipped 分类统计）
  - Price Position 各窗口独立：min_periods=2，数据不够窗口用已有全部数据
  - `v_indicator_availability` 视图提供 full/partial/missing/unavailable/historical 五态
  - 详细架构见 `docs/superpowers/plans/2026-06-04-empty-data-handling.md`
- **Date-batched per-stock 增量检测:** 全市场 fetch 传入 active codes；`_get_trading_days(..., ts_codes=...)` 仅当**全部**目标股已有该日 ODS 才跳过。避免 partial day（如 623/5524）导致整日误跳过。
- **Freshness 三门禁（calc）：** G1 calc 准入（DWD≥250 行）+ G2 stale ODS + G3 stale DWD。`run` 先 fetch 故 calc 设 `skip_stale_fetch=True`。
- **双轨 warmup（拆分门禁）：**
  - **calc 准入：** `dwd_rows ≥ 250`（`check_data_completeness.ok`）
  - **fetch 门禁：** 成熟股（available week-end ≥ 120）且 `week_end_bars < 120` → `weekly_fetch` 桶，触发 auto-fetch；**上市不足 120 周除外**（`weekly_required = min(120, available)`）。`week_end_bars` 仅在 `[resolve_weekly_warmup_start(calc_date), calc_date]` 窗口内计数（与 fetch 历史对齐），非全历史
  - fetch 起点：`min(250td_start, 120 week-end_start, list_date)`
- **导出语义：** `-`=当日无事件信号；`N/A`=不可算或源端无数据（如亏损股 PE、历史不足量能分位）。
- **Fetch 覆盖率:** `_compute_fetch_range` 要求 100% ODS 覆盖才跳过（对齐 123 项目严格检查模式）。
- **停牌缺口跳过（stock-batched）:** `fetch_stocks_incremental` 的 `_get_missing_ranges_per_stock` 在合并 range 前调用 `_drop_suspension_gaps`：落在该股 ODS `[first_ods, last_ods]` 内部的缺失日视为停牌（tushare 不返回），不再发 API；head/tail 缺口仍拉取。无 ODS 行时全保留（首次拉取）。
- **数据质量门禁:** `_validate_ods_batch` 已接入全部 3 条 `daily` 写库路径（`fetch_by_date_range` / `_fetch_chunk` 并行 / `fetch_stocks_incremental`）。在 ODS INSERT 前校验 OHLC 逻辑（high >= low）和必需字段（open/high/low/close/vol/amount）非空，**返回过滤后的有效记录列表**，无效行丢弃并按批次打 WARNING。仅作用于 daily（OHLCV），daily_basic/moneyflow 不校验。
- **adj_factor 护栏:** `build_dwd_daily_quote` 排除 `adj_factor IS NULL` 或 `latest_adj IS NULL/0` 的行，避免静默产出 NULL `close_qfq` / 除零；插入前诊断 COUNT 被排除行数并打 WARNING（某股 qfq 不可用时可见）。
- **DWS 指纹跳过（P0.5 策略 A）:** `compute_input_fingerprint(df, recalc_start)` 对
  `[recalc_start, last_trade_date]` 域做 SHA256；`check_dwd_unchanged()` 比对最新指纹
  （可选 `expected_spec_version` + `load_latest_spec_versions`：仅输入指纹同但算法版本旧时不 skip）。
  相同 → 跳过计算；不同 → 窄域重算。重复跑同一天从 ~107min 降到秒级。
  `input_fingerprint` / `spec_version` 由 `insert_dws_batch()` 写入每行。
- **指纹检测批量化（A4）:** `load_latest_fingerprints(con, table, ts_codes)` 用
  `ROW_NUMBER()`（`ORDER BY calc_date DESC, trade_date DESC` 确定性平局）一次取回整组
  `{ts_code: 最新指纹}`，6 个计算器在循环前预取并传入
  `check_dwd_unchanged(..., latest_fps=...)`，把 ~6.6 万次单股 SELECT 降到每组一次。
- **同日复跑短路（CALC_FAST_SKIP v1）:** 实库同日复跑 **834s→630s**（24%）；12 指标全 SKIP 才短路。未达 60s → v2 partial skip 见 `docs/superpowers/plans/2026-06-08-calc-partial-skip-v2.md`。
- **同日复跑 partial skip（v2）:** `preflight_stock_modes_v2` 按指标分区；SKIP 直接记 skip，APPEND/FULL 走 `calc_stock_pipeline_selective`（按 `(freq,source)` 窄加载，APPEND 复用 chunk 尾窗）；BSE DDE 空帧视为 SKIP。`write_calc_state_from_df` 在 SKIP/写后统一刷新 `dws_calc_state`；`repair-weekly --execute` 自动清空 weekly state。
- **同日复跑幂等闸门:** 全市场同 `calc_date` 已成功 calc 且无 stale ODS → `run_calc` 秒级退出（`calc idempotent skip`）。**spec 检测（fast gate）：** `dws_calc_state` 聚合 **+ 当日/当周 anchor `trade_date` 截面**（`v_*_latest @ trade_date`，非全库 per-stock 扫描）；**`--force` 智能短路** 与幂等同理。**`CALC_AUTO_SPEC_REFRESH=1`（默认）** 在 batch APPEND 前对 stale 子集窄窗 FULL（respect `indicator_filter`）；应急 `calc --refresh-spec`；迁移期见 runbook S2。
- **同 day `--force` batch 短路（`CALC_FORCE_BATCH_REUSE=1` 默认）：** 若 run 级未短路但数据未变，`run_batch_append_phase` 跳过 5388×245 tail SQL，仅读 `dws_calc_state` 路由 SKIP（~489s→秒级）。
- **新日追算（CALC_APPEND，append-only calc）:** 新交易日 calc 不再对每股窄写 255 窗，
  而由 `classify_calc_mode()` 按 `dws_calc_state`（PK `(ts_code, freq, indicator)`）+ 各计算器
  `SIGNATURE_COLS` 路由 SKIP/APPEND/FULL。**APPEND** 仅算/写新 bar（`append_calculate()`，
  EMA/ddx2 用 `resolve_ema_seeds` 种子、Volume 迟滞用 zone 种子、滚动类复用全尾窗算法），
  INSERT-only 不改写历史；**FULL**（无 state / 强签名变 / 写后兜底）走原窄窗重算并作 APPEND 的
  等价 oracle。强签名 `state_signature()` 对 last_td 前**固定 245 根尾窗**输入值序列做 SHA256
  （`compute_history_signature`），替代弱的 min/max/mean 指纹，**跨运行稳定**（误判只多一次安全
  FULL，绝不误 APPEND——新 bar 永远全窗重算）。设计/实施见
  `docs/superpowers/specs/2026-06-07-calc-append-only-design.md` 与
  `docs/superpowers/plans/2026-06-07-calc-append-only-impl.md`。等价性 `atol=1e-9` 由
  `tests/test_etl/test_append_calc.py` 锁定。⚠️ **范围外后续项：** 端到端「秒级日更」仍受
  ~320s 全市场 freshness-fetch 拖累，需单独立项提速。
- **计算器批量取数（B1）:** `load_quote_groups(con, src, freq, columns, ts_codes)` 用
  `WHERE ts_code IN (...)` + 内存 `groupby` 一次取回整组分股帧（daily 过滤 `is_suspended=0`，
  weekly `JOIN dim_date is_week_end=1`），替代每股一次 SELECT；MACD/MA/Volume/PricePosition/
  KPattern 共用，DDE 用 `_load_daily_batch`/`_load_weekly_batch`。**指标计算逻辑零改动**，
  等价性由"批量帧 == 逐股查询逐行相等"测试锁定。
- **进程级共享限流（A1）:** `client._InterfaceRateLimiter`——按接口名滑动窗口 + `threading.Lock`，
  `TushareClient._limiter` 类级共享。tushare 按接口独立限流，故多线程对同一接口共享一份预算
  （`PER_API_LIMIT=480`），避免 3 线程各放行 600/min 合计超限被封 IP。
- **交易日历优先本地（A3）:** `_local_trading_days(con, start, end)` 在 `dim_date` 覆盖区间时
  本地查 `is_trade_day=1`，否则回退 `trade_cal` API；`_get_trading_days` 与
  `fetch_stocks_incremental` 都已改走，省增量场景的多余 API 往返。
- **stock-batched 批量 INSERT（A2）:** `fetch_stocks_incremental` 的 daily/daily_basic/moneyflow
  三接口改 `register`+`INSERT SELECT`，与 date-batched 对齐（逐行 ~666x 提速）。
- **逐 bar polyfit 向量化（B2）:** `base.weighted_window_slopes(y, window, decay)` 用定窗加权回归
  **闭式解**一次算完整列斜率，替代每根 bar 一次 `np.polyfit` 的循环；`sliding_window_mean_abs`
  做斜率归一化。MACD/DDE/MA 直接替换，Volume 用「全正满窗闭式快路径 + 含 NaN/非正值窗回退」混合。
  **数值要点:** `np.polyfit(...,w)` 最小化 `Σ(w·残差)²`，故闭式 WLS 权重为 `w²`。
  等价性由 golden-master（冻结旧 polyfit 循环为 oracle，`atol=1e-9`）锁定；MACD 微基准 ~250×。
- **DDE 周线批量聚合:** `_load_weekly` 使用 LAG 窗口函数一次性完成周间 SUM 聚合，
  替代原来的 per-week N+1 查询。SQL 调用 ~150x 减少，DDE 周线 ~50min → ~25s。
- **批量完整度检查:** `check_data_completeness` 使用 GROUP BY 替代逐股票循环，5524 SQL → 1 SQL。
- **多线程 calc（P2）:** `run_calc` 用 `ThreadPoolExecutor` 按股分片并行（默认 `min(cpu-1, 8)` 线程，
  `CALC_WORKERS=N` 覆盖），每线程独立 DuckDB 连接、共享同一进程实例（MVCC 多写）。
  **DuckDB 单文件仅允许一个 read-write 进程**，故禁用 `multiprocessing.Pool`（会触发
 `IOException: Could not set lock`）。与 `ods_daily` fetch 的多线程写模式一致。
 墙钟记 `ods_etl_log`（观测项，非硬 KPI）。
- **calc 进度心跳（防"假卡死"）:** `StageProgress`（`backend/etl/progress.py`）统一计数节流 + 30s 时间心跳。
  worker 每算完 1 只调用 `_report_calc_progress()`，输出 `progress calc.stocks: N/total (X%) | 耗时 | stocks/s | ETA`；
  `batch_append` 阶段：`calc.batch_tails`（五步尾窗）→ `calc.batch_preflight` → `calc.batch_state`（SKIP 刷新 + APPEND 写 state）→ `calc.batch_compute`（按指标批算）→ `calc.batch_full`（mass 单指标 FULL）→ `calc.batch_append` 汇总。chunk worker：FULL 余量 `_calc_full_work_chunk`（按 `(ts_code, indicator, freq)` work item）；fallthrough `_calc_stock_chunk`；无 batch_ctx 时 `calc.chunk_tails` + `calc.chunk`。`calc.stocks` 支持 0/N 心跳。`calc_dws.data_completeness` 含 **`chunk_work_items`**、`chunk_stocks`、`batch_full_items`、`full_by_indicator`（如 `{"kpattern_weekly": 5003}`）。运维：`calc.stale_fetch` / `calc.stale_dwd` / `calc.auto_fetch` DWD 重建均有分步日志。export：`weekly query` / `building sheets` / `xlsx saved`。DDE 日线尾窗 SQL `ROW_NUMBER<=245`。
  **DDE 周线尾窗：** `_load_weekly_batch` 打 `progress calc.dde_weekly: i/N chunks`；`tail_window=245` 只扫最近 246 周 + `expected_days` 标量子查询（全市场 ~38s）。等价性 `test_dde_weekly_tail_matches_load_weekly_batch`。
  **batch_preflight 后空窗：** preflight 缓存 `fp_cache`，`build_skip_state_records` 复用、不二次 `state_signature`；`CALC_SKIP_LOG_VERBOSE=0`（默认）同批 `fingerprint_match` 只写摘要行 `__batch__`；`chunk=0` 跳过 12×大 IN COUNT。
  fetch/DWD/export 同契约，grep `progress ` 即可观察全链路。
- **周线计算器一律只采样真周末 bar:** `dwd_weekly_quote` 为滚动周线（每交易日一行），
  全部 6 个周线计算器（含已修复的 kpattern）weekly 路径 `JOIN dim_date ... WHERE is_week_end=1`，
  禁止直接查滚动 bar。**注意：** kpattern 周线历史曾遗漏此过滤产出 intra-week 幽灵行，
  修复后须 `DELETE FROM dws_kpattern_weekly` 再重算才能从 `v_*_latest` 清除残留。
- **DWD rebuild 结果字典：** `rebuild_dwd_incremental` / `rebuild_all_dwd` 返回的 dict
  含 `changed_codes`（list）键。需要用 `_dwd_rebuild_row_count()` 而非裸
  `sum(result.values())` 计算总行数（后者会 TypeError）。该 helper 定义于
  `backend/etl/build_dwd.py`。

## 日志系统

- **配置入口：** `backend/log_config.py` — `setup_logging()` 统一配置（轮转文件 + stderr）
- **日志格式：** `ISO8601 LEVEL [run_id][module] message`，例如：
  `2026-06-05T20:44:00 INFO [a1b2c3d4][backend.etl.orchestrator] calc ALL DONE`
- **文件轮转：** `./data/tradeanalysis.log`，10MB × 5 备份（`LOG_MAX_BYTES`/`LOG_BACKUP_COUNT` 可配）
- **双通道：** 文件记 DEBUG（事后排查），stderr 记 INFO（实时观察）
- **Trace ID：** `set_run_id()` 注入请求级唯一标识，基于 `contextvars`（线程安全）。CLI 在 `main()` 自动分配，零侵入
- **数据库审计：**
  - `ods_etl_log` — 每次 ETL 步骤的耗时/行数/状态/完整度，SQL 可查
  - `ods_calc_skip_log` — 股票跳过计算的 6 种根因分类（`SkipReason` enum）
- **异常日志：** `logger.exception()` 保留完整调用栈；`log_etl_error()` 将完整栈存入 DB
- **API 中间件：** `log_requests` 记录每个请求 method/path/status/耗时
- **级别规范：** DEBUG=跳过详情 / INFO=进度+完成 / WARNING=降级 / ERROR=异常
- **统一进度前缀：** `progress {stage}:` — `fetch.ods` / `fetch.stocks` / `dwd.*` / `calc.batch_*` / `calc.chunk*` / `calc.stocks` / `export` / `export: tradable enrich` / `screening.tradable`
- **batch 阶段中文进度：** `calc.batch_tails`/`calc.batch_compute` 等用中文步骤名；`calc.batch_compute` 按股 `stock_progress` 心跳（默认 30s + 每 5 股），示例：`progress calc.batch_compute: MACD日线 | 1250/5251 (23%) | 380s | 3.3 股/s | 预计剩余 ~1200s`
- **节流：** 默认每 5 交易日 / 5 股票一条；`LOG_PROGRESS_HEARTBEAT_SEC=30` 无输出则打 `still running`
- **环境变量：** `LOG_PROGRESS_HEARTBEAT_SEC` / `LOG_PROGRESS_DAY_STEP` / `LOG_PROGRESS_STOCK_STEP`

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
