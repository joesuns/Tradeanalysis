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

# ===== 数据拉取 =====
# stock-batched 模式（--ts-code ≤500 只推荐）：每只股票独立检测缺失日期
python -m backend.cli fetch --ts-code 000543.SZ 600580.SH --start 20150101

# date-batched 模式（全市场）：遍历交易日
python -m backend.cli fetch --all --start 20260601

# ===== 指标计算 =====
# 指定股票（前置数据完整度检查，少量缺失自动补拉）
python -m backend.cli calc --ts-code 000543.SZ 600580.SH

# 全市场
python -m backend.cli calc --all

# 禁用自动补拉
python -m backend.cli calc --ts-code 000001.SZ --no-auto-fetch

# ===== Excel 导出 =====
# 直接导出（不重算），文件自动命名为 analysis_{date}_gen{now}.xlsx
python -m backend.cli export --date 20260603 --ts-code 000543.SZ

# 重算后导出
python -m backend.cli export --date 20260603 --ts-code 000543.SZ --recalc

# 指定输出文件
python -m backend.cli export --date 20260603 --ts-code 000543.SZ --output custom.xlsx

# ===== 查询 =====
python -m backend.cli query --ts-code 000001.SZ --freq daily

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

### CLI 三层架构

```
fetch（数据拉取层）
├── --ts-code → stock-batched（per-stock 增量，调用 adj_factor+daily+daily_basic+moneyflow）
├── --all     → date-batched（遍历交易日，全市场）
└── per-stock 增量：每只股票独立查询 ods_daily 已有日期，只补缺

calc（计算层）
├── 退市股过滤：delist_date < calc_date 且 DWS 已有 → 跳过
├── 前置检查 check_data_completeness()：验证 DWD 数据完整度
├── 缺数据 → 无条件自动补拉（warmup=27 tdays，熔断器：连续 5 次失败中止）
├── 补拉后 rebuild_all_dwd() → re-check → 分类原因 → 写 ods_calc_skip_log
├── 所有 Calculator.calculate() 返回 CalcResult（calculated + skipped 分类统计）
└── 收尾汇总：每 calc 输出 calculated/skipped 明细，skip_log 可查询

export（导出层）
├── 默认：从 latest 视图直接导出
└── --recalc → 先调 run_calc() → 再导出
```

## 关键技术细节

- **前复权公式：** `price_qfq = price × adj_factor / latest_adj_factor`（latest_adj_factor 取每只股票最晚交易日对应的 adj_factor）
- **dwd_weekly_quote 没有 is_suspended 列**，周线查询不要加此过滤
- **DWS 用 INSERT-only 快照模式**，calc_date 区分批次
- **所有查询必须走 v_*_latest 视图**，禁止直接查 DWS 基表
- **停牌填充到每只股票 ODS 数据的 max(trade_date)**，而非全局 dim_date
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
- **warmup 周期：** 系统级 warmup = 27 个交易日（MACD 功能下限，所有指标最大值）。补拉时按此窗口计算起始日期，不拉无用历史数据

## 已知问题和注意事项

- `concept_detail` 接口需要 `id` 或 `ts_code` 参数，空参报错
- Python 3.9 不支持 `X | None` 类型语法，必须用 `Optional[X]`
- MACD 参数为 (12, 26, 9)，非 (10, 20, 7)
- `build_dwd_daily_quote` 依赖 `ods_daily.adj_factor`——stock-batched 拉取时必须单独调 `adj_factor` API（daily 接口不返回此字段）
- BSE 股票无 moneyflow 数据（DDE 不可用）
- 上市不足 1 年的股票周线数据可能不足（MACD 需 ≥27 条，price_position 需 ≥60 条）
- `ema()` 函数在 `total_valid < min(period, 5)` 时返回全 NaN——极短历史股票无法计算 EMA
- **空数据处理：**
  - `run_calc()` 无条件自动补拉缺失数据（warmup=27 tdays，熔断器：连续 5 次 fetch 异常或 5 次空返回则中止）
  - 补拉范围 = `[max(上市日, analysis_end - 27tdays), min(calc_date, 退市日)]`，warmup=27 由 MACD 功能下限决定
  - 补拉失败按根因分 5 类写入 `ods_calc_skip_log`：source_unavailable / insufficient_rows / no_dwd_data / fetch_failed / delisted
  - 退市股：delist_date < calc_date 且 DWS 已有 → 跳过并记 DELISTED。首次计算退市股仍执行
  - 所有 Calculator.calculate() 返回 `CalcResult`（calculated + skipped 分类统计）
  - Price Position 各窗口独立：min_periods=2，数据不够窗口用已有全部数据
  - `v_indicator_availability` 视图提供 full/partial/missing/unavailable/historical 五态
  - 详细架构见 `docs/superpowers/plans/2026-06-04-empty-data-handling.md`

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
