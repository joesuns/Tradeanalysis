# CLAUDE.md

## 项目概述

Tradeanalysis — 基于 tushare + DuckDB 的 A 股技术分析数据管道。拉取全市场 OHLCV/资金流/PE 等数据，计算 MACD/MA/K线形态/DDE/量能五大类技术指标（日线+周线），通过 FastAPI 查询、CLI 导出 Excel。

- **数据源：** tushare Pro（6200 积分）
- **数据范围：** 全 A 股 5000+ 只，覆盖 2015 年至今
- **Spec：** `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` (v1.8)
- **信号优化方案：** `docs/superpowers/plans/2026-06-02-signal-timing-optimization.md`

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

# 单只股票完整流水线
python3 -m backend.cli etl --step build-all --ts-code 000001.SZ

# 指定日期范围（150 天）
python3 -m backend.cli etl --step build-all --start 20251001 --end 20260529

# 查询单只股票最新 MACD
python3 -m backend.cli query --ts-code 000001.SZ --freq daily

# 导出 Excel
python3 -c "from backend.export_wide import export_wide_to_excel; export_wide_to_excel('data/tradeanalysis.duckdb', '20260529')"

# 启动 API
uvicorn backend.api.app:app --reload

# 环境检查
python3 -m backend.cli check
python3 -m backend.cli status
```

## 项目结构

```
backend/
├── config.py              # 环境变量加载（TUSHARE_TOKEN/DUCKDB_PATH/LOG_LEVEL）
├── cli.py                 # CLI 入口（check/etl/query/export/status 5 子命令）
├── export_wide.py         # Excel 导出（中文列名、分组着色、自动格式化）
├── db/
│   ├── connection.py      # DuckDB 连接、自检、WAL checkpoint
│   └── schema.py          # 完整 DDL（24 表 + 14 视图 + 27 索引）
├── fetch/
│   ├── client.py          # tushare API 封装（限流 600/min、指数退避重试）
│   ├── ods_daily.py       # 按 trade_date 批量拉取（3 线程并行）
│   ├── ods_stock_basic.py # 个股基础信息
│   ├── ods_trade_cal.py   # 交易日历
│   ├── ods_concept.py     # 概念板块（>100 股自动切 per-concept 策略）
│   └── ods_moneyflow.py   # 资金流向
├── etl/
│   ├── base.py            # EMA/SMA/线性回归/安全浮点转换
│   ├── build_dim.py       # 维度表构建（stock/date/concept，事务保护）
│   ├── build_dwd.py       # 前复权 + 停牌检测 + 周线聚合
│   ├── calc_macd.py       # MACD(12,26,9)：EMA/趋势(加权+强度)/背离(确认日+去重)/near(est_days)/警惕
│   ├── calc_ma.py         # MA5/MA10：乖离率/5日回归斜率/10值alignment(sideways)/near(est_days)
│   ├── calc_kpattern.py   # 7种K线形态 + 分形态强度评分（Doji双路径/零体兜底/MA10趋势上下文）
│   ├── calc_dde.py        # DDX/DDX2：背离(DDX源+尖刺过滤)/趋势/警惕 周线假期感知
│   ├── calc_volume.py     # 量能：raw_vol趋势/百分位/爆量地量滞回
│   ├── orchestrator.py    # ETL 编排器（自检→拉取→DIM→DWD→DWS）
│   └── error_handler.py   # 5级错误分级（FATAL/ERROR/DEGRADED/WARN/INFO）
└── api/
    ├── app.py             # FastAPI app
    ├── router.py          # 5 端点（health/analysis/history/screening）
    └── models.py          # Pydantic 模型
