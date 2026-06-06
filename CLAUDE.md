# CLAUDE.md

## 沟通规范

- **默认中文**：所有交互使用中文，代码/术语可中英混杂
- **代码任务前加载 `/engineering-protocol`** — 5 步协议（全量解析→诚实判断→全量审计→等待审批→收尾核对）

## 项目概述

Tradeanalysis — 基于 tushare + DuckDB 的 A 股技术分析数据管道。拉取全市场 OHLCV/资金流/PE 等数据，计算 MACD/MA/K线形态/DDE/量能/价格位置六大类技术指标（日线+周线），通过 FastAPI 查询、CLI 导出 Excel。

- **数据源：** tushare Pro（6200 积分）
- **数据范围：** 全 A 股 5000+ 只，覆盖 2015 年至今
- **Spec：** `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` (v1.8)
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
python -m backend.cli run --date 20260604        # 指定日期

# ===== 数据拉取 =====
python -m backend.cli fetch                        # 全市场增量拉取
python -m backend.cli fetch --ts-code 000543.SZ 600580.SH  # 指定股票

# ===== 指标计算 =====
python -m backend.cli calc                         # 全市场（analysis_date=今天）
python -m backend.cli calc --date 20260605         # 指定分析日
python -m backend.cli calc --ts-code 000543.SZ 600580.SH  # 指定股票

# ===== Excel 导出 =====
# 从数据库直接导出（不重算），默认 exports/analysis_{date}_gen{now}.xlsx
python -m backend.cli export --date 20260603
python -m backend.cli export --date 20260603 --ts-code 000543.SZ

# ===== 查询 =====
python -m backend.cli query --ts-code 000001.SZ --freq daily

# ===== 快照清理 =====
python -m backend.cli prune              # 保留最近 5 次运行（默认）
python -m backend.cli prune --keep 1     # 完全坍缩为纯 latest，最省空间

# ===== 周线历史修复（date_trunc 周划分变更后一次性运维）=====
python -m backend.cli repair-weekly            # dry-run 预览：错误周末数 + 各周线表孤儿行数
python -m backend.cli repair-weekly --execute  # 重建 dim_date+dwd_weekly+删孤儿，之后再跑 calc 刷新

# 启动 API
uvicorn backend.api.app:app --reload

# 环境检查
python -m backend.cli check
python -m backend.cli status
```

## 项目结构

```
backend/
├── config.py              # 环境变量加载（TUSHARE_TOKEN/DUCKDB_PATH/LOG_LEVEL）
├── cli.py                 # CLI 入口（check/fetch/calc/export/query/status 6 子命令）
├── export_wide.py         # Excel 导出（中文列名、分组着色、日线+周线水平合并）
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
│   ├── build_dim.py       # 维度表构建（stock/date/concept，事务保护）
│   ├── build_dwd.py       # DWD 三层：rebuild_all_dwd() 统一入口 → daily_quote/weekly_quote/moneyflow
│   ├── calc_macd.py       # MACD(12,26,9)：EMA/趋势(加权+强度)/背离(确认日+去重)/near(est_days)/警惕
│   ├── calc_ma.py         # MA5/MA10：乖离率/5日回归斜率/10值alignment(sideways)/near(est_days)
│   ├── calc_kpattern.py   # 7种K线形态 + 分形态强度评分（Doji双路径/零体兜底/MA10趋势上下文）
│   ├── calc_dde.py        # DDX/DDX2：背离(DDX源+尖刺过滤)/趋势/警惕 周线假期感知
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

run 分步短连接：解析 dim_date → fetch+rebuild → calc（skip_stale_fetch）→ export。
calc/export 共用同一 analysis_date（`--date`）；calc 收尾 run_checkpoint。

fetch（数据拉取层）
├── 不传 --ts-code → date-batched 全市场并行拉取（3线程），传 active codes 做 per-stock 增量
├── --ts-code → stock-batched per-stock 增量拉取
├── per-stock 增量：某日仅当**全部**目标股已有 ODS 才跳过（partial day 会重拉）
└── `fetch_by_date_range_parallel` 在 ts_codes=None 时自动 resolve active codes

calc（计算层）
├── `--date` 指定 analysis_date（默认 today），与 export/run 对齐
├── 退市股过滤：delist_date < calc_date 且 DWS 已有 → 跳过
├── G1 warmup：check_data_completeness() — DWD 行数 ≥250
├── G2 fresh：find_stale_ods_codes() — ODS max < calc_date → date/stock-batched 补 tail（run 内 skip）
├── G3 sync：find_stale_dwd_codes() — ODS 有当日但 DWD 落后 → rebuild_all_dwd
├── 缺 warmup → auto-fetch（策略选择器 + 熔断器 5 次）
├── 补拉后 rebuild_all_dwd() → re-check → 分类原因 → 写 ods_calc_skip_log
└── export 行数 << 预期（<80% 活跃股）→ WARNING