```

## 数据流

```
tushare API → ODS(7表) → DIM(4表) + DWD(3表) → DWS(10表) → ADS(视图) → Excel/API
```

- **ODS：** stock_basic / daily / daily_basic / moneyflow / trade_cal / concept_detail / etl_log
- **DIM：** dim_stock / dim_date / dim_concept / dim_concept_stock
- **DWD：** dwd_daily_quote（前复权+停牌填充）/ dwd_weekly_quote / dwd_daily_moneyflow
- **DWS：** macd / ma / kpattern / dde / volume（各 daily + weekly）
- **视图：** v_dws_*_latest（10个，过滤最新 calc_date）+ v_ads_analysis_wide_*（4个，LEFT JOIN 全表）

## 关键技术细节

- **前复权公式：** `price_qfq = price × adj_factor / latest_adj_factor`（latest_adj_factor 取每只股票最晚交易日对应的 adj_factor）
- **dwd_weekly_quote 没有 is_suspended 列**，周线查询不要加此过滤
- **DWS 用 INSERT-only 快照模式**，calc_date 区分批次，60 日前数据冻结
- **所有查询必须走 v_*_latest 视图**，禁止直接查 DWS 基表
- **停牌填充到每只股票 ODS 数据的 max(trade_date)**，而非全局 dim_date
- **ods_etl_log 用 UUID 主键**，避免并发写冲突
- **DuckDB 不支持 AUTOINCREMENT**，用 INTEGER PRIMARY KEY 默认自增
- **tushare 6200 分限流实测：** ~645 次/分钟无节流，daily/daily_basic/moneyflow/adj_factor 都支持 `trade_date=xxx` 一次调用返回全市场数据

### MACD 信号

- **趋势方向：** 5-bar 指数加权回归（decay=0.15），阈值 0.001，与 trae/123 项目对齐
- **趋势强度：** `trend_strength` 列，加权斜率/均值去量纲，正值多头/负值空头
- **转折点 near_golden/near_dead：** 预估交叉天数（est_days = gap / convergence_speed）< 3 判定，替代旧的 gap<15% 空间阈值
- **背离：** 60 日窗口，确认日标注（DIF 从峰值回落 + 价格距前高 2% 内），5 日去重
- **零轴兜底：** |DEA| < close×0.1% 时用绝对阈值 close×0.01% 替代 15% 相对阈值

### MA 信号

- **斜率：** 5-bar 线性回归归一化 %/日，alignment 阈值 0.08%/日
- **alignment：** 10 值（8 方向 + tangle + sideways），tangle 需近 10 日 ≥2 次交叉 + 间距<3%
- **转折点 near_golden/near_dead：** 同 MACD，est_days<3 + 小间距 (<0.5%) 直通

### K线形态

- **Doji：** body < 10% 振幅 OR |O-C|/前收 < 0.5%
- **零体兜底：** body=0 时上影/全日振幅 > 60% 替代上影/实体 ≥ 3x
- **强度：** 7 种分形态独立维度+权重，代替代通用公式
- **阳/阴克阳：** 量条件用 5 日均量（非前一日），需要 MA10 趋势上下文

### DDE 信号

- **背离：** 使用原始 DDX（非 DDX2），60 日窗口 + 邻域尖刺过滤（±2 日 ≥ 峰值 80%）
- **趋势：** 8-bar 等权回归，阈值 0.0001，使用 DDX2
- **警惕：** 使用 DDX2，含 upturn_flat/downturn_flat（变化 ≤ 2%）
- **周线：** 第一周 7 日回溯，dim_date.is_trade_day 假期感知缺失检测

### 量能信号

- **趋势：** raw vol → ln → 10-bar 回归（去掉 SMA(5) 前置平滑），阈值 0.008
- **区域：** 120 日百分位 + 滞回（P90 进/P75 出，P10 进/P25 出）

## 已知问题和注意事项

- `concept_detail` 接口需要 `id` 或 `ts_code` 参数，空参报错。全市场用 per-concept 策略（879 调用），单股用 per-stock
- Python 3.9 不支持 `X | None` 类型语法，必须用 `Optional[X]`
- MACD 参数为 (12, 26, 9)，非 (10, 20, 7)，与他人验证时注意参数差异
- MACD 趋势已对齐 trae/123 项目（5-bar 加权回归，阈值 0.001）
- MA 斜率已改为 5-bar 回归归一化（非旧 diff(3) 公式）

## 工作流程

修改代码必须遵循以下流程，不可跳步：

1. **分析原因** — 先解释问题根因，不做任何修改
2. **制定方案** — 提出修改方案，等待用户审核
3. **用户同意** — 用户明确说"好"/"可以"/"同意"后，才进入下一步
4. **制定计划** — 写实施计划到 plan file
5. **用户审核** — 用户审批计划
6. **落地实施** — 按计划修改代码

**禁止：** 在用户同意方案前直接改代码。先问、后改。