export（导出层）
├── 从 latest 视图直接导出（不重算）
├── 默认 filter_st 排除 ST；`--include-st` 含 ST
├── 日线+周线水平合并（无 freq 参数）；周线为空时（无 week-end≤date）仅输出日线，不崩溃
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
- **DWD 重建必须统一入口：** 调用 `rebuild_all_dwd(con, ts_codes)`，禁止单独调 `build_dwd_daily_quote` / `build_dwd_weekly_quote` / `build_dwd_daily_moneyflow`。否则必出现部分 DWD 表遗漏
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
- **趋势：** ln(vol) → 10-bar 指数加权回归（decay=0.20），阈值 0.008。与 trend_strength 统一使用加权回归
- **trend_strength：** 加权斜率/均值去量纲，正值放量/负值缩量，横截面可比
- **量价背离：** 60 日窗口 + 确认日 + 5 日去重。顶背离（价格高位+量缩），底背离（价格低位+量回升>10%）
- **区域：** 120 日百分位 + 迟滞（P90 进/P75 出，P10 进/P25 出）

### MACD 信号

- **趋势方向：** 5-bar 指数加权回归（decay=0.15），阈值 0.001
- **趋势强度：** `trend_strength` 列，加权斜率/均值去量纲
- **背离：** 60 日窗口，确认日标注，5 日去重
- **最小数据要求：** 27 条（EMA26 种子 = 前 26 日 SMA）

### MA 信号

- **斜率：** 5-bar 线性回归归一化 %/日，alignment 阈值 0.08%/日
- **alignment：** 10 值（8 方向 + tangle + sideways）

### K线形态

- **7 种形态 + 强度评分（0.0~1.0）**，向量化 NumPy 实现

### DDE 信号

- **DDX：** (大单+超大单净买入量) / 总成交量
- **DDX2：** EMA(DDX, 5)
- **背离：** 使用原始 DDX，60 日窗口 + 邻域尖刺过滤
- **趋势：** DDX2 8-bar 指数加权回归（decay=0.20），阈值 0.0001
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
- **Freshness 三门禁（calc）：** G1 warmup（DWD≥250 行 + week-end≥120 根）+ G2 stale ODS（max<calc_date）+ G3 stale DWD（ODS 有当日 DWD 无）。`run` 先 fetch 故 calc 设 `skip_stale_fetch=True`。
- **双轨 warmup：** 日线 `WARMUP_TDAYS=250`；周线 volume `WEEKLY_WARMUP_WEEKS=120`（week-end bar）。fetch/calc 门禁取两者较早起点。`check_data_completeness` 检查 `dwd_rows≥250` 且 `week_end_bars≥120`。
- **导出语义：** `-`=当日无事件信号；`N/A`=不可算或源端无数据（如亏损股 PE、历史不足量能分位）。
- **Fetch 覆盖率:** `_compute_fetch_range` 要求 100% ODS 覆盖才跳过（对齐 123 项目严格检查模式）。
- **数据质量门禁:** `_validate_ods_batch` 已接入全部 3 条 `daily` 写库路径（`fetch_by_date_range` / `_fetch_chunk` 并行 / `fetch_stocks_incremental`）。在 ODS INSERT 前校验 OHLC 逻辑（high >= low）和必需字段（open/high/low/close/vol/amount）非空，**返回过滤后的有效记录列表**，无效行丢弃并按批次打 WARNING。仅作用于 daily（OHLCV），daily_basic/moneyflow 不校验。
- **adj_factor 护栏:** `build_dwd_daily_quote` 排除 `adj_factor IS NULL` 或 `latest_adj IS NULL/0` 的行，避免静默产出 NULL `close_qfq` / 除零；插入前诊断 COUNT 被排除行数并打 WARNING（某股 qfq 不可用时可见）。
- **DWS 指纹跳过:** `check_dwd_unchanged()` 在 calc 前比对 DWD 输入数据 SHA256 指纹。
  相同 → 跳过计算；不同 → 重算。重复跑同一天从 ~107min 降到秒级。
  `input_fingerprint` 存储于每张 DWS 表的每行中，由 `insert_dws_batch()` 写入。
- **指纹检测批量化（A4）:** `load_latest_fingerprints(con, table, ts_codes)` 用
  `ROW_NUMBER()` 一次取回整组 `{ts_code: 最新指纹}`，6 个计算器在循环前预取并传入
  `check_dwd_unchanged(..., latest_fps=...)`，把 ~6.6 万次单股 SELECT 降到每组一次。
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
- **多线程 calc:** `run_calc` 将股票分 3 片并行计算，WAL 并发写入。每线程独立 DuckDB 连接。
  总 calc 从 ~58min → ~20min（3 线程）。
- **周线计算器一律只采样真周末 bar:** `dwd_weekly_quote` 为滚动周线（每交易日一行），
  全部 6 个周线计算器（含已修复的 kpattern）weekly 路径 `JOIN dim_date ... WHERE is_week_end=1`，
  禁止直接查滚动 bar。**注意：** kpattern 周线历史曾遗漏此过滤产出 intra-week 幽灵行，
  修复后须 `DELETE FROM dws_kpattern_weekly` 再重算才能从 `v_*_latest` 清除残留。

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

## 工作流程

修改代码必须遵循以下流程，不可跳步：

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
