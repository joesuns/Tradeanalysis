# 个股技术分析数据模型设计

> 日期: 2026-06-11 | 状态: 已确认 | 版本: v1.12

---

## 1. 概述

### 1.1 目标

基于 tushare 数据源，构建覆盖全 A 股 5-10 年历史数据的个股技术分析数据模型，支撑后续多维度指标计算和量化选股。

### 1.2 数据源

- **tushare Pro**（6000+ 积分权限），核心接口：

| tushare API | 提供数据 | 服务指标 |
|-------------|---------|---------|
| `stock_basic` | 个股基础信息（代码/名称/行业/交易所） | 基础信息(DIM) |
| `daily` | 日线 OHLCV（量:手, 额:千元）+ 涨跌幅 + 复权因子 | K线/MACD/均线/量能 + 周线聚合源 |
| `daily_basic` | PE/总市值(万元)/换手率/量比 | 基础信息 + 量能 |
| `moneyflow` | 个股资金流向，买卖明细按大中小单拆分（量:手, 额:万元）| DDE（主力净流入 + DDX 代理计算）|
| `adj_factor` | 复权因子（由 `daily` 接口返回，内含于日线数据） | 前复权价计算 |
| `concept_detail` | 概念→个股映射 | 概念维度(DIM) |
| `trade_cal` | 交易日历 | 日期维度(DIM) |

### 1.3 技术架构

- **存储引擎**：DuckDB（持久化 + 分析查询一体）
- **运行模式**：日线/周线跑批，后续扩展到 30 分钟/月线
- **DuckDB 版本**：≥ 1.0（依赖 `ON CONFLICT` UPSERT 语法、CHECK 约束强制执行、ATTACH 多文件支持）
- **部署方式**：DuckDB 持久化文件（`.duckdb`），FastAPI 通过 Python client 直连。独立 schema + 独立路由 + 独立前端页面

### 1.4 开发环境

> 本节解决"开发者在自己的机器上从零跑通第一条 ETL 需要什么"。

**环境依赖**：

| 依赖 | 最低版本 | 用途 |
|------|:------:|------|
| Python | ≥ 3.10 | ETL 脚本 + FastAPI + Excel 导出 |
| DuckDB | ≥ 1.0 | 持久化存储 + 分析查询 |
| tushare Pro | — | 行情数据源（需 6000+ 积分 token） |
| openpyxl | ≥ 3.1 | Excel 导出格式化 |
| pandas | ≥ 2.0 | DataFrame 中间处理 |
| fastapi | ≥ 0.110 | API 服务 |
| uvicorn | ≥ 0.30 | ASGI server |

**环境变量**（`.env` 文件或 shell export）：

```bash
# 必需
TUSHARE_TOKEN=your_pro_token_here       # tushare Pro token

# 可选（有默认值）
DUCKDB_PATH=./data/tradeanalysis.duckdb  # DuckDB 数据库文件路径
LOG_LEVEL=INFO                           # 日志级别: DEBUG/INFO/WARN/ERROR
ETL_WORKERS=1                            # ETL 并发线程数（默认 1，保守）
```

**Python 依赖清单**（`requirements.txt`）：

```
duckdb>=1.0.0
tushare>=1.4.0
pandas>=2.0.0
openpyxl>=3.1.0
fastapi>=0.110.0
uvicorn>=0.30.0
python-dotenv>=1.0.0
```

> `.env` 文件由 `python-dotenv` 自动加载，放在项目根目录。不要提交 `.env` 到 git（加入 `.gitignore`）。

### 1.5 快速开始

> 新开发者 5 分钟内从零到看到第一条计算结果。

```bash
# 1. 克隆项目 + 安装依赖（1 分钟）
git clone <repo-url> && cd Tradeanalysis
pip install -r requirements.txt

# 2. 配置 tushare token（30 秒）
cp .env.example .env
# 编辑 .env，填入 TUSHARE_TOKEN=your_token

# 3. 验证连通性（30 秒）
python -m backend.cli check
# 输出: ✓ DuckDB 就绪 (/data/tradeanalysis.duckdb)
#       ✓ tushare 已连接 (Pro 6000+ 积分)
#       ✗ 数据为空（首次使用需运行 ETL 初始化）

# 4. 拉取单只股票验证（1 分钟）
python -m backend.cli etl --step fetch-daily --ts-code 000001.SZ --start 20260501
# 输出: 000001.SZ: 已拉取 20 个交易日数据 → ods_daily (2026-05-01 ~ 2026-05-30)

# 5. 构建 DWD + 计算 DWS，查看第一条 MACD（2 分钟）
python -m backend.cli etl --step build-all --ts-code 000001.SZ
python -m backend.cli query --ts-code 000001.SZ --indicator macd --latest
# 输出:
# ts_code    trade_date   dif      dea      macd_bar  zone  trend
# 000001.SZ  20260530     0.1234   0.0987   0.0494    bull  up
```

> 如果第 4 步报 `tushare token 无效`，检查 `.env` 中的 `TUSHARE_TOKEN` 是否正确。
> 如果第 3 步报 `DuckDB 就绪: ✗`，检查 `data/` 目录是否存在且可写。

**时间预算**：

| 步骤 | 操作 | 预计耗时 |
|:----:|------|:-------:|
| 1 | 克隆 + 安装依赖 | 1 分钟 |
| 2 | 配置 token | 30 秒 |
| 3 | 连通性检查 | 30 秒 |
| 4 | 单只股票数据验证 | 1 分钟 |
| 5 | DWS 计算 + 首个查询 | 2 分钟 |
| **合计** | | **~5 分钟** |

---

## 2. 数仓分层架构

```
tushare API
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│  ODS 层 (7 表)        含 etl_log                                │
│  职责：原始贴源，1:1 保留 tushare 返回数据，不做清洗         │
└───────────────┬─────────────────────────────────────────────┘
                │ ETL: 数据清洗 + JOIN + 前复权计算
    ┌───────────┴───────────┐
    ▼                       ▼
┌──────────────┐    ┌──────────────────────────────────────┐
│ DIM (4 表)    │    │ DWD (3 表)                           │
│               │    │                                      │
│ dim_stock     │    │ dwd_daily_quote    — 日线明细宽表     │
│ dim_date      │    │ dwd_weekly_quote   — 周线明细宽表     │
│ dim_concept   │    │ dwd_daily_moneyflow — 资金流明细      │
│ dim_concept_stock │ │                                      │
└──────┬────────┘    └──────────┬───────────────────────────┘
       │                        │ ETL: 指标计算引擎 (DuckDB)
       │           ┌────────────┴──────────────────────────────────┐
       │           ▼            ▼          ▼          ▼          ▼
       │         dws_kpattern  dws_macd  dws_ma    dws_dde   dws_volume  dws_price_position
       │         (K线形态)     (MACD)    (均线)     (DDE)     (量能)       (价格位置)
       │           │            │          │          │          │
       │           │            │   各含日线/周线两个粒度           │
       └───────────┴────────────┴──────────┴──────────┴──────────┘
                                       │
                                       ▼  (后续扩展)
                              ADS 层 — 综合评分 / 信号共振 / 选股 / 板块宽度
```

---

## 3. ODS 层 — 原始贴源层

### 3.0 ODS diff 浮点容差（分层定稿）

ODS 写入前 `backend/fetch/ods_diff.py` 做 API vs DuckDB 行级比对；**容差与 DWS 等价性门禁分离**：

| 层 | 常量 / 阈值 | 用途 |
|----|-------------|------|
| ODS diff（价格/比率） | `FLOAT_ABS_TOL = 1e-4` | 元、% 等字段；吸收 float32 存库 vs API roundtrip |
| ODS diff（大数值） | `FLOAT_LARGE_ABS_TOL = 1.0`, `FLOAT_RTOL = 1e-5` | vol/amount/mv（手、万元）；按 scale 取 max |
| DWS 等价性 | `atol = 1e-9` | append vs FULL、batch vs selective、Wave5 narrow vs full（pytest golden） |

> ODS 层「未变更」≠ DWS 逐 bit 相等；DWS 门禁在 calc 路由与 golden 测试中单独锁定。

**FetchResult 观测字段（`to_completeness()`）：** `changed_field_events_count`、`affected_ods_columns`（列名去重排序），供 `run_fetch` / `refresh_fetch` 审计。

### 3.1 ods_stock_basic

```sql
CREATE TABLE ods_stock_basic (
    ts_code        TEXT PRIMARY KEY,
    symbol         TEXT,
    name           TEXT,
    area           TEXT,
    industry       TEXT,               -- 申万行业
    exchange       TEXT,               -- SSE / SZSE / BSE
    list_date      TEXT,               -- YYYYMMDD
    delist_date    TEXT,
    raw_json       TEXT,               -- tushare 原始响应完整备份
    fetched_at     TEXT DEFAULT (datetime('now'))
);
```

### 3.2 ods_daily

```sql
CREATE TABLE ods_daily (
    ts_code        TEXT,
    trade_date     TEXT,               -- YYYYMMDD
    open           REAL,               -- 未复权开盘价
    high           REAL,
    low            REAL,
    close          REAL,               -- 未复权收盘价
    vol            REAL,               -- 成交量（手）
    amount         REAL,               -- 成交额（千元）
    pct_chg        REAL,               -- 涨跌幅 (%)
    adj_factor     REAL,               -- 复权因子（前复权乘数）
    fetched_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (ts_code, trade_date)
);
```

### 3.3 ods_daily_basic

```sql
CREATE TABLE ods_daily_basic (
    ts_code        TEXT,
    trade_date     TEXT,
    total_mv       REAL,               -- 总市值（万元）
    pe_ttm         REAL,               -- 市盈率 TTM
    turnover_rate  REAL,               -- 换手率 (%)
    volume_ratio   REAL,               -- 量比
    fetched_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (ts_code, trade_date)
);
```

### 3.4 ods_moneyflow

> tushare `moneyflow` 接口，数据起始 2015-01-05，覆盖 ~5000+ 只股票，含买入/卖出双向拆分明细。

```sql
CREATE TABLE ods_moneyflow (
    ts_code        TEXT,
    trade_date     TEXT,
    -- 小单（<4万）
    buy_sm_vol     REAL,               -- 小单买入量（手）
    buy_sm_amount  REAL,               -- 小单买入额（万元）
    sell_sm_vol    REAL,               -- 小单卖出量
    sell_sm_amount REAL,               -- 小单卖出额
    -- 中单（4万-20万）
    buy_md_vol     REAL,
    buy_md_amount  REAL,
    sell_md_vol    REAL,
    sell_md_amount REAL,
    -- 大单（20万-100万）
    buy_lg_vol     REAL,
    buy_lg_amount  REAL,
    sell_lg_vol    REAL,
    sell_lg_amount REAL,
    -- 超大单（≥100万）
    buy_elg_vol    REAL,
    buy_elg_amount REAL,
    sell_elg_vol   REAL,
    sell_elg_amount REAL,
    -- 汇总
    net_mf_vol     REAL,               -- 主力净流入量（手）= (buy_lg_vol+buy_elg_vol) - (sell_lg_vol+sell_elg_vol)
    net_mf_amount  REAL,               -- 主力净流入额（万元）
    fetched_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (ts_code, trade_date)
);
```

### 3.5 ods_trade_cal

```sql
CREATE TABLE ods_trade_cal (
    cal_date       TEXT PRIMARY KEY,   -- YYYYMMDD
    is_open        INTEGER,            -- 1=交易日，0=非交易日
    pretrade_date  TEXT                -- 前一交易日
);
```

### 3.6 ods_concept_detail

```sql
CREATE TABLE ods_concept_detail (
    concept_name   TEXT,
    ts_code        TEXT,
    fetched_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (concept_name, ts_code)
);
```

---

## 4. DIM 层 — 维度层

### 4.1 dim_stock

```sql
CREATE TABLE dim_stock (
    ts_code        TEXT PRIMARY KEY,     -- tushare 代码（含交易所后缀，如 000001.SZ）
    stock_code     TEXT,                 -- 标准代码，直接取自 ods_stock_basic.symbol（如 000001）
    symbol         TEXT,                 -- tushare 原始 symbol，保留用于数据溯源和与 tushare 接口对齐
    name           TEXT,
    exchange       TEXT,               -- 上海 / 深圳 / 北京
    sector         TEXT,               -- 主板 / 创业板 / 科创板
    industry       TEXT,               -- 申万行业分类
    list_date      TEXT,
    delist_date    TEXT,
    is_active      INTEGER DEFAULT 1,
    is_st          INTEGER DEFAULT 0     -- 1=ST/*ST股票
);
-- stock_code 来源：直接使用 ods_stock_basic.symbol（tushare 已提供去后缀的纯数字代码）
-- 示例：ts_code='000001.SZ' → symbol='000001'
CREATE INDEX idx_dim_stock_code ON dim_stock(stock_code);
```

**归属判断**：

| 字段 | 归属 | 理由 |
|------|------|------|
| 个股名称 | DIM | 几乎不变的描述属性 |
| tushare 代码 (ts_code) | DIM | 主键级别，tushare 原始标识，含交易所后缀 |
| 标准代码 (stock_code) | DIM | 直接取自 `ods_stock_basic.symbol`，tushare 原始字段，如 000001 |
| 交易所 | DIM | 上海/深圳/北交所，静态 |
| 所在板块 | DIM | 主板/创业板/科创板，静态 |
| 所在行业 | DIM | 申万行业分类，季度调整，缓变维度 |
| 所属概念 | DIM | 概念→股票多对多映射，缓变 |
| 上市日期 | DIM | 静态 |
| 当日涨跌幅 | DWD（度量） | 每天变化 |
| 总市值 | DWD（度量） | 每天变化 |
| 市盈率 | DWD（度量） | 每天变化 |
| 成交量 | DWD（度量） | 每天变化 |
| 成交额 | DWD（度量） | 每天变化 |

### 4.2 dim_date

```sql
CREATE TABLE dim_date (
    trade_date     TEXT PRIMARY KEY,   -- YYYYMMDD
    is_trade_day   INTEGER,            -- 1=交易日
    is_week_end    INTEGER,            -- 1=本周最后一个交易日
    is_month_end   INTEGER,            -- 1=本月最后一个交易日
    is_year_end    INTEGER,            -- 1=本年最后一个交易日
    year           INTEGER,
    quarter        INTEGER,            -- 1-4
    month          INTEGER,
    week_of_year   INTEGER
);
```

### 4.3 dim_concept

```sql
CREATE TABLE dim_concept (
    concept_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_name   TEXT UNIQUE
);

CREATE TABLE dim_concept_stock (
    concept_id     INTEGER REFERENCES dim_concept(concept_id),
    ts_code        TEXT REFERENCES dim_stock(ts_code),
    PRIMARY KEY (concept_id, ts_code)
);
```

---

## 5. DWD 层 — 明细宽表层

### 5.1 dwd_daily_quote

```sql
CREATE TABLE dwd_daily_quote (
    ts_code        TEXT,
    trade_date     TEXT,
    open_qfq       REAL,               -- 前复权开盘价 = open × adj_factor / latest_adj_factor
    high_qfq       REAL,               -- 前复权最高价
    low_qfq        REAL,               -- 前复权最低价
    close_qfq      REAL,               -- 前复权收盘价
    vol            REAL,               -- 成交量（手）
    amount         REAL,               -- 成交额（千元）
    pct_chg        REAL,               -- 涨跌幅 (%)
    total_mv       REAL,               -- 总市值（万元）
    pe_ttm         REAL,               -- 市盈率 TTM
    turnover_rate  REAL,               -- 换手率 (%)
    volume_ratio   REAL,               -- 量比
    is_suspended   INTEGER DEFAULT 0,  -- 1=停牌日（OHLCV=前值, vol=0, amount=0）
    PRIMARY KEY (ts_code, trade_date)
);
```

> 来源: `ods_daily` JOIN `ods_daily_basic` ON (ts_code, trade_date)。前复权公式：`price_qfq = price × adj_factor / latest_adj_factor`，其中 `latest_adj_factor` = 该股所有历史交易日中 trade_date 最大值对应的 adj_factor（等价于最新交易日的复权因子）

### 5.2 dwd_weekly_quote

```sql
CREATE TABLE dwd_weekly_quote (
    ts_code        TEXT,
    trade_date     TEXT,               -- 周最后交易日
    open_qfq       REAL,
    high_qfq       REAL,
    low_qfq        REAL,
    close_qfq      REAL,
    vol            REAL,               -- 周成交量（折算为 5 日等效值）
    amount         REAL,               -- 周成交额（折算为 5 日等效值）
    pct_chg        REAL,
    total_mv       REAL,
    pe_ttm         REAL,
    turnover_rate  REAL,
    volume_ratio   REAL,
    active_days    INTEGER,            -- 该周实际交易天数（is_suspended=0 的日数）
    PRIMARY KEY (ts_code, trade_date)
);
```

> 来源: 不从 `ods_weekly` 建表。改为从 `ods_daily` 按周聚合生成——基于前复权价格。聚合后 JOIN `ods_daily_basic` 取周最后交易日的 PE/市值。
> 周涨跌幅不从复权价比率计算（含分红失真），改为聚合日线 `pct_chg`（对数累加）。

### 5.3 dwd_daily_moneyflow

```sql
CREATE TABLE dwd_daily_moneyflow (
    ts_code        TEXT,
    trade_date     TEXT,
    net_mf_vol     REAL,               -- 主力净流入量（手）
    net_mf_amount  REAL,               -- 主力净流入额（万元）
    buy_lg_vol     REAL,               -- 大单买入量
    sell_lg_vol    REAL,               -- 大单卖出量
    buy_elg_vol    REAL,               -- 超大单买入量
    sell_elg_vol   REAL,               -- 超大单卖出量
    total_vol      REAL,               -- 总成交量（手）= SUM(all buy vols)
    PRIMARY KEY (ts_code, trade_date)
);
```

> 来源: `ods_moneyflow` 清洗后映射。total_vol = buy_sm_vol + buy_md_vol + buy_lg_vol + buy_elg_vol（用买入侧求和；tushare 的成交分类基于主动买卖方向，买卖两侧合计在理论上市值等价，但实际因分类算法可能存在微小偏差。选用买入侧口径与 DDX 分子的大单+超大单净买入方向一致）。
> 注意：tushare 不提供 DDX/DDY/DDZ 原始值，DWS 层自行计算 DDX 代理指标。

### 5.4 DWD 增量 rebuild 语义（`DWD_INCREMENTAL=1` 默认）

日常 `run` / calc G3 走 `rebuild_dwd_for_stale(con, stale_codes, trade_date)` → `rebuild_dwd_incremental`，**禁止日常全库** `rebuild_all_dwd(con)`（无 `ts_codes` 仅限运维/首次建库）。`DWD_INCREMENTAL=0` 回退 stale 子集全量 rebuild。

| 层 | 路径 | 行为 |
|----|------|------|
| **daily** | qfq / adj 漂移 | `refresh_qfq_prices`：SQL UPDATE 四列 `open/high/low/close_qfq`，**非 DELETE** |
| **daily** | 新股（无 DWD 历史） | 该股 full `build_dwd_daily_quote` |
| **daily** | tail（其余 stale） | 仅 `trade_date` 当日 INSERT（`mode=tail=`，无 DELETE） |
| **weekly** | tail 股 | `mode=week=`：仅删/插含 `trade_date` 的周分区（`dwd_weekly_sql.py`） |
| **weekly** | qfq / insert 股 | 该股 full `build_dwd_weekly_quote` |
| **moneyflow** | stale 子集 | tail INSERT（`mode=tail=`） |

停牌填充 gap 检测改为 **DWD 行数 vs 交易日历**（非 ODS vs 日历），已填充股不再重复 LATERAL。运维：`docs/superpowers/plans/2026-06-09-daily-runbook.md`。

---

## 6. DWS 层 — 技术指标汇总层

> 按指标类型拆分为 **6** 个子表（日线 + 周线各一张，共 **12** 表），所有计算使用**前复权价格**。

---

### 6.1 dws_kpattern_daily / dws_kpattern_weekly — K 线形态

```sql
CREATE TABLE dws_kpattern_daily (
    ts_code        TEXT,
    trade_date     TEXT,
    -- 买入形态
    yang_bao_yin   INTEGER,            -- 阳包阴: 1=触发, 0=否
    yang_ke_yin    INTEGER,            -- 阳克阴: 1=触发, 0=否
    -- 卖出形态
    mu_bei_xian    INTEGER,            -- 墓碑线: 1=触发
    bi_lei_zhen    INTEGER,            -- 避雷针: 1=触发
    gao_kai_chang_yin INTEGER,         -- 高开长阴: 1=触发
    yin_bao_yang   INTEGER,            -- 阴包阳: 1=触发
    yin_ke_yang    INTEGER,            -- 阴克阳: 1=触发
    -- 强度
    strength       REAL,               -- 触发形态的强度评分 (0.0~1.0)，无形态时为 NULL
    calc_date      TEXT,               -- 计算日期
    PRIMARY KEY (ts_code, trade_date, calc_date)
);
-- 周线表结构完全相同，表名 dws_kpattern_weekly
```

#### 通用前提

> 实现：`backend/etl/calc_kpattern.py`；数值参数：`backend/kpattern_params.py`（回测调参不改计算器逻辑）。

| 项目 | 口径 |
|------|------|
| **数据源** | `dwd_daily_quote` / `dwd_weekly_quote` 前复权 OHLCV（`open_qfq`/`high_qfq`/`low_qfq`/`close_qfq`/`vol`） |
| **阴阳判定** | 阳线：`close ≥ open`；阴线：`close < open`（平盘/十字星算阳线） |
| **最小数据** | 每股 ≥ 30 根 K 线（`min_data_rows`），不足则跳过 |
| **涨跌停过滤** | `\|pct_chg\| ≥ 9.9%`（非 ST）或 `≥ 4.9%`（ST）→ 当日 7 列形态均为 0，`strength=NULL` |
| **周线** | 判定逻辑与日线相同；周线计算器仅采样 `dim_date.is_week_end=1` 的真周末 bar |
| **DWS 输出** | 7 个独立 0/1 列可**同日并存**；ADS `kpattern` 枚举合并为单一展示值（见 §12.43） |

#### 计算口径

> 以下参数为 `kpattern_params.py` 默认值，标注 `[待回测调优]` 的阈值应在实现后通过历史回测验证并调整。

| 形态 | 类型 | 判定逻辑 | 关键参数 |
|------|------|---------|---------|
| **阳包阴** | 买入 | 前日阴线 + 当日阳线 + 当日实体 > 0；当日开 ≤ 前日收 **且** 当日收 ≥ 前日开（实体吞没，不看影线）；非涨跌停日 | `ma10_filter`/`vol_filter` 已在 params 预留，**检测逻辑尚未接入** |
| **阳克阴** | 买入 | 前日阴线 + 量学"量价双向胜阴"：① 当日量 > MA5_vol × 1.2 **且** ② 当日实顶[max(开,收)] > 前日实顶 **且** ③ 收盘 > MA10。单向胜 = 不触发 | `vol_multiplier=1.2`；`ma10_filter=True` `[待回测]` |
| **墓碑线** | 卖出 | ① 上升趋势高位：close ≥ 近 60 日最高价 × 0.90，或近 20 日累计涨幅 > 15% `[待回测]`；② Doji：\|O-C\|/前收 < 0.5%，**或** 实体 < 全日振幅 10%；③ 长上影：实体 > 0 时上影 ≥ 3×实体；实体近零时上影/全日振幅 > 60% | 高位/Doji/上影阈值见 `mu_bei_xian` 块 |
| **避雷针** | 卖出 | ① 上升趋势高位（同墓碑线）；② 小实体：实体 < 全日振幅 20%，且实体中心处于全日区间下 1/3 `[待回测]`；③ 长上影规则同墓碑线 | 见 `bi_lei_zhen` 块 |
| **高开长阴** | 卖出 | ① 近 10 日累计涨幅 > 15% `[待回测]`；② 跳空高开：当日开 > 前日收；③ 当日阴线且 \|C-O\|/O ≥ 5%；④ vol > MA5_vol × 1.5 | `trend_10d_gain=0.15`；`bear_body_min=0.05`；`vol_multiplier=1.5` |
| **阴包阳** | 卖出 | 前日阳线 + 当日阴线 + 当日实体 > 0；当日开 ≥ 前日收 **且** 当日收 ≤ 前日开（实体吞没）；非涨跌停日 | — |
| **阴克阳** | 卖出 | 前日阳线 + 量学"量价双向胜阳"：① 当日量 > MA5_vol × 1.2 **且** ② 当日实底[min(开,收)] < 前日实底 **且** ③ 收盘 < MA10。单向胜 = 不触发 | `vol_multiplier=1.2`；`ma10_filter=True` `[待回测]` |

#### 强度评分 (strength)

> `strength` 输出 0.0~1.0 连续值；无形态触发时为 NULL。实现用 `if/elif` 链，**同日多形态时按下列固定顺序取第一个触发形态的公式**（与 ADS `kpattern` 展示优先级不同）：阳包阴 → 阳克阴 → 墓碑线 → 避雷针 → 高开长阴 → 阴包阳 → 阴克阳。

**买入形态强度公式**：

| 形态 | 维度 | 公式 | 权重 |
|------|------|------|:--:|
| **阳包阴** | 吞没幅度 | `min(1.0, 当日实体 / 前日实体 / 2)` | 0.5 |
| | 量能配合 | `min(1.0, 当日量 / MA5_vol / 1.5)` | 0.3 |
| | 收盘位置 | `(收-低) / (高-低)` — 光头上线趋近 1.0 | 0.2 |
| **阳克阴** | 实顶超越 | `min(1.0, (当日实顶-前日实顶) / 前日实顶 / 0.02)` | 0.4 |
| | 量能配合 | `min(1.0, 当日量 / MA5_vol / 1.5)`（MA5 不足时回退 `当日量/前日量/1.5`） | 0.4 |
| | 收盘位置 | `(收-低) / (高-低)` | 0.2 |

**卖出形态强度公式**：

| 形态 | 维度 | 公式 | 权重 |
|------|------|------|:--:|
| **墓碑线** | 上影占比 | `min(1.0, 上影/实体 / 4)`。实体近零时 `min(1.0, (H-max(O,C))/全日振幅 / 0.8)` | 0.4 |
| | Doji 纯度 | `1.0 - min(1.0, |O-C|/前收 / 0.005)` | 0.3 |
| | 高位确认 | 满足"60日高点10%内"=0.6，同时满足"20日涨>15%"=1.0 | 0.3 |
| **避雷针** | 上影占比 | 同上 | 0.4 |
| | 底部位置 | `1.0 - (收-低)/(高-低)` — 收盘越低越强 | 0.3 |
| | 高位确认 | 同墓碑线 | 0.3 |
| **高开长阴** | 阴体幅度 | `min(1.0, |C-O|/O / 0.08)` | 0.3 |
| | 量能放大 | `min(1.0, vol/MA5_vol / 2.5)` | 0.3 |
| | 跳空幅度 | `min(1.0, (O-前收)/前收 / 0.03)` | 0.2 |
| | 前序涨幅 | `min(1.0, 近10日涨幅 / 0.25)` | 0.2 |
| **阴包阳** | 吞没幅度 | `min(1.0, 当日实体 / 前日实体 / 2)` | 0.5 |
| | 量能配合 | `min(1.0, 当日量 / MA5_vol / 1.5)` | 0.3 |
| | 收盘位置 | `1.0 - (收-低)/(高-低)` — 光脚满分 | 0.2 |
| **阴克阳** | 实底超越 | `min(1.0, (前日实底-当日实底) / 前日实底 / 0.02)` | 0.4 |
| | 量能配合 | `min(1.0, 当日量 / MA5_vol / 1.5)`（MA5 不足时回退 `当日量/前日量/1.5`） | 0.4 |
| | 收盘位置 | `1.0 - (收-低)/(高-低)` | 0.2 |

> `strength = Σ(维度得分 × 权重)`。所有阈值 `[待回测]`。

---

### 6.2 dws_macd_daily / dws_macd_weekly — MACD

```sql
CREATE TABLE dws_macd_daily (
    ts_code        TEXT,
    trade_date     TEXT,
    ema_12         REAL,               -- EMA(Close, 12)
    ema_26         REAL,               -- EMA(Close, 26)
    dif            REAL,               -- DIF = EMA12 - EMA26
    dea            REAL,               -- DEA = EMA(DIF, 9)
    macd_bar       REAL,               -- MACD柱 = 2 × (DIF - DEA)
    -- 背离
    divergence     TEXT,               -- 'top_divergence' / 'bottom_divergence' / NULL
    -- 区域
    zone           TEXT,               -- 'bull' (MACD柱>0) / 'bear' (MACD柱<0)
    -- 转折点
    turning_point  TEXT,               -- 'golden_cross' / 'dead_cross' /
                                       -- 'near_golden' / 'near_dead' / NULL
    -- 警惕点
    alert          TEXT,               -- 'upturn_reverse' / 'downturn_reverse' /
                                       -- 'upturn_flat' / 'downturn_flat' / NULL
    -- 趋势
    trend          TEXT,               -- 'up' / 'down' / 'flat'
    trend_strength REAL,               -- 加权斜率/均值去量纲，横截面可比
    calc_date      TEXT,               -- 计算日期
    PRIMARY KEY (ts_code, trade_date, calc_date)
);
```

#### 计算口径

> 实现：`backend/etl/calc_macd.py`。背离调用 `backend/etl/divergence_structure.py` → `compute_macd_structure_divergence()`（通达信 Level 2 顶底结构，非 60 窗 rolling）。`RECALC_SPEC` lookback=**250**、event_tail=**10**（覆盖 ≥3 个金/死叉周期 + dedup）。

| 字段 | 计算方法 | 参数 |
|------|---------|------|
| **EMA 初始值** | 前 N 日 SMA 做种子；APPEND 模式用 `resolve_ema_seeds` 递推 | EMA26 种子需 ≥26 有效 bar |
| **基础公式** | EMA(Close,12), EMA(Close,26), DIF = EMA12-EMA26, DEA = EMA(DIF,9), MACD柱 = 2×(DIF-DEA) | α=2/(N+1) |
| **多方区域** | MACD柱 > 0 | 纯柱体符号 |
| **空方区域** | MACD柱 < 0 | 柱体 = 0 → NULL |
| **金叉** | MACD柱从 ≤0 翻正（`bar[i-1]≤0 且 bar[i]>0`） | — |
| **死叉** | MACD柱从 ≥0 翻负（`bar[i-1]≥0 且 bar[i]<0`） | — |
| **即将金叉** | DIF < DEA **且**（\|DIF-DEA\| < 0.005 小间距直通 **或** 3 日 gap 回归收敛且预估交叉 < 3 日）；零轴兜底：\|DEA\| < close×0.1% 时用绝对阈值 | 15% 为 \|DEA\| 较大时的相对阈值 |
| **即将死叉** | DIF > DEA **且** 对称于即将金叉 | 同上 |
| **上升拐头** | 柱体连续 2 日上升 → 今日柱体 < 昨日 | `upturn_reverse` |
| **下降拐头** | 柱体连续 2 日下降 → 今日柱体 > 昨日 | `downturn_reverse` |
| **上升走平** | 柱体连续 2 日上升 → 今日 \|变化\|/\|昨日\| ≤ 2% | 拐头优先于走平 |
| **下降走平** | 柱体连续 2 日下降 → 今日 \|变化\|/\|昨日\| ≤ 2% | 同上 |
| **趋势（上升）** | MACD柱 **5-bar** 指数加权回归斜率 > **0.001** | decay=0.15 |
| **趋势（下降）** | 加权斜率 < **-0.001** | 同上 |
| **趋势（走平）** | 斜率在 [-0.001, 0.001] | 同上 |
| **trend_strength** | 加权斜率 / mean(\|MACD柱\|)，有符号去量纲 | 与 trend 同窗口 |

#### 背离 (divergence)

> MACD 调用：`compute_macd_structure_divergence(close_qfq, dif, dea, macd_bar, dedup=10)`。语义对齐通达信「MACD 顶底结构」**Level 2**（直接线背 + 隔峰线背 ∧ 柱背）；**DWS 仅写入结构形成日 TG**，钝化日 T 不落库（§12.29）。非东财黑盒、非 60 bar rolling。

| 项目 | 口径 |
|------|------|
| **锚点** | 顶背离：红柱区，锚点 = **金叉** `CROSS(DIF, DEA)`；底背离：绿柱区，锚点 = **死叉** `CROSS(DEA, DIF)` |
| **段内极值** | `M1=BARSLAST(金叉)` 后：`CH1=HHV(close,M1+1)`、`DIFH1=HHV(DIF,M1+1)`、`MACDH1=HHV(MACD柱, M1+1)`（仅 `macd_bar>0`）；前段 `CH2/DIFH2/MACDH2=REF(·,M1+1)`；隔峰 `CH3/DIFH3/MACDH3` 再 REF 一档 |
| **MDIF 归一** | `mdif_part(value, ref_peak)`：INTPART 截断，`PDIFH=INTPART(LOG(\|ref\|))-1`，scale=`10^PDIFH`；顶背比较 `MDIFT` vs `MDIFH`，底背比较 `MDIFB` vs `MDIFL` |
| **钝化 T** | 直接：`CH1>CH2` ∧ `MDIFT2<MDIFH2` ∧ 红柱连续（`macd_bar>0` 且 `REF>0`）∧ `MDIFT2≥REF(MDIFT2,1)` ∧ **柱背** `MACDH1<MACDH2`；隔峰：`CH1>CH3>CH2` ∧ `MDIFT3<MDIFH3` ∧ 红柱连续 ∧ `MDIFT3≥REF` ∧ `MACDH1<MACDH3` |
| **结构形成 TG** | 前 bar 钝化 T 成立，且所用 `MDIFT` **转向**（顶：`MDIFT<REF`；底：`MDIFB>REF`）→ 候选标注日；**消失**则否决（顶：`DIFH1≥DIFH2` 或 `≥DIFH3`；底：`DIFL1≤DIFL2` 或 `≤DIFL3`） |
| **去重** | 同类背离标注后 **10 bar** 内不重复（`FILTER(dedup=10)`） |
| **标注日** | **结构形成 TG**（非钝化 T、非价格/DIF 极值当日），消除前视偏差（§12.29） |

**顶背离 `top_divergence`**：红柱区自最近金叉起，价创新高（直接或隔峰）而 DIF MDIF 未创新高（直接或隔峰），且 MACD 红柱峰值回落（柱背），于 DIF MDIF 转向日 TG 写入。

**底背离 `bottom_divergence`**：绿柱区自最近死叉起对称——价创新低、DIF MDIF 未创新低、绿柱峰值回升（柱背），于 DIF MDIF 转向日 TG 写入。

#### 6.2.1 结构背离三用与可交易门槛

> **一存三用：** DWS `divergence` 列存 **L1 结构背离**（通达信 Level 2，不变）；消费层按场景分三用：
>
> | 用途 | 列/出口 | 说明 |
> |------|---------|------|
> | **对标** | Excel `MACD结构背离` / `DDE结构背离` | 与通达信 L2 对齐，保留隔峰/TG 滞后语义 |
> | **交易** | Excel `MACD可交易背离` / `DDE可交易背离` | 三条硬门槛过滤后的 L2 消费层（export 时 enrich，不落 DWS） |
> | **诊断** | Excel `MACD背离剔除` / `DDE背离剔除` | reject_reason：`隔峰` / `滞后` / `区域` |
>
> 实现：`backend/etl/divergence_tradable.py`；trace 元数据来自 `divergence_structure.trace_*_structure_events()`。

**可交易三条硬门槛（`classify_tradable`）：**

| 门槛 | 常量 | 规则 |
|------|------|------|
| 路径 | `path == direct` | 仅 T1/B1 直接峰，拒绝 T2/B2 隔峰（`skip_peak`） |
| 时效 | `TRADABLE_TG_LAG_MAX = 1` | TG 距段内价极值 bar 数 ≤1（`tg_lag`） |
| 区域 | 顶：`macd_bar>0` / `ddx>0`；底：`<0` | 标注日柱/DDX 与背离方向一致（`zone_mismatch`） |

reject 优先级：`skip_peak` > `tg_lag` > `zone_mismatch`。L1 存在但 trace 无法复现 TG 时，fallback 保守剔除（见 `divergence_tradable._fallback_verdict`）。

**日/周/DDE 定位：**

- **周线可交易背离**：趋势/结构滤镜（慢变量）
- **日线可交易背离**：执行参考（快变量）
- **DDE 可交易背离**：辅信号；`.BJ` 无 moneyflow → N/A
- **共振策略**（`combo_eval`）：默认 `use_tradable=True`，单结构背离不作交易依据；须 K 线形态 + 可交易背离等同向共振

**Screening CLI：** `python -m scripts.screen_divergence_tradable --date YYYYMMDD [--freq daily|weekly] [--indicator macd|dde] [--tradable-only]`

**各模块调用差异**：

| 模块 | 实现函数 | 锚点/信号 | dedup | 备注 |
|------|----------|-----------|-------|------|
| MACD | `compute_macd_structure_divergence` | 金叉/死叉；线=DIF/DEA，柱=MACD柱 | 10 | `divergence_structure.py` |
| DDE | `compute_dde_structure_divergence` | `CROSS(DDX,DDX2)` / `CROSS(DDX2,DDX)`；柱背段内 DDX 峰 | 10 | 顶区 DDX 峰 **尖刺过滤**（邻域 `[peak-2,peak+3)` 内 ≥0.8×峰值 bar <2 → 剔除）；`require_finite=True` |
| Volume | `compute_price_signal_divergence` | 60 bar rolling；信号=**vol** | 5 | 仍用 `base.py` rolling 法，口径见 §6.5 |

---

### 6.3 dws_ma_daily / dws_ma_weekly — 均线

```sql
CREATE TABLE dws_ma_daily (
    ts_code        TEXT,
    trade_date     TEXT,
    ma_5           REAL,               -- MA(Close, 5)
    ma_10          REAL,               -- MA(Close, 10)
    -- 乖离率
    bias_ma5       REAL,               -- (close - MA5) / MA5 × 100
    bias_ma10      REAL,               -- (close - MA10) / MA10 × 100
    -- 斜率（5-bar 线性回归，%/日）
    ma5_slope      REAL,               -- 5-bar OLS 斜率 / 当前 MA5 × 100
    ma10_slope     REAL,               -- 5-bar OLS 斜率 / 当前 MA10 × 100
    -- 趋势（位置 + 双斜率方向组合，DWS 层存英文码）
    alignment      TEXT,               -- 8方向 + tangle + sideways + NULL
    -- 转折点
    turning_point  TEXT,               -- 'golden_cross' / 'dead_cross' /
                                       -- 'near_golden' / 'near_dead' / NULL
    calc_date      TEXT,
    PRIMARY KEY (ts_code, trade_date, calc_date)
);
```

#### 计算口径

> 实现：`backend/etl/calc_ma.py`。min_rows=11。

**基础值**：

| 字段 | 计算方法 | 参数 |
|------|---------|------|
| **MA5** | SMA(Close, 5) | 前复权收盘价 |
| **MA10** | SMA(Close, 10) | 前复权收盘价 |
| **bias_ma5** | `(close_qfq - ma_5) / ma_5 × 100` | — |
| **bias_ma10** | `(close_qfq - ma_10) / ma_10 × 100` | — |
| **ma5_slope** | 5-bar 线性回归斜率 / 当前 MA5 × 100（%/日） | `weighted_window_slopes(decay=0)` |
| **ma10_slope** | 同上，分母为 MA10 | 同上 |

**alignment 判定顺序**（Layer 1 → 2 → 3 → 8 方向；斜率平区阈值 **±0.08%/日**）：

| DWS 存储 | 条件 |
|----------|------|
| `tangle` | Layer 1：\|MA5-MA10\|/MA10 < **2.99%** 且近 10 日 MA5/MA10 交叉 ≥ **2** 次 |
| `sideways` | Layer 2：\|ma5_slope\| 与 \|ma10_slope\| 均 < 0.08%/日（且非 tangle） |
| `bull_strong` … `bear_rolling` | Layer 4：MA5 vs MA10 位置 × 双斜率方向（>0.08 上升，<-0.08 下降） |
| Layer 3 fallback | 一走平一趋势：8 格显式查表（`MACalculator.SPEC_VERSION=v2`，2026-06-14 修复） |
| NULL | MA5/MA10/斜率不可用（<11 bar、NaN） |

**Layer 3 fallback 八格映射（v2）：**

| 位置 | s5 | s10 | 结果 |
|------|----|-----|------|
| MA5>MA10 | up | flat | `bull_building` |
| MA5>MA10 | flat | up | `bull_building` |
| MA5>MA10 | dn | flat | `bull_weakening` |
| MA5>MA10 | flat | dn | `bull_weakening` |
| MA5<MA10 | dn | flat | `bear_building` |
| MA5<MA10 | flat | up | `bear_building` |
| MA5<MA10 | up | flat | `bear_weakening` |
| MA5<MA10 | flat | dn | `bear_strong` |

| DWS 存储 | MA5/MA10 | ma5_slope | ma10_slope | 交易含义 |
|----------|:--------:|:---------:|:----------:|------|
| `bull_strong` | > | > 0.08 | > 0.08 | 两线同步上行 |
| `bull_building` | > | > 0.08 | < -0.08 | MA5 上行、MA10 仍下行 |
| `bull_weakening` | > | < -0.08 | > 0.08 | MA5 先拐头向下 |
| `bull_rolling` | > | < -0.08 | < -0.08 | 两线均下行，死叉边缘 |
| `bear_strong` | < | < -0.08 | < -0.08 | 两线同步下行 |
| `bear_building` | < | < -0.08 | > 0.08 | 死叉后 MA10 惯性未消 |
| `bear_weakening` | < | > 0.08 | < -0.08 | MA5 尝试上拐 |
| `bear_rolling` | < | > 0.08 | > 0.08 | 两线均上行，金叉边缘 |

**转折点**：

| 字段 | 计算方法 | 参数 |
|------|---------|------|
| **金叉** | MA5 上穿 MA10（`ma5[i-1]≤ma10[i-1]` 且 `ma5[i]>ma10[i]`） | — |
| **死叉** | MA5 下穿 MA10 | — |
| **即将金叉** | MA5 < MA10 **且**（间距/MA10 < **0.5%** 小间距直通 **或** 3 日 gap 回归收敛且 `gap/conv_speed < 3`） | 同 MACD near 逻辑 |
| **即将死叉** | MA5 > MA10 **且** 对称于即将金叉 | 同上 |

---

### 6.4 dws_dde_daily / dws_dde_weekly — DDE

```sql
CREATE TABLE dws_dde_daily (
    ts_code        TEXT,
    trade_date     TEXT,
    net_mf_amount  REAL,               -- 主力净流入额（万元）
    ddx            REAL,               -- DDX 代理 = (大单+超大单净买入量) / 总成交量
    ddx2           REAL,               -- DDX2 = EMA(DDX, 5)
    -- 趋势（基于 DDX2）
    trend          TEXT,               -- 'up' / 'down' / 'flat'
    trend_strength REAL,               -- 加权斜率/mean(|DDX2|)
    -- 警惕点（基于 DDX2）
    alert          TEXT,               -- 'upturn_reverse' / 'downturn_reverse' /
                                       -- 'upturn_flat' / 'downturn_flat' / NULL
    -- 背离（DDX vs 价格，见 §6.2 背离专节）
    divergence     TEXT,               -- 'top_divergence' / 'bottom_divergence' / NULL
    calc_date      TEXT,               -- 计算日期（回测快照）
    PRIMARY KEY (ts_code, trade_date, calc_date)
);
```

#### 计算口径

> 实现：`backend/etl/calc_dde.py`。数据源 `dwd_daily_moneyflow` + quote JOIN；`.BJ` 无 moneyflow → 跳过。min_rows=10。

| 字段 | 计算方法 | 参数 |
|------|---------|------|
| **net_mf_amount** | 直接取自 `dwd_daily_moneyflow` | 万元 |
| **DDX** | `(buy_lg + buy_elg - sell_lg - sell_elg) / total_vol`；`total_vol=0` → NULL | 大单+超大单净买入量占比 |
| **DDX2** | EMA(DDX, 5) | SMA 种子 / APPEND `resolve_ema_seeds` |
| **趋势（上升）** | DDX2 **8-bar** 指数加权回归斜率 > **0.0001** | decay=0.20 |
| **趋势（下降）** | 加权斜率 < **-0.0001** | 同上 |
| **趋势（走平）** | 斜率在 [-0.0001, 0.0001] | 同上 |
| **trend_strength** | 加权斜率 / mean(\|DDX2\|) | 与 trend 同窗口 |
| **上升拐头** | DDX2 连续 **2** 日上升 → 今日 < 昨日 | `upturn_reverse` |
| **下降拐头** | DDX2 连续 **2** 日下降 → 今日 > 昨日 | `downturn_reverse` |
| **上升走平** | 连涨 2 日后 \|变化\|/\|昨日\| ≤ **2%** | 拐头优先 |
| **下降走平** | 连跌 2 日后 \|变化\|/\|昨日\| ≤ 2% | 同上 |
| **顶背离** | `compute_dde_structure_divergence`；锚点 `CROSS(DDX,DDX2)` + 尖刺过滤 | dedup=10；`RECALC_SPEC` lookback=250 |
| **底背离** | 同上；锚点 `CROSS(DDX2,DDX)`，底区对称 | dedup=10 |

> **DDX 说明**：tushare 不直接提供东方财富 DDX/DDY/DDZ。本方案使用 `moneyflow` 接口的大单+超大单净买入占比作为 DDX 代理——语义等价（主力资金方向），且与 DDX 的量纲一致（-1 到 +1）。DDY 和 DDZ 因需要逐单数据无法从 tushare 计算，从方案中移除。

---

### 6.5 dws_volume_daily / dws_volume_weekly — 量能

```sql
CREATE TABLE dws_volume_daily (
    ts_code        TEXT,
    trade_date     TEXT,
    ma_vol_5       REAL,               -- MA(vol, 5)
    pct_vol_rank   REAL,               -- MA5_vol 在近 120 bar 内的百分位排名 [0,100]
    volume_ratio   REAL,               -- vol / MA5_vol（量比）
    -- 区域（基于 pct_vol_rank 迟滞）
    zone           TEXT,               -- 'explosive' / 'low_volume' / 'normal'
    -- 趋势（全样本，非 zone 内子集）
    trend          TEXT,               -- 'expanding' / 'shrinking' / 'flat'
    trend_strength REAL,               -- ln(vol) 加权斜率/mean(|ln vol|)
    -- 量价背离
    divergence     TEXT,               -- 'top_divergence' / 'bottom_divergence' / NULL
    calc_date      TEXT,
    PRIMARY KEY (ts_code, trade_date, calc_date)
);
```

#### 计算口径

> 实现：`backend/etl/calc_volume.py`。min_rows=5；APPEND 时 zone 迟滞用 `zone_seed` 续接。

**周线窗口说明：** 周线 `dws_volume_weekly` 的 120 窗口指 **120 根 week-end bar**（约 2.5 年），非 120 个交易日。fetch/calc 门禁 `WEEKLY_WARMUP_WEEKS=120` 与 `dim_date.is_week_end=1` 对齐。

**calc 与 fetch 门禁分离：** 新股 `daily_ok` 即可 calc（周线 volume 窗口不足导出 N/A）；仅 mature 股 history 不足时进入 `weekly_fetch` 补拉。

| 字段 | 计算方法 | 参数 |
|------|---------|------|
| **ma_vol_5** | SMA(vol, 5) | — |
| **volume_ratio** | vol / MA5_vol | MA5=0 → NaN |
| **pct_vol_rank** | 当前 MA5_vol 在 trailing 120 bar 有效值中的百分位 | mid-rank，`len<2` → NaN |

**区域判定**（基于 `pct_vol_rank` 迟滞；APPEND 从上一 bar 的 zone 种子续接）：

| 切换方向 | 条件 | 参数 |
|---------|------|------|
| **进入爆量区** | `pct_vol_rank > 90` **且** 连续 **2** 日 | P90 进 |
| **退出爆量区** | `pct_vol_rank < 75` **且** 连续 **2** 日 | P75 出 |
| **进入地量区** | `pct_vol_rank < 10` **且** 连续 **5** 日 | P10 进 |
| **退出地量区** | `pct_vol_rank > 25` **且** 连续 **2** 日 | P25 出 |
| **正常区** | 非爆量非地量 | — |

> 进出阈值不对称形成迟滞，防止边界反复切换。

**趋势判定**（**ln(原始 vol)** 10-bar 指数加权回归，**全 bar 计算**，非 zone 内子集）：

| 趋势 | 判定 | 参数 |
|------|------|------|
| **expanding（放量）** | 加权斜率 > **0.008** | decay=0.20；窗内 ≥5 个正 vol |
| **shrinking（缩量）** | 加权斜率 < **-0.008** | 同上 |
| **flat（平量）** | 斜率在 [-0.008, 0.008] | 同上 |
| **trend_strength** | 加权斜率 / mean(\|ln vol\|) | 与 trend 同窗口 |

**量价背离**：`compute_price_signal_divergence(close_qfq, vol, window=60, dedup=5)`（`base.py` rolling 60 窗法，与 MACD/DDE 结构法独立）。顶：价近窗高（≥98%）+ vol 峰不在当日 + 当日 vol < 窗峰；底：价近窗低（≤102%）+ vol 谷不在当日 + vol 回升 >10% + 价低 ≥3 bar 前；确认日 `argmax/argmin < window-1`。

---

### 6.6 dws_price_position_daily / dws_price_position_weekly — 价格位置

```sql
CREATE TABLE dws_price_position_daily (
    ts_code              TEXT,
    trade_date           TEXT,
    price_position_60d   REAL,       -- 60 bar 滚动分位 [0,100]
    price_position_120d  REAL,       -- 120 bar 滚动分位
    price_position_250d  REAL,       -- 250 bar 滚动分位
    calc_date            TEXT,
    PRIMARY KEY (ts_code, trade_date, calc_date)
);
-- 周线表结构相同，表名 dws_price_position_weekly；窗口为 week-end bar 数
```

#### 计算口径

> 实现：`backend/etl/calc_price_position.py`。纯价格特征，**不依赖**其他 DWS 表。min_rows=2。

| 字段 | 公式 | 参数 |
|------|------|------|
| **price_position_Nd** | `(close - N_bar_low) / (N_bar_high - N_bar_low) × 100` | N ∈ {60, 120, 250}；`min_periods=2`；high=low → NaN |
| **算法** | `rolling_window_minmax_deque` 滚动 min/max | 值域 [0, 100] |

---

## 7. ADS 层 — 应用服务层（后续扩展）

> 暂不实现，后续分析需求明确后构建。

| 表 | 说明 |
|----|------|
| `ads_stock_composite_score` | 个股综合技术评分（MACD打分 + 均线打分 + DDE打分 + 量能打分 → 加权总分） |
| `ads_stock_signal` | 个股信号汇总（买入/卖出/预警信号，多指标共振判定） |
| `ads_daily_screening` | 每日选股结果（满足多条件的个股列表） |
| `ads_sector_breadth` | 板块/行业宽度（某行业多头排列占比、资金净流入排名） |
| `ads_market_overview` | 全市场概况（今日金叉数、死叉数、多头排列占比、爆量个股占比） |
| `ads_analysis_wide` | **分析宽表**（K线形态 + 均线 + MACD + DDE + 量能 + 个股基础信息 → 单表 JOIN，支持 Excel 导出） |

> **延后依赖**：`ads_sector_breadth` 和 `ads_daily_screening` 需要指数成分数据。`dim_index` + `dim_index_member` 的 DDL 见 12.2 节（已注释），待板块功能实现时引入。在此之前 DIM 层为 4 表。

### 7.1 ads_analysis_wide — 分析宽表

> 将 **6** 张 DWS 表 + dim_stock 通过 `v_*_latest` 视图 JOIN 为一张大宽表，每个交易日每只股票一行，包含全部技术指标 + 基础信息。数据可直接导出 Excel 做离线分析。

```sql
-- ============================================================
-- 日线分析宽表：v_ads_analysis_wide_daily
-- ============================================================
CREATE VIEW v_ads_analysis_wide_daily AS
SELECT
    'D'             AS freq,            -- 日线标记
    c.trade_date,
    c.ts_code,
    s.stock_code,                         -- 标准代码（去后缀）
    s.name           AS stock_name,
    s.exchange       AS exchange,
    s.sector         AS sector,
    s.industry       AS industry,
    s.is_st          AS is_st,

    q.close_qfq      AS close,
    q.pct_chg        AS pct_chg,
    q.vol            AS vol,
    q.amount         AS amount,
    q.total_mv       AS total_mv,
    q.pe_ttm         AS pe_ttm,
    q.turnover_rate  AS turnover_rate,

    -- K线形态合并为单一字段（子形态优先；阴包阳/阴克阳回测为反向买入 → contrarian_*）
    CASE
        WHEN k.yang_ke_yin = 1    THEN 'yang_ke_yin'
        WHEN k.yang_bao_yin = 1   THEN 'yang_bao_yin'
        WHEN k.yin_ke_yang = 1    THEN 'contrarian_yin_ke_yang'
        WHEN k.yin_bao_yang = 1   THEN 'contrarian_yin_bao_yang'
        WHEN k.mu_bei_xian = 1    THEN 'mu_bei_xian'
        WHEN k.bi_lei_zhen = 1    THEN 'bi_lei_zhen'
        WHEN k.gao_kai_chang_yin = 1 THEN 'gao_kai_chang_yin'
        ELSE NULL
    END              AS kpattern,
    k.strength       AS kpattern_strength,  -- 触发形态的强度 (0.0~1.0)

    c.ema_12, c.ema_26, c.dif, c.dea, c.macd_bar,
    c.divergence     AS macd_divergence,
    c.zone           AS macd_zone,
    c.turning_point  AS macd_turning_point,
    c.alert          AS macd_alert,
    c.trend          AS macd_trend,

    a.ma_5, a.ma_10,
    a.bias_ma5, a.bias_ma10,
    a.ma5_slope, a.ma10_slope,
    CASE a.alignment
        WHEN 'bull_strong'    THEN '多头强势 — 两线同步上行，持仓舒适区'
        WHEN 'bull_building'  THEN '多头初建 — MA5已拐头向上，MA10惯性下行'
        WHEN 'bull_weakening' THEN '多头衰竭 — MA5先拐头向下，即将死叉前兆'
        WHEN 'bull_rolling'   THEN '多头翻转 — 两线均下行，死叉边缘'
        WHEN 'bear_strong'    THEN '空头强势 — 两线同步下行，持币观望区'
        WHEN 'bear_building'  THEN '空头初建 — 死叉后MA10惯性未消，下跌中继'
        WHEN 'bear_weakening' THEN '空头衰竭 — MA5尝试上拐，空方减弱'
        WHEN 'bear_rolling'   THEN '空头翻转 — 两线均上行，金叉边缘'
        WHEN 'tangle'         THEN '均线缠绕 — 方向不明，观望'
        ELSE NULL
    END              AS ma_alignment,
    a.turning_point  AS ma_turning_point,

    d.net_mf_amount, d.ddx, d.ddx2,
    d.trend          AS dde_trend,
    d.alert          AS dde_alert,
    d.divergence     AS dde_divergence,   -- DDX-价格背离

    v.ma_vol_5, v.pct_vol_rank,
    v.zone           AS vol_zone,
    v.trend          AS vol_trend

FROM v_dws_macd_latest c
LEFT JOIN dim_stock s              ON c.ts_code = s.ts_code
LEFT JOIN v_dws_kpattern_latest k  ON c.ts_code = k.ts_code AND c.trade_date = k.trade_date
LEFT JOIN v_dws_ma_latest      a  ON c.ts_code = a.ts_code AND c.trade_date = a.trade_date
LEFT JOIN v_dws_dde_latest     d  ON c.ts_code = d.ts_code AND c.trade_date = d.trade_date
LEFT JOIN v_dws_volume_latest  v  ON c.ts_code = v.ts_code AND c.trade_date = v.trade_date
LEFT JOIN dwd_daily_quote      q  ON c.ts_code = q.ts_code AND c.trade_date = q.trade_date;

-- ============================================================
-- 周线分析宽表：v_ads_analysis_wide_weekly
-- ============================================================
CREATE VIEW v_ads_analysis_wide_weekly AS
SELECT
    'W'             AS freq,            -- 周线标记
    cw.trade_date,
    cw.ts_code,
    s.stock_code,                         -- 标准代码（去后缀）
    s.name           AS stock_name,
    s.exchange       AS exchange,
    s.sector         AS sector,
    s.industry       AS industry,
    s.is_st          AS is_st,

    qw.close_qfq     AS close,
    qw.pct_chg       AS pct_chg,
    qw.vol           AS vol,
    qw.amount        AS amount,
    qw.total_mv      AS total_mv,
    qw.pe_ttm        AS pe_ttm,
    qw.turnover_rate AS turnover_rate,

    -- K线形态合并为单一字段（子形态优先；阴包阳/阴克阳 → contrarian_*）
    CASE
        WHEN kw.yang_ke_yin = 1    THEN 'yang_ke_yin'
        WHEN kw.yang_bao_yin = 1   THEN 'yang_bao_yin'
        WHEN kw.yin_ke_yang = 1    THEN 'contrarian_yin_ke_yang'
        WHEN kw.yin_bao_yang = 1   THEN 'contrarian_yin_bao_yang'
        WHEN kw.mu_bei_xian = 1    THEN 'mu_bei_xian'
        WHEN kw.bi_lei_zhen = 1    THEN 'bi_lei_zhen'
        WHEN kw.gao_kai_chang_yin = 1 THEN 'gao_kai_chang_yin'
        ELSE NULL
    END              AS kpattern,
    kw.strength      AS kpattern_strength,

    cw.ema_12, cw.ema_26, cw.dif, cw.dea, cw.macd_bar,
    cw.divergence    AS macd_divergence,
    cw.zone          AS macd_zone,
    cw.turning_point AS macd_turning_point,
    cw.alert         AS macd_alert,
    cw.trend         AS macd_trend,

    aw.ma_5, aw.ma_10,
    aw.bias_ma5, aw.bias_ma10,
    aw.ma5_slope, aw.ma10_slope,
    CASE aw.alignment
        WHEN 'bull_strong'    THEN '多头强势 — 两线同步上行，持仓舒适区'
        WHEN 'bull_building'  THEN '多头初建 — MA5已拐头向上，MA10惯性下行'
        WHEN 'bull_weakening' THEN '多头衰竭 — MA5先拐头向下，即将死叉前兆'
        WHEN 'bull_rolling'   THEN '多头翻转 — 两线均下行，死叉边缘'
        WHEN 'bear_strong'    THEN '空头强势 — 两线同步下行，持币观望区'
        WHEN 'bear_building'  THEN '空头初建 — 死叉后MA10惯性未消，下跌中继'
        WHEN 'bear_weakening' THEN '空头衰竭 — MA5尝试上拐，空方减弱'
        WHEN 'bear_rolling'   THEN '空头翻转 — 两线均上行，金叉边缘'
        WHEN 'tangle'         THEN '均线缠绕 — 方向不明，观望'
        ELSE NULL
    END              AS ma_alignment,
    aw.turning_point AS ma_turning_point,

    dw.net_mf_amount, dw.ddx, dw.ddx2,
    dw.trend         AS dde_trend,
    dw.alert         AS dde_alert,
    dw.divergence    AS dde_divergence,

    vw.ma_vol_5, vw.pct_vol_rank,
    vw.zone          AS vol_zone,
    vw.trend         AS vol_trend

FROM v_dws_macd_weekly_latest cw
LEFT JOIN dim_stock s                  ON cw.ts_code = s.ts_code
LEFT JOIN v_dws_kpattern_weekly_latest kw ON cw.ts_code = kw.ts_code AND cw.trade_date = kw.trade_date
LEFT JOIN v_dws_ma_weekly_latest      aw ON cw.ts_code = aw.ts_code AND cw.trade_date = aw.trade_date
LEFT JOIN v_dws_dde_weekly_latest     dw ON cw.ts_code = dw.ts_code AND cw.trade_date = dw.trade_date
LEFT JOIN v_dws_volume_weekly_latest  vw ON cw.ts_code = vw.ts_code AND cw.trade_date = vw.trade_date
LEFT JOIN dwd_weekly_quote            qw ON cw.ts_code = qw.ts_code AND cw.trade_date = qw.trade_date;
```

> 日线 46 列，周线 46 列。`freq` 字段区分粒度（D/W），同一查询模板可复用。
> v1.4 变更：K 线新增 `kpattern_strength`；均线新增 `bias_ma5/bias_ma10/ma5_slope/ma10_slope`，移除 `ma5_flat/ma10_flat/ma_all_flat`，`ma_alignment` 改为中文展示；DDE 新增 `dde_divergence`。净增 3 列（43→46）。

```sql
-- ============================================================
-- 上证指数宽表：v_ads_index_wide
-- ============================================================
CREATE VIEW v_ads_index_wide AS
SELECT
    'D'             AS freq,
    c.trade_date,
    c.ts_code,
    '000001'        AS stock_code,
    '上证指数'       AS index_name,

    q.close_qfq      AS close,
    q.pct_chg        AS pct_chg,
    q.vol            AS vol,
    q.amount         AS amount,

    -- K线形态合并为单一字段（子形态优先；阴包阳/阴克阳 → contrarian_*）
    CASE
        WHEN k.yang_ke_yin = 1    THEN 'yang_ke_yin'
        WHEN k.yang_bao_yin = 1   THEN 'yang_bao_yin'
        WHEN k.yin_ke_yang = 1    THEN 'contrarian_yin_ke_yang'
        WHEN k.yin_bao_yang = 1   THEN 'contrarian_yin_bao_yang'
        WHEN k.mu_bei_xian = 1    THEN 'mu_bei_xian'
        WHEN k.bi_lei_zhen = 1    THEN 'bi_lei_zhen'
        WHEN k.gao_kai_chang_yin = 1 THEN 'gao_kai_chang_yin'
        ELSE NULL
    END              AS kpattern,
    k.strength       AS kpattern_strength,

    c.ema_12, c.ema_26, c.dif, c.dea, c.macd_bar,
    c.divergence     AS macd_divergence,
    c.zone           AS macd_zone,
    c.turning_point  AS macd_turning_point,
    c.alert          AS macd_alert,
    c.trend          AS macd_trend,

    a.ma_5, a.ma_10,
    a.bias_ma5, a.bias_ma10,
    a.ma5_slope, a.ma10_slope,
    CASE a.alignment
        WHEN 'bull_strong'    THEN '多头强势 — 两线同步上行，持仓舒适区'
        WHEN 'bull_building'  THEN '多头初建 — MA5已拐头向上，MA10惯性下行'
        WHEN 'bull_weakening' THEN '多头衰竭 — MA5先拐头向下，即将死叉前兆'
        WHEN 'bull_rolling'   THEN '多头翻转 — 两线均下行，死叉边缘'
        WHEN 'bear_strong'    THEN '空头强势 — 两线同步下行，持币观望区'
        WHEN 'bear_building'  THEN '空头初建 — 死叉后MA10惯性未消，下跌中继'
        WHEN 'bear_weakening' THEN '空头衰竭 — MA5尝试上拐，空方减弱'
        WHEN 'bear_rolling'   THEN '空头翻转 — 两线均上行，金叉边缘'
        WHEN 'tangle'         THEN '均线缠绕 — 方向不明，观望'
        ELSE NULL
    END              AS ma_alignment,
    a.turning_point  AS ma_turning_point,

    -- DDE 不适用（指数无资金流向），全 NULL
    NULL             AS net_mf_amount,
    NULL             AS ddx,
    NULL             AS ddx2,
    NULL             AS dde_trend,
    NULL             AS dde_alert,
    NULL             AS dde_divergence,

    v.ma_vol_5, v.pct_vol_rank,
    v.zone           AS vol_zone,
    v.trend          AS vol_trend

FROM v_dws_macd_latest c
LEFT JOIN v_dws_kpattern_latest k ON c.ts_code = k.ts_code AND c.trade_date = k.trade_date
LEFT JOIN v_dws_ma_latest      a ON c.ts_code = a.ts_code AND c.trade_date = a.trade_date
LEFT JOIN v_dws_volume_latest  v ON c.ts_code = v.ts_code AND c.trade_date = v.trade_date
LEFT JOIN dwd_daily_quote      q ON c.ts_code = q.ts_code AND c.trade_date = q.trade_date
WHERE c.ts_code = '000001.SH';

-- ============================================================
-- 上证指数周线版本
-- ============================================================
CREATE VIEW v_ads_index_wide_weekly AS
SELECT
    'W'             AS freq,
    c.trade_date,
    c.ts_code,
    '000001'        AS stock_code,
    '上证指数'       AS index_name,
    q.close_qfq      AS close,  q.pct_chg AS pct_chg,  q.vol AS vol,  q.amount AS amount,
    CASE
        WHEN k.yang_ke_yin = 1    THEN 'yang_ke_yin'
        WHEN k.yang_bao_yin = 1   THEN 'yang_bao_yin'
        WHEN k.yin_ke_yang = 1    THEN 'contrarian_yin_ke_yang'
        WHEN k.yin_bao_yang = 1   THEN 'contrarian_yin_bao_yang'
        WHEN k.mu_bei_xian = 1    THEN 'mu_bei_xian'
        WHEN k.bi_lei_zhen = 1    THEN 'bi_lei_zhen'
        WHEN k.gao_kai_chang_yin = 1 THEN 'gao_kai_chang_yin'
        ELSE NULL
    END              AS kpattern,
    k.strength       AS kpattern_strength,
    c.ema_12, c.ema_26, c.dif, c.dea, c.macd_bar,
    c.divergence AS macd_divergence, c.zone AS macd_zone,
    c.turning_point AS macd_turning_point, c.alert AS macd_alert, c.trend AS macd_trend,
    a.ma_5, a.ma_10, a.bias_ma5, a.bias_ma10, a.ma5_slope, a.ma10_slope,
    CASE a.alignment
        WHEN 'bull_strong'    THEN '多头强势 — 两线同步上行，持仓舒适区'
        WHEN 'bull_building'  THEN '多头初建 — MA5已拐头向上，MA10惯性下行'
        WHEN 'bull_weakening' THEN '多头衰竭 — MA5先拐头向下，即将死叉前兆'
        WHEN 'bull_rolling'   THEN '多头翻转 — 两线均下行，死叉边缘'
        WHEN 'bear_strong'    THEN '空头强势 — 两线同步下行，持币观望区'
        WHEN 'bear_building'  THEN '空头初建 — 死叉后MA10惯性未消，下跌中继'
        WHEN 'bear_weakening' THEN '空头衰竭 — MA5尝试上拐，空方减弱'
        WHEN 'bear_rolling'   THEN '空头翻转 — 两线均上行，金叉边缘'
        WHEN 'tangle'         THEN '均线缠绕 — 方向不明，观望'
        ELSE NULL
    END              AS ma_alignment,
    a.turning_point AS ma_turning_point,
    NULL AS net_mf_amount, NULL AS ddx, NULL AS ddx2, NULL AS dde_trend, NULL AS dde_alert,
    NULL AS dde_divergence,
    v.ma_vol_5, v.pct_vol_rank, v.zone AS vol_zone, v.trend AS vol_trend
FROM v_dws_macd_weekly_latest c
LEFT JOIN v_dws_kpattern_weekly_latest k ON c.ts_code = k.ts_code AND c.trade_date = k.trade_date
LEFT JOIN v_dws_ma_weekly_latest      a ON c.ts_code = a.ts_code AND c.trade_date = a.trade_date
LEFT JOIN v_dws_volume_weekly_latest  v ON c.ts_code = v.ts_code AND c.trade_date = v.trade_date
LEFT JOIN dwd_weekly_quote            q ON c.ts_code = q.ts_code AND c.trade_date = q.trade_date
WHERE c.ts_code = '000001.SH';
```

> 与个股宽表列结构一致（46 列），DDE 列填 NULL。日线/周线各一个版本。

> **扩展建议**：当前硬编码 `WHERE c.ts_code = '000001.SH'`。后续如需支持多个指数（沪深300、中证500 等），建议改为从 `dim_index` 表读取指数代码列表，动态生成 UNION ALL 视图或使用表值函数，避免为每个指数手写 VIEW。

### 7.2 Excel 导出

```python
# backend/export_wide.py
import duckdb
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font
from openpyxl.utils import get_column_letter

VIEW_MAP = {
    "daily":  "v_ads_analysis_wide_daily",
    "weekly": "v_ads_analysis_wide_weekly",
}
INDEX_VIEW_MAP = {
    "daily":  "v_ads_index_wide",
    "weekly": "v_ads_index_wide_weekly",
}

def export_wide_to_excel(
    db_path: str,
    trade_date: str,       # YYYYMMDD
    output_path: str,      # .xlsx 路径
    freq: str = "daily",   # "daily" | "weekly"
    filter_st: bool = True,
    include_index: bool = True
):
    """导出分析宽表到 Excel。个股和上证指数分不同 sheet。"""
    view = VIEW_MAP[freq]

    con = duckdb.connect(db_path)

    # ---- Sheet 1: 个股 ----
    sql_stocks = f"SELECT * FROM {view} WHERE trade_date = ?"
    params = [trade_date]
    if filter_st:
        sql_stocks += " AND is_st = 0"
    df_stocks = con.execute(sql_stocks, params).df()
    df_stocks = _reorder_signal_first(df_stocks)

    # ---- Sheet 2: 上证指数 (可选) —— 与 freq 同粒度 ----
    df_index = None
    if include_index:
        index_view = INDEX_VIEW_MAP[freq]
        df_index = con.execute(
            f"SELECT * FROM {index_view} WHERE trade_date = ?", [trade_date]
        ).df()
        df_index = _reorder_signal_first(df_index)

    con.close()

    # ---- 写入 Excel ----
    wb = Workbook()
    # 删除默认空 sheet
    wb.remove(wb.active)

    _write_sheet(wb, f"个股_{freq}", df_stocks)
    if df_index is not None and len(df_index) > 0:
        _write_sheet(wb, "上证指数", df_index)

    wb.save(output_path)
    return len(df_stocks) + (len(df_index) if df_index is not None else 0)


def _reorder_signal_first(df: "pd.DataFrame") -> "pd.DataFrame":
    """重排列：信号列紧随标识列，数值列放后面。打开 Excel 即可看到信号。"""
    head = ["freq", "trade_date", "ts_code", "stock_code", "stock_name", "exchange",
            "sector", "industry", "is_st", "close", "pct_chg"]
    signals = [
        # K线形态
        "kpattern", "kpattern_strength",
        # MACD 信号
        "macd_divergence", "macd_zone", "macd_turning_point", "macd_alert", "macd_trend",
        # 均线信号
        "ma_alignment", "ma_turning_point", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope",
        # DDE 信号
        "dde_trend", "dde_alert", "dde_divergence",
        # 量能信号
        "vol_zone", "vol_trend",
    ]
    tail = [c for c in df.columns if c not in head and c not in signals]
    ordered = [c for c in head + signals + tail if c in df.columns]
    return df[ordered]


def _write_sheet(wb: Workbook, sheet_name: str, df: "pd.DataFrame"):
    """将 DataFrame 写入一个 sheet，带表头冻结 + K线信号颜色标注。"""
    ws = wb.create_sheet(title=sheet_name)

    green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    red   = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    blue  = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=10)

    # 表头
    for col_idx, col_name in enumerate(df.columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill = header_fill
        cell.font = header_font
    ws.freeze_panes = "A2"

    # 数据
    for row_idx, row in enumerate(df.itertuples(index=False), 2):
        for col_idx, value in enumerate(row, 1):
            ws.cell(row=row_idx, column=col_idx, value=value)

    # 信号高亮：K线形态（单一枚举列；阴包阳/阴克阳回测为反向买入 → 绿色）
    kpattern_colors = {
        'yang_bao_yin': green, 'yang_ke_yin': green,
        'mu_bei_xian': red, 'bi_lei_zhen': red,
        'gao_kai_chang_yin': red, 'yin_bao_yang': red, 'yin_ke_yang': red,
        'contrarian_yin_bao_yang': green, 'contrarian_yin_ke_yang': green,
    }
    if 'kpattern' in df.columns:
        col_idx = list(df.columns).index('kpattern') + 1
        for row_idx in range(2, len(df) + 2):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val in kpattern_colors:
                ws.cell(row=row_idx, column=col_idx).fill = kpattern_colors[val]

    # 信号高亮：文本枚举列（按值匹配）
    text_signal_cols = {
        'macd_turning_point': {'golden_cross': green, 'dead_cross': red},
        'macd_zone':           {'bull': green, 'bear': red},
        'macd_divergence':     {'top_divergence': red, 'bottom_divergence': green},
        'ma_alignment': {
            '多头强势': green, '多头初建': green,
            '多头衰竭': blue, '多头翻转': blue,
            '空头强势': red,  '空头初建': red,
            '空头衰竭': blue, '空头翻转': blue,
            '均线缠绕': blue, '均线走平': blue,
        },
        'ma_turning_point':    {'golden_cross': green, 'dead_cross': red},
        'dde_trend':           {'up': green, 'down': red},
        'dde_divergence':      {'top_divergence': red, 'bottom_divergence': green},
        'vol_zone':            {'explosive': red, 'low_volume': blue},
        'vol_trend':           {'expanding': green, 'shrinking': red},
    }
    for col_name, value_colors in text_signal_cols.items():
        if col_name in df.columns:
            col_idx = list(df.columns).index(col_name) + 1
            for row_idx in range(2, len(df) + 2):
                val = ws.cell(row=row_idx, column=col_idx).value
                if val in value_colors:
                    ws.cell(row=row_idx, column=col_idx).fill = value_colors[val]

    # 列宽
    for col_idx in range(1, len(df.columns) + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 14
```

> 用法：`export_wide_to_excel("tradeanalysis.db", "20260530", "output.xlsx", freq="daily")`
> 输出：Sheet 1「个股_daily」≈5000 行 + Sheet 2「上证指数」1 行（include_index=True）

---

## 8. ETL 链路

```
Step 1 — 拉取 ODS
  tushare API → ods_stock_basic / ods_daily / ods_daily_basic /
                ods_moneyflow / ods_trade_cal / ods_concept_detail

Step 2 — 构建 DIM
  ods_stock_basic → dim_stock
    转换明细：
      - stock_code = ods_stock_basic.symbol  （tushare 已提供去后缀代码，无需拆分）
      - sector      = 从 ts_code 前缀推导   （12.24 规则）
      - is_st       = 从 name 检测 ST/*ST   （12.33 规则）
      - exchange    = 值映射：SSE→上海, SZSE→深圳, BSE→北京
                      映射表: {'SSE': '上海', 'SZSE': '深圳', 'BSE': '北京'}
  ods_trade_cal      → dim_date
  ods_concept_detail → dim_concept + dim_concept_stock

Step 3 — 构建 DWD（前复权宽表，UPSERT 写入，与 ODS 幂等策略一致）
  ods_daily + ods_daily_basic + adj_factor → dwd_daily_quote
    含停牌日填充（OHLCV=前值, vol=0, amount=0, is_suspended=1）
  ods_daily(按周聚合) + ods_daily_basic → dwd_weekly_quote
  ods_moneyflow → dwd_daily_moneyflow

Step 4 — 构建 DWS（DuckDB 分析引擎计算）
  dwd_daily_quote        → dws_kpattern_daily / dws_macd_daily /
                            dws_ma_daily / dws_volume_daily
  dwd_daily_moneyflow    → dws_dde_daily
  dwd_weekly_quote       → 对应的 6 张周线表

Step 5 — 后续
  DWS → ADS（根据后续分析需求扩展）
```

### 8.1 ETL CLI 接口

> 所有 ETL 操作通过统一的 `backend/cli.py` 入口触发，子命令覆盖完整的数据流水线。

```bash
# ============================================================
# CLI 命令总览
# ============================================================
python -m backend.cli --help

# 输出:
# Usage: python -m backend.cli <command> [options]
#
# Commands:
#   check          检查环境连通性（DuckDB + tushare + 磁盘空间）
#   etl            执行 ETL 步骤
#   query          查询 DWS 指标数据
#   export         导出分析宽表到 Excel
#   status         查看数据库状态（各表行数、最新 trade_date、数据新鲜度）
#
# Options:
#   --help, -h     显示帮助
#   --dry-run      仅打印将要执行的操作，不实际写入
#   --verbose, -v  详细日志输出
```

**`etl` 子命令**：

```bash
python -m backend.cli etl --help

# 输出:
# Usage: python -m backend.cli etl [options]
#
# Options:
#   --step <step>         ETL 步骤: fetch-ods | build-dim | build-dwd | calc-dws | build-all
#   --ts-code <code>      仅处理指定股票（默认: 全部），如 000001.SZ
#   --start <YYYYMMDD>    起始日期（增量模式默认: 上次断点日期）
#   --end <YYYYMMDD>      截止日期（默认: 最近交易日）
#   --workers <N>         tushare 并发拉取线程数（默认: 1）
#   --batch-size <N>      每批处理股票数（默认: 100）
#   --dry-run             仅打印将执行的操作，不实际拉取/写入
#   --force-full           强制全量重算，忽略增量窗口
#
# 示例:
#   python -m backend.cli etl --step fetch-ods --ts-code 000001.SZ --start 20260501
#   python -m backend.cli etl --step build-all --dry-run
#   python -m backend.cli etl --step calc-dws --ts-code 000001.SZ
#   python -m backend.cli etl --step build-all  # 全量流程
```

**`query` 子命令**：

```bash
python -m backend.cli query --ts-code 000001.SZ --indicator macd --latest

# 输出:
# ts_code    trade_date   dif      dea      macd_bar  divergence  zone  trend
# 000001.SZ  20260530     0.1234   0.0987   0.0494    NULL        bull  up

python -m backend.cli query --ts-code 000001.SZ --indicator all --date 20260530 --format json
```

**`export` 子命令**：

```bash
python -m backend.cli export --date 20260530 --output ./analysis_20260530.xlsx --freq daily
# 默认排除 ST 股票。如需包含 ST，加 --include-st 标志

# 输出:
# 已导出 4,987 行 → ./analysis_20260530.xlsx
#   Sheet 1: 个股_daily (4,987 rows)
#   Sheet 2: 上证指数 (1 row)
```

**`status` 子命令**：

```bash
python -m backend.cli status

# 输出:
# ============================================================
# 数据库状态: /data/tradeanalysis.duckdb (7.2 GB)
# ============================================================
# 表                  行数        最新日期      新鲜度
# ods_daily           12,340,000  2026-05-30   fresh
# ods_daily_basic     12,340,000  2026-05-30   fresh
# ods_moneyflow       12,100,000  2026-05-30   fresh
# dwd_daily_quote     12,500,000  2026-05-30   fresh
# dws_macd_daily      21,000,000  2026-05-30   fresh (latest calc_date)
# ...
# ETL 最后运行: 2026-05-31 16:30 CST  状态: success
# 下次建议跑批: 2026-05-31 16:00 CST (今日收盘后)
```

### 8.2 ETL 错误分级策略

> 错误分级决定 ETL 在处理失败时的行为——跳过、重试、降级、还是中止。五级分类覆盖从致命错误到正常信息的全频谱。

| 级别 | 标识 | 含义 | 处理动作 | 示例 |
|:----:|------|------|---------|------|
| **FATAL** | 致命 | 环境不可用，继续运行无意义 | 立即中止整个 ETL，写 `ods_etl_log`，退出码 1 | DuckDB 文件不可读写、磁盘空间 < 100 MB |
| **ERROR** | 错误 | 当前操作失败，但不影响其他股票 | 跳过当前股票/批次，继续处理后续，写 `ods_etl_log.status='failed'` | tushare 对该股票返回错误、单只股票 INSERT 违反 CHECK 约束 |
| **WARN** | 告警 | 数据质量可疑，但计算可继续 | 计算继续，`error_msg` 记录告警信息，`status='success'` | 某股某日 vol=0 但非停牌日、复权因子异常跳变 >50% |
| **DEGRADED** | 降级 | 数据可用但新鲜度下降（非错误） | 写 `ods_etl_log.status='degraded'`，API `freshness.status='stale'`，ETL 继续运行 | tushare 持续不可用导致数据落后 >1 天、daily_basic 未到导致部分列延迟 |
| **INFO** | 信息 | 正常流程中的关键事件 | 仅日志输出，不写 `ods_etl_log` | "批次 37/50 完成 (000001.SZ-002300.SZ)" |

**FATAL 条件清单**：

| 条件 | 检测方式 | 错误消息模板 |
|------|---------|-------------|
| DuckDB 文件不可读写 | `ATTACH` 或 `BEGIN TRANSACTION` 失败 | `FATAL: DuckDB 数据库不可访问 — {path} ({os_error})` |
| 磁盘空间不足 | 写前检查可用空间 | `FATAL: 磁盘空间不足 — 需要 {required_mb} MB，仅剩 {available_mb} MB` |
| Python 版本过低 | 启动时 `sys.version_info` 检查 | `FATAL: Python 版本不满足 — 需要 ≥3.10，当前 {version}` |
| DuckDB 版本过低 | `SELECT version()` 解析 | `FATAL: DuckDB 版本不满足 — 需要 ≥1.0，当前 {version}` |

**ERROR 恢复策略**：单只股票 ERROR 后，该股票的下次 ETL 自动重试（幂等 UPSERT）。连续 3 次 ERROR 的同只股票告警升级为 FATAL 日志行（但**不中止**全局 ETL——只跳过该股，下次继续重试）。

**WARN 阈值示例**：

| 条件 | 阈值 | 告警消息模板 |
|------|------|-------------|
| 复权因子异常 | `\|adj_factor - LAG(adj_factor)\|/LAG(adj_factor) > 0.5` | `WARN: {ts_code} {trade_date} 复权因子跳变 {pct}%，可能数据修正` |
| vol=0 非停牌 | `vol=0 AND is_suspended=0` | `WARN: {ts_code} {trade_date} 成交量为零但非停牌日，可能数据缺失` |

---

## 9. 指标口径速查表

### 9.1 K 线形态

| 指标 | 类型 | 核心逻辑 | 关键参数 |
|------|------|---------|---------|
| 阳包阴 | 买入 | 前阴+当阳实体吞没；涨跌停日过滤 | — |
| 阳克阴 | 买入 | 前阴+量价双向胜：量>MA5×1.2 且 实顶>前实顶 且 收>MA10 | vol×1.2；MA10 |
| 墓碑线 | 卖出 | 高位+Doji+长上影(≥3x实体或零实体时上影/振幅>60%) | 60日高点90%内或20日涨>15% |
| 避雷针 | 卖出 | 高位+小实体(<振幅20%且在下1/3)+长上影 | 同墓碑线高位规则 |
| 高开长阴 | 卖出 | 10日涨>15%+跳空高开+阴体≥5%+量>MA5×1.5 | 见 `gao_kai_chang_yin` |
| 阴包阳 | 卖出 | 前阳+当阴实体吞没；涨跌停日过滤 | — |
| 阴克阳 | 卖出 | 前阳+量价双向胜：量>MA5×1.2 且 实底<前实底 且 收<MA10 | vol×1.2；MA10 |
| **strength** | 强度 | 0.0~1.0 加权评分，无形态=NULL；多形态同日取 `if/elif` 固定顺序（§6.1） | `[待回测]` |
| **ADS kpattern** | 展示 | DWS 7 列合并为枚举；阴包阳/阴克阳映射为 `contrarian_*`（回测反向买入） | 子形态优先 |

### 9.2 MACD

| 子类 | 指标 | 判定 |
|------|------|------|
| 基础 | DIF/DEA/MACD柱 | EMA(12,26,9)，SMA 种子 / APPEND 递推 |
| 背离 | 顶背离 | 结构法：金叉锚点 + 直接/隔峰线背(MDIF) ∧ 红柱柱背；TG 日标注；10 bar 去重 |
| | 底背离 | 死叉锚点对称；绿柱柱背；TG 日标注 |
| 区域 | 多方/空方 | MACD柱 > 0 / < 0 |
| 转折点 | 金叉/死叉 | MACD柱符号翻转 |
| | 即将金叉/死叉 | gap<0.005 或 3日收敛预估<3日（零轴兜底见 §6.2） |
| 警惕点 | 上升/下降拐头 | 柱体连涨/跌2日后逆转 |
| | 上升/下降走平 | 连涨/跌2日后日变化≤2% |
| 趋势 | 上升/下降/走平 | MACD柱 5-bar 加权回归(decay=0.15)，阈值 ±0.001 |
| | trend_strength | 加权斜率/mean(\|柱\|) |

### 9.3 均线

| 子类 | 指标 | 判定 |
|------|------|------|
| 基础 | MA5/MA10 | 前复权收盘价 SMA |
| | 乖离率 | (close-MA)/MA×100 |
| | 斜率 | 5-bar OLS 斜率/当前MA×100（%/日） |
| 趋势 | alignment | tangle(间距<2.99%+10日交叉≥2) → sideways(双斜率<0.08%/日) → 8方向；Layer3 单斜率 fallback |
| 转折点 | 金叉/死叉 | MA5 上穿/下穿 MA10 |
| | 即将金叉/死叉 | 间距/MA10<0.5% 或 3日 gap 收敛预估<3日 |

### 9.4 DDE

| 子类 | 指标 | 判定 |
|------|------|------|
| 基础 | DDX | (大单+超大单净买入)/total_vol；.BJ 无数据 |
| | DDX2 | EMA(DDX,5) |
| | net_mf_amount | 主力净流入额（万元） |
| 趋势 | 上升/下降/走平 | DDX2 8-bar 加权回归(decay=0.20)，阈值 ±0.0001 |
| | trend_strength | 加权斜率/mean(\|DDX2\|) |
| 警惕点 | 上升/下降拐头 | DDX2 连涨/跌2日后逆转 |
| | 上升/下降走平 | 连涨/跌2日后变化≤2% |
| 背离 | 顶/底背离 | `compute_dde_structure_divergence`；DDX/DDX2 交叉锚点 + 柱背；顶区尖刺过滤；TG 日标注 |

### 9.5 量能

| 子类 | 指标 | 判定 |
|------|------|------|
| 基础 | ma_vol_5 / volume_ratio | SMA(vol,5)；vol/MA5_vol |
| | pct_vol_rank | MA5_vol 在 120 bar 内百分位 |
| 区域 | 爆量/地量/正常 | pct_vol_rank 迟滞：>90×2日进/<75×2日出；<10×5日进/>25×2日出 |
| 趋势 | expanding/shrinking/flat | ln(vol) 10-bar 加权回归，阈值 ±0.008 |
| | trend_strength | 加权斜率/mean(\|ln vol\|) |
| 背离 | 量价顶/底背离 | 同 §6.2，信号=vol |

### 9.6 价格位置

| 子类 | 指标 | 判定 |
|------|------|------|
| 分位 | 60/120/250 bar | `(close-N_low)/(N_high-N_low)×100`；min_periods=2 |

---

## 10. 数据量预估

| 表 | 粒度 | 预估行数 (10年全A股) | 预估存储 |
|----|------|---------------------|---------|
| ods_stock_basic | 每只股一行 | ~5,000 | < 1 MB |
| ods_daily | 日 × 股 | ~12,000,000 | ~500 MB |
| ods_daily_basic | 日 × 股 | ~12,000,000 | ~300 MB |
| ods_moneyflow | 日 × 股 | ~12,000,000 | ~500 MB |
| ods_trade_cal | 日期 | ~3,650 | < 1 MB |
| ods_concept_detail | 概念 × 股 | ~100,000 | ~5 MB |
| ods_etl_log | ETL每次一行 | ~10,000 | < 1 MB |
| DWD (3表) | 日/周 × 股 | ~26,000,000 | ~1 GB |
| DWS 日线 (5表) | 日 × 股 | ~105,000,000 | ~4.0 GB |
| DWS 周线 (5表) | 周 × 股 | ~15,000,000 | ~600 MB |
| DIM (4表) | — | ~150,000 | ~10 MB |
| **总计** | | | **~7.0 GB** |

> DWS 日线行数估算：基础 12.5M（5000股 × 2500日 × 1快照）+ 快照膨胀 ~8.5M（近60日每 trade_date 累积 ~30 calc_date 快照）= ~21M/表 × 5表 ≈ 105M。DuckDB 列式引擎在此量级秒级响应，7 GB 存储完全可行。

---

## 11. 接口依赖

| tushare API | 频率 | 增量策略 |
|-------------|------|---------|
| `stock_basic` | 按需（季度） | 全量刷新，对比 `list_date`/`delist_date` |
| `daily` | 日（收盘后） | 按 `trade_date` 增量拉取，去重 upsert。复权因子内含 |
| `daily_basic` | 日（收盘后） | 同上 |
| `moneyflow` | 日（收盘后） | 同上 |
| `adj_factor` | 日（随 `daily` 接口一并返回） | 无需独立拉取；daily 数据中 `adj_factor` 字段直接用于前复权计算 |
| `concept_detail` | 按需（季度） | 全量刷新 |
| `trade_cal` | 按需（年度） | 全量刷新 |

### 11.1 全量初始加载 — API 限流与耗时估算

> tushare Pro 6000 积分的默认限流为 **200 次/分钟**，单次调用最多返回约 5000 行（取决于接口）。全量加载 10 年全 A 股数据涉及 ~7,500 次 API 调用。

| 接口 | 调用次数估算 | 说明 |
|------|:----------:|------|
| `daily` | ~2,500 | 每批 ~2000 只 × 500 日，或按 stock 逐个拉 |
| `daily_basic` | ~2,500 | 同上 |
| `moneyflow` | ~2,500 | 同上 |
| `stock_basic` | 1 | 单次全量 |
| `concept_detail` | 1 | 单次全量 |
| `trade_cal` | 1 | 单次全量 |
| **总计** | **~7,500** | |

**耗时估算**（单线程，200 次/分钟）：

| 场景 | 耗时 | 说明 |
|------|:----:|------|
| 理论最快 | ~38 分钟 | 7,500 / 200，无重试无抖动 |
| 实际（含重试+指数退避） | **2-4 小时** | 网络抖动 + 偶发限流触发退避 |
| 3 线程并发 | **1-2 小时** | 注意：tushare 对并发连接数无公开承诺，建议 ≤3 |

**全量加载断点续传**：

```
策略（利用 ods_etl_log 记录进度）：
  1. 加载前检查 ods_etl_log 是否有未完成的 fetch 任务
  2. 按 ts_code 切片分批次（每批 100 只），每批完成写入 ods_etl_log
  3. 中断后重启：查询 ods_daily 中每只股票的 max(trade_date)，
     跳过已达目标日期的股票，从断点继续
  4. 三次重试失败的股票记录到 ods_etl_log（status='failed'），
     不阻塞整体流程
```

> **并发建议**：tushare 未公开承诺并发连接上限。实测中 ≤3 并发线程通常是安全的，超过可能触发 IP 级别限流（HTTP 429 或连接拒绝）。建议实现时提供 `--workers N` 参数，默认值为 1（保守）。

### 11.2 FastAPI 端点设计

> `/api/v1/` 前缀，独立 router 挂载到 FastAPI app。所有端点返回 JSON，带 `freshness` 标记。

**端点清单**：

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v1/analysis/{ts_code}` | 单只股票最新技术指标 |
| `GET` | `/api/v1/analysis/{ts_code}/history` | 单只股票历史指标时间序列 |
| `GET` | `/api/v1/screening` | 多条件选股筛选 |
| `GET` | `/api/v1/market/overview` | 全市场概况（金叉数、多头占比等） |
| `GET` | `/api/v1/health` | 数据库新鲜度检查 |

**1. 单只股票最新指标**：

```
GET /api/v1/analysis/000001.SZ?freq=daily

Response 200:
{
  "ts_code": "000001.SZ",
  "stock_code": "000001",
  "stock_name": "平安银行",
  "trade_date": "20260530",
  "freq": "daily",
  "close": 12.56,
  "pct_chg": 1.23,
  "macd": {
    "dif": 0.1234, "dea": 0.0987, "macd_bar": 0.0494,
    "zone": "bull", "trend": "up", "divergence": null,
    "turning_point": null, "alert": null
  },
  "ma": {
    "ma_5": 12.34, "ma_10": 12.18,
    "bias_ma5": 1.78, "bias_ma10": 3.12,
    "alignment": "多头强势 — 两线同步上行，持仓舒适区",
    "turning_point": null
  },
  "dde": {
    "net_mf_amount": 15230.5, "ddx": 0.0234, "ddx2": 0.0189,
    "trend": "up", "alert": null, "divergence": null
  },
  "kpattern": { "type": null, "strength": null },
  "volume": {
    "ma_vol_5": 45000000, "pct_vol_rank": 72.5,
    "zone": "normal", "trend": "expanding"
  },
  "freshness": { "age_days": 0, "status": "fresh" }
}
```

**错误响应**：

```json
GET /api/v1/analysis/999999.XZ

Response 404:
{
  "error": {
    "code": "STOCK_NOT_FOUND",
    "message": "股票代码 '999999.XZ' 不存在",
    "cause": "ts_code 格式错误或该股票已退市",
    "fix": "请使用 tushare 标准代码格式（如 000001.SZ）。运行 'python -m backend.cli status --stocks' 查看所有可用代码。",
    "doc_url": "/api/docs#stock-not-found"
  }
}
```

```
GET /api/v1/analysis/000001.SZ?freq=daily
(数据库 3 天未更新)

Response 200:
{
  ...,
  "freshness": { "age_days": 3, "status": "stale" }
}
```

> `freshness.status` = `"stale"` 时，前端应展示横幅提示用户数据可能不是最新的。

**2. 历史时间序列**：

```
GET /api/v1/analysis/000001.SZ/history?freq=daily&fields=dif,dea,macd_bar,close&from=20260501&to=20260530

Response 200:
{
  "ts_code": "000001.SZ",
  "freq": "daily",
  "fields": ["trade_date", "close", "dif", "dea", "macd_bar"],
  "rows": [
    ["20260501", 12.10, 0.1000, 0.0800, 0.0400],
    ["20260502", 12.23, 0.1100, 0.0850, 0.0500],
    ...
  ],
  "count": 20,
  "freshness": { "age_days": 0, "status": "fresh" }
}
```

> `fields` 参数支持列选择以减少传输量。`rows` 返回数组的数组（列式），客户端可按索引解析。

**3. 多条件选股**：

```
GET /api/v1/screening?freq=daily&macd_zone=bull&ma_alignment=bull_strong&min_ddx=0.01&limit=50

Response 200:
{
  "conditions": {
    "freq": "daily", "macd_zone": "bull",
    "ma_alignment": "bull_strong", "min_ddx": 0.01
  },
  "count": 23,
  "results": [
    { "ts_code": "000001.SZ", "stock_code": "000001", "stock_name": "平安银行", ... },
    ...
  ],
  "freshness": { "age_days": 0, "status": "fresh" }
}
```

**4. 健康检查**：

```
GET /api/v1/health

Response 200:
{
  "database": "connected",
  "latest_trade_date": "20260530",
  "freshness": { "age_days": 0, "status": "fresh" },
  "table_stats": {
    "ods_daily": {"rows": 12340000, "latest_date": "20260530"},
    "dws_macd_daily": {"rows": 21000000, "latest_calc_date": "20260531"}
  },
  "last_etl": {"finished_at": "2026-05-31T16:30:00+08:00", "status": "success"}
}
```

**API 错误码约定**：

| HTTP 状态 | 错误码 | 含义 | 示例触发条件 |
|-----------|--------|------|-------------|
| 400 | `INVALID_PARAM` | 请求参数格式错误 | `freq=monthly`（不支持） |
| 400 | `DATE_OUT_OF_RANGE` | 日期超出数据范围 | `from=20100101`（数据始于 2015） |
| 404 | `STOCK_NOT_FOUND` | 股票代码不存在或已退市 | `ts_code=999999.XZ` |
| 503 | `DATA_STALE` | 数据过期，查询不可靠 | `age_days > 7` 且用户要求 fresh-only |
| 500 | `INTERNAL_ERROR` | 数据库或计算引擎异常 | DuckDB 文件损坏、CHECK 约束意外违反 |

---

## 12. 审核修订记录

> 2026-05-31 `/plan-ceo-review` SELECTIVE EXPANSION 审核。以下修改将在实现阶段应用。

### 12.1 审核决定

| # | 类别 | 决定 | 结论 |
|---|------|------|------|
| 1 | 架构 | DuckDB 统一存储 + 分析引擎（v1.5） | DWS 表直接 INSERT INTO DuckDB |
| 2 | 架构 | 全部 DWS 表加 `(ts_code, trade_date DESC)` 复合索引 | 新增索引 |
| 3 | 架构 | adj_factor 从接口列表移除重复声明，daily/weekly 自带 | 保留显式声明 |
| 4 | 错误处理 | 补充 ETL 异常处理策略：API 重试+指数退避、LEFT JOIN 不丢行 | 新增章节 |
| 5 | 数据边界 | 退市过滤、停牌补 NULL 行、新股<120日百分位降级 NULL | 新增策略 |
| 6 | 测试 | 加测试策略：单元测试+回归测试+一致性校验 | 新增章节 |
| 7 | 性能 | DWS 增量 60 日滑动窗口重算，EMA 误差<0.01% | 新增策略 |
| 8 | 可观测 | 新增 `ods_etl_log` 表记录每步 ETL 状态 | 新增表 |
| C-1 | 扩展 | 新增 `dim_index` + `dim_index_member` 指数成分维表 | 新增表 |
| C-2 | 扩展 | 全部 DWS 表加 `calc_date` 字段支持回测快照 | 字段变更 |
| C-3 | 扩展 | DWS DDL 建议元数据驱动生成，避免日线/周线重复手写 | 实现备注 |

### 12.2 新增表 DDL

```sql
-- ETL 元数据
CREATE TABLE ods_etl_log (
    id                INTEGER PRIMARY KEY,
    step_name         TEXT,               -- 'fetch_daily' / 'build_dwd' / 'calc_dws_macd' ...
    started_at        TEXT,
    finished_at       TEXT,
    status            TEXT,               -- 'success' / 'failed' / 'running'
    row_count         INTEGER,            -- 处理行数
    error_msg         TEXT,
    data_completeness TEXT                -- JSON: 各依赖表最新 trade_date，如 {"ods_daily":"20260530","ods_daily_basic":"20260529"}
);

-- 指数维度（延后实现，ADS 板块功能时引入，详见 12.63）
-- CREATE TABLE dim_index ( ... );
-- CREATE TABLE dim_index_member ( ... );
```

### 12.3 DWS 表字段变更

所有 DWS 日线/周线表的 `calc_date` 字段：

```sql
-- 在每张 DWS 表中增加（主键随之扩展）
ALTER TABLE dws_macd_daily ADD COLUMN calc_date TEXT DEFAULT NULL;
-- calc_date = 计算该行指标的日期（YYYYMMDD），T+1 跑批时为 trade_date 后第一日
```

### 12.4 DWS 索引

> DWS 表需要 **两组索引** 覆盖两种查询模式。

```sql
-- 时间序列索引：按股票拉历史（以 dws_macd_daily 为例）
CREATE INDEX idx_macd_code_date ON dws_macd_daily(ts_code, trade_date DESC);

-- 横截面索引：按日期拉全市场（交易员盘前查询的核心路径）
CREATE INDEX idx_macd_date_code ON dws_macd_daily(trade_date, ts_code);
-- 其余 9 张 DWS 表各加两组索引，共 20 条
```

### 12.4b ODS / DWD 层索引

> DWD 层是 DWS 计算的数据源，增量计算需要按 `ts_code` + `trade_date` 范围快速定位。ODS 层增量拉取需要按日期去重。

```sql
-- ============================================================
-- DWD 层索引
-- ============================================================

-- dwd_daily_quote（~1200 万行）：DWS 日线计算的核心数据源
-- 每次增量计算按 ts_code 拉最近 60 日数据
CREATE INDEX idx_dwd_daily_code_date ON dwd_daily_quote(ts_code, trade_date);

-- dwd_daily_moneyflow：DDE 计算的数据源，查询模式同上
CREATE INDEX idx_dwd_mf_code_date ON dwd_daily_moneyflow(ts_code, trade_date);

-- dwd_weekly_quote：周线 DWS 的数据源
CREATE INDEX idx_dwd_weekly_code_date ON dwd_weekly_quote(ts_code, trade_date);

-- ============================================================
-- ODS 层索引
-- ============================================================

-- 加速增量拉取的去重判断（判断某 trade_date 是否已存在）
CREATE INDEX idx_ods_daily_date ON ods_daily(trade_date);
CREATE INDEX idx_ods_daily_basic_date ON ods_daily_basic(trade_date);
CREATE INDEX idx_ods_moneyflow_date ON ods_moneyflow(trade_date);
```

> 缺失 DWD 索引会导致 DuckDB 每次增量计算扫描全表来定位每只股票的最新数据。ODS 日期索引将增量 UPSERT 的去重检查从全表扫描降为索引查找。

### 12.5 数据边界处理策略

| 场景 | 处理方式 |
|------|---------|
| 退市股票 | DWD/DWS 计算截止到 delist_date，之后不再产出新行 |
| 停牌日 | DWD 中保留该行，OHLCV=前一交易日收盘价，vol=0，amount=0，`is_suspended=1` |
| 停牌检测 | 通过 `dwd_daily_quote.is_suspended` 显式标记，不使用 `vol=0` 启发式判断。`is_suspended` 由 ODS→DWD ETL 中对比 `dim_date` 交易日历确定（某只股票在交易日无行情数据 = 停牌） |
| 停牌日 EMA 窗口 | **跳过停牌日，不消耗 EMA 窗口**。EMA 递推仅计入 `is_suspended=0` 的有效交易日，LAG 引用前一个有效交易日。复牌后指标连续性好，不被停牌填充值平滑化 |
| 复牌首日 | 连续停牌 ≥ 5 个交易日后复牌的首日（通过 `is_suspended` 连续计数判定），所有信号强制=NULL（12.33 规则） |
| 新股冷启动 | 上市不足 120 日时，量能百分位/区域字段返回 NULL。上市不足对应窗口期时，所有依赖滚动窗口的指标（EMA/MACD/DDX2）统一返回 NULL，各窗口阈值由各指标定义 |
| IPO 30 日过滤 | 上市不足 **30 个交易日**的股票，所有 DWS 指标=NULL。此过滤在最外层执行，优先级高于各指标的窗口期规则 |
| 复权因子缺失 | 当日前复权价格=NULL，DWS 行跳过（不产出），记录告警日志 |

### 12.6 测试策略

| 类型 | 内容 | 覆盖目标 |
|------|------|---------|
| 单元测试 | 每个指标函数一个参数化用例（给定 OHLCV，assert 精确值） | DWS 所有计算列 |
| 回归测试 | 10 只经典股票 + 精选形态日期作为黄金数据集 | K线形态/MACD 背离 |
| 一致性校验 | DWS 行数 = DWD 行数（每只股票），每日 ETL 后自动运行 | 全部 DWS 表 |
| 基线对比 | 与 tushare 或聚宽平台的 MACD 值交叉验证（抽样 100 只） | MACD 基础值 |

### 12.7 DWS 增量计算策略

```
每日跑批流程（INSERT-only 快照模式）：
  0. ETL 启动自检：DuckDB 数据库文件可读写
  1. ODS 增量拉取最新 N 日数据，INSERT INTO ODS 表
  2. DuckDB 计算 DWS（直接读 DWD 表；CALC_INCREMENTAL=1 时窄读窄写）
  3. INSERT INTO DWS 表（新 calc_date 行），每股窄写 [recalc_start, calc_date]
  4. 快照保留由 prune_dws_snapshots(keep_runs=N) 运维清理（默认 5 次 calc_date）
  5. calc_date = 当前跑批日期
  6. 墙钟耗时记入 ods_etl_log（观测项，非硬 KPI）
```

**重算窗口（RecalcSpec 注册表，禁止 magic number）：**

| 概念 | 含义 |
|------|------|
| `RecalcSpec` | 各 Calculator 声明 `lookback + seed + event_tail + min_rows` |
| `resolve_recalc_bars()` | `max(total) + safety(5)`；当前 daily 聚合值 **255** |
| `recalc_start` | `calc_date` 向前回溯 255 交易日（指纹域 + DWS 窄写起点） |
| `load_start` | `recalc_start` 再向前 `max(lookback)-1` bar（EMA/PP 种子正确性） |
| `CALC_INCREMENTAL=0` | 回退全量读/写（指纹 skip 仍可用） |
| `CALC_WORKERS` | 计算**线程**并行度，默认 `min(cpu-1, 8)`（DuckDB 单文件禁跨进程写，故用线程池） |
| `CALC_APPEND=1`（默认） | 新交易日双路径追算：每股每 freq 每指标按 `dws_calc_state` 路由 SKIP/APPEND/FULL；`=0` 回退 12.7 窄窗 |
| `CALC_FAST_SKIP=1`（默认） | chunk 级 preflight v2：按指标 partial skip（`preflight_stock_modes_v2` + `calc_stock_pipeline_selective`）；BSE DDE 空帧视为 SKIP；需 `CALC_APPEND`；`=0` 回退 |
| `CALC_BATCH_APPEND=1`（默认） | 全市场新日 `run_calc` 在 ThreadPool 前走 `run_batch_append_phase()`：APPEND/SKIP 股按 `(indicator,freq)` 跨股批处理（共享尾窗加载 + batch 种子）；FULL/fallthrough 股仍进 `_calc_stock_chunk`；`--ts-code` 子集或 `=0` 回退逐股 APPEND；需 `CALC_APPEND`；设计见 `docs/superpowers/plans/2026-06-08-cross-stock-batch-append.md` |
| `CALC_SKIP_STATE_REFRESH=1`（默认） | 同日复跑 SKIP 路径：当 `history_fp` 未变且 `updated_calc_date` 同 `calc_date` 时跳过冗余 `dws_calc_state` UPSERT；`=0` 回退旧行为（每次 SKIP 仍写 state）；需 `CALC_APPEND`；设计见 `docs/superpowers/plans/2026-06-08-calc-performance-special.md` |

**同日复跑 partial skip（CALC_FAST_SKIP v2）：** v1 实库 **834s→630s**（12 指标全 SKIP 才短路）。v2 见 `docs/superpowers/plans/2026-06-08-calc-partial-skip-v2.md`：`SKIP` 指标直接记 `fingerprint_match`，仅 `APPEND`/`FULL` 走 selective pipeline；DDE weekly batch SQL `tail_window=245`。

**新日追算（append-only，CALC_APPEND）：** 见独立设计 `docs/superpowers/specs/2026-06-07-calc-append-only-design.md`。要点：

- **状态表 `dws_calc_state`** — PK `(ts_code, freq, indicator)`，列 `last_trade_date / history_fp / quote_latest_adj / spec_version / updated_calc_date`，每指标一行。缺失（新股/首部署）→ FULL 建基线。
- **路由 `classify_calc_mode()`** — 无 state→FULL；强签名变（除权/填充/修正）→FULL；无新 bar 同签名→SKIP；有新 bar 同签名→APPEND。
- **强签名 `state_signature()`** — 对 `last_td` 前**固定 245 根尾窗**的输入值序列（按各计算器 `SIGNATURE_COLS`）做 SHA256，替代弱的 min/max/mean 指纹；固定宽度保证跨运行稳定。误判方向只会多一次安全 FULL，绝不误 APPEND（新 bar 始终全窗重算）。
- **APPEND `append_calculate()`** — 仅算/写新 bar；EMA/ddx2 `resolve_ema_seeds` 种子递推、Volume 迟滞 zone 种子、滚动类复用全尾窗算法。INSERT-only 不改写历史。与 FULL 逐值 `atol=1e-9` 等价（`tests/test_etl/test_append_calc.py`）。

**Calc Spec Version 与 SKIP 不变量（2026-06-14）：**

```
允许 SKIP ⇔ input_fingerprint 同 AND spec_version 同 AND 无新 bar
```

- **双门禁：** `classify_calc_mode(..., expected_spec_version)`（路由层）与 `check_dwd_unchanged(..., expected_spec_version)`（per-stock calculate / batch_full `check_spec=True`）须同时生效。
- **注册表：** `backend/etl/calc_indicators.INDICATOR_SPEC_VERSIONS` — 12 管线 `(indicator,freq) → Calculator.SPEC_VERSION`。
- **禁止旁路：** `_modes_from_state_only` / `--force` 同日复跑短路 **不得**在 `dws_calc_state.spec_version` 落后时全 SKIP；`has_spec_stale_indicators()` 阻断整段 idempotent skip。
- **运维刷新：** `cli calc --refresh-spec ma[,volume]` 或 spec 发布日 `CALC_FORCE_HARD=1 calc --force`（窄窗 FULL，**非** DWD rebuild）。Runbook：`docs/superpowers/plans/2026-06-09-daily-runbook.md`「算法 SPEC_VERSION 发布」。
- **质检视图：** `v_dq_spec_freshness`（按指标/freq 统计 latest 截面 spec_ok/spec_stale）；`health_check` Section J。

- **范围外后续项：** 端到端「秒级日更」仍受 ~320s 全市场 freshness-fetch 拖累，需单独立项。

**P3 热路径优化（与全量 golden-master 等价）：**

- PricePosition：`rolling_window_minmax_deque` O(n) 滚动极值
- MACD/DDE：`load_ema_seed` 从上一 calc_date DWS 读 EMA 状态再递推
- 背离：MACD/DDE → `divergence_structure` 结构法（Level 2 隔峰柱背，TG 标注，dedup=10，lookback=250）；Volume → `compute_price_signal_divergence` 60 窗 + 5 bar dedup

### 12.7b ETL 故障恢复与数据回补

> 当 ETL 因 tushare 不可用等原因停顿超过 1 天后重启时，自动检测并回补缺失数据。

```
故障恢复流程（ETL 任务启动时自动执行）：
  1. 检查 ods_daily 的 max(trade_date)，对比 dim_date 中最近的交易日
  2. 断层判定：
     - 差 0-1 天 → 正常增量跑批（12.7 流程）
     - 差 2-60 天 → 补拉缺失日期的 ODS → DWD 补建行 → DWS 60 日窗口重算
     - 差 >60 天 → 补拉 ODS → DWD 补建行 → 对受影响股票触发全量历史重算
                    （超过冻结窗口，解冻后重算全部历史）
  3. ods_etl_log 记录 recovery 状态（step_name='recovery_fill_ods' 等）
  4. DWS 全部重算完毕后，data_freshness 自动恢复为 fresh
  5. 若 tushare 持续不可用，标记 ods_etl_log.status='degraded'，
     API 查询返回时附带 freshness 标记（age_days + stale/fresh 状态）
```

> 数据恢复是幂等的——补拉与增量拉取共用同一套 UPSERT 逻辑，重复执行不会产生重复行。

---

### 12.8 ETL 部分失败处理

```
粒度：单只股票为独立事务
策略：ODS 写入 UPSERT，失败股票跳过 + ods_etl_log 记录，其余股票继续
重试：失败股票下次跑批自动重试（幂等）
DWD/DWS：仅对成功写入 ODS 的股票计算，不因部分失败阻塞全局
```

### 12.8b 数据到达延迟处理

> tushare 每日数据分批到达（15:30-18:00），`daily_basic`（含 PE/市值）通常比 `daily` 晚到。跑批前需做完整性检查。

```
策略：
  1. 跑批前检查所有依赖表的最新 trade_date 是否一致
  2. 若 daily 已到但 daily_basic 未到：
     - DWD 行延迟产出，等待所有依赖数据到齐
     - 不产出「残缺」DWD 行（PE/市值=NULL），避免下游 DWS 计算时静默使用不完整数据
  3. ods_etl_log 增加 data_completeness 字段，记录各依赖表的最新 trade_date
  4. 跑批脚本入口做完整性守卫：若任一核心表（daily/daily_basic/moneyflow）的
     max(trade_date) < 预期跑批日期，跳过本次跑批并告警
```

### 12.8c ETL 日志扩展

> `data_completeness` 字段已直接纳入 12.2 节主 DDL。以下 ALTER TABLE 仅作为已部署数据库的升级参考，新建数据库无需执行。

```sql
-- 已纳入 12.2 主 DDL，此 ALTER TABLE 仅用于已部署数据库升级
ALTER TABLE ods_etl_log ADD COLUMN data_completeness TEXT;
-- JSON 格式示例：
-- {"ods_daily":"20260530", "ods_daily_basic":"20260530", "ods_moneyflow":"20260529"}
```

### 12.9 DWS 计算引擎架构

```
架构：混合 Python + DuckDB
├── 有状态计算（EMA/MA递推）→ Python for 循环 + NumPy
└── 无状态计算（形态/趋势/百分位/区域）→ DuckDB SQL 窗口函数
    结果直接 INSERT INTO DuckDB DWS 表，无中间格式转换

组织：每个 DWS 表对应一个独立 Calculator 类（继承 BaseCalculator）
初始化：首次全量用 DuckDB PARTITION BY ts_code 分批窗口函数 + 分批写入
```

### 12.10 DWS 枚举列 CHECK 约束

```sql
-- 示例：dws_macd_daily（枚举列 CHECK）
CHECK (divergence    IN ('top_divergence', 'bottom_divergence', NULL))
CHECK (zone          IN ('bull', 'bear'))
CHECK (turning_point IN ('golden_cross', 'dead_cross', 'near_golden', 'near_dead', NULL))
CHECK (alert         IN ('upturn_reverse', 'downturn_reverse', 'upturn_flat', 'downturn_flat', NULL))
CHECK (trend         IN ('up', 'down', 'flat'))

-- dws_kpattern_daily（布尔列 CHECK + 值域 CHECK）
CHECK (yang_bao_yin       IN (0, 1))
CHECK (yang_ke_yin        IN (0, 1))
CHECK (mu_bei_xian        IN (0, 1))
CHECK (bi_lei_zhen        IN (0, 1))
CHECK (gao_kai_chang_yin  IN (0, 1))
CHECK (yin_bao_yang       IN (0, 1))
CHECK (yin_ke_yang        IN (0, 1))
CHECK (strength IS NULL OR (strength >= 0.0 AND strength <= 1.0))

-- dws_volume_daily（值域 CHECK）
CHECK (pct_vol_rank >= 0 AND pct_vol_rank <= 100)
CHECK (zone IN ('explosive', 'low_volume', 'normal'))
CHECK (trend IN ('expanding', 'shrinking', 'flat'))

-- dws_ma_daily（枚举 CHECK）
CHECK (alignment IN ('bull_strong','bull_building','bull_weakening','bull_rolling',
                      'bear_strong','bear_building','bear_weakening','bear_rolling',
                      'tangle', NULL))
CHECK (turning_point IN ('golden_cross', 'dead_cross', 'near_golden', 'near_dead', NULL))

-- dws_dde_daily（枚举 CHECK）
CHECK (trend IN ('up', 'down', 'flat'))
CHECK (alert IN ('upturn_reverse', 'downturn_reverse', 'upturn_flat', 'downturn_flat', NULL))
CHECK (divergence IN ('top_divergence', 'bottom_divergence', NULL))
```

> **DuckDB CHECK 约束**：DuckDB 强制执行 CHECK 约束，INSERT 违反约束的行会抛出 `ConstraintException`。上述 CHECK 作为数据库层面的最后防线，Python ETL 层的 `validate()` 方法同时做业务侧预检。

### 12.11 周线 DWS 一致性声明

> 周线 DWS 表结构与日线完全一致。所有计算公式、参数、CHECK 约束、calc_date 逻辑从日线表复用。仅数据源为 dwd_weekly_quote，trade_date 为周最后交易日。
>
> **实现方式**：周线 5 张 DWS 表的 DDL、索引、CHECK 约束、`v_*_latest` 视图均从同一模板渲染生成（日线后缀 `_daily` → 周线后缀 `_weekly`，数据源 `dwd_daily_*` → `dwd_weekly_*`），避免手写日线/周线两套 DDL 产生不一致。元数据驱动方式参见 12.7 节建议。

### 12.12 测试数据源

| 测试类型 | 数据源 / Ground Truth |
|---------|---------------------|
| MACD 基线 | tushare `macd()` 接口返回值（同一数据源的官方计算） |
| K线形态 | 通达信公式源码对照计算 |
| 回归覆盖 | 主板≥3 + 创业板≥2 + 科创板≥1 + ≥3 行业 |
| 极端行情 | 2015-07-08(股灾底) / 2019-01-03 / 2020-03-23 / 2022-04-26 / 2024-02-05 |

### 12.13 初始全量加载策略

```
首次构建 10 年全 A 股历史：
  1. DuckDB 连接数据库文件
  2. 分批查询：每批 100 只股票，WHERE ts_code IN (...)
  3. 每批执行 PARTITION BY ts_code ORDER BY trade_date + 窗口函数 + LAG()
  4. INSERT INTO DWS 表（每批 100 只股票一个事务），释放 DuckDB 内存后处理下一批
  5. 重复步骤 2-4 直到处理完所有股票（约 50 批次）
  6. 验证：DWS 行数 = DWD 行数（每只股票）

后续每日增量：60 日窗口 + Python/DuckDB 混合方案
```

> **分批策略说明**：全量初始化时，若一次性加载所有股票到 DuckDB 做窗口函数计算，内存中需持有 1250 万行 × ~50 列 = 2-5 GB。改为分批查询（每批 100 只股票）+ 分批窗口函数 + 分批 INSERT：每批在 DuckDB 中完成 PARTITION BY + LAG() 计算后直接 INSERT INTO 写入，释放内存再处理下一批。约 50 批次完成全量初始化。

### 12.14 EMA 种子窗口规格

> 所有 EMA 计算的初始种子使用前 N 日 SMA，N = EMA 周期参数。

| 指标 | 种子窗口 | 公式 |
|------|---------|------|
| EMA12 (MACD) | SMA(前12日 close) | 第13日起 EMA12 值可用 |
| EMA26 (MACD) | SMA(前26日 close) | 第27日起 EMA26 值可用 |
| DEA (EMA9 of DIF) | SMA(前9日 DIF) | DIF 有效后的第10日起 DEA 可用 |
| DDX2 (EMA5 of DDX) | SMA(DDX, 前5日) | 第6日起 DDX2 值可用 |

> 种子期内 DWS 行照常写入，但以下字段标记 NULL：DIF（前12日内）、DEA（前26日内）、MACD柱（前26日内）、DDX2（前5日内）。周线同理。
>
> **MACD 完整可用时间线**（新股上市后）：
> - 第 1-12 个交易日：DIF=NULL（EMA12 种子期）
> - 第 13-26 个交易日：DIF 可用，但 EMA26 种子未满 → DEA=NULL → MACD柱=NULL
> - 第 27-35 个交易日：DIF/DEA 可用（EMA26 种子满 26 日 + DEA 种子满 9 日 DIF）→ MACD柱 可用
> - **第 36 个交易日起**：MACD 全字段可用（对齐 12.33 IPO 30 日过滤规则——IPO <30 日所有 DWS=NULL，自然包含了 MACD 冷启动窗口）

### 12.15 "即将金叉/即将死叉" 零轴边界修正

> 当 DEA 接近 0 时，\|DIF-DEA\|/\|DEA\| 会异常膨胀，导致即将金叉/死叉在零轴附近系统性漏报。

**修正方案**：双条件兜底。

```
常规判定（|DEA| ≥ close × 0.1%）：
  即将金叉 = DIF < DEA 且 差值收窄 且 |DIF-DEA|/|DEA| < 15%

零轴兜底（|DEA| < close × 0.1%）：
  即将金叉 = DIF < DEA 且 差值收窄 且 |DIF-DEA| < close × 0.01%
```

> 即当 DEA 相对股价已经极小时，改用绝对阈值（收盘价的 0.01%）替代相对阈值。即将死叉同理。

### 12.16 周线数据源：日线聚合

> 经 tushare 实际调试验证：`weekly`/`stk_weekly_monthly` 接口不保证返回 adj_factor。改为从 `ods_daily` 按周聚合生成 `dwd_weekly_quote`。

```
聚合规则：
  周开盘价     = 该周第一个交易日的 open_qfq
  周最高价     = MAX(该周所有 high_qfq)
  周最低价     = MIN(该周所有 low_qfq)
  周收盘价     = 该周最后一个交易日的 close_qfq
  active_days = COUNT(该周 is_suspended=0 的交易日)
  周成交量     = SUM(该周所有 vol) / active_days × 5  （折算为 5 个交易日等效值）
  周成交额     = SUM(该周所有 amount) / active_days × 5
  周涨跌幅     = 汇总日线 pct_chg（对数收益率累加），不从复权价比率计算（含分红失真）
  PE/市值     = 取周最后交易日的 ods_daily_basic 值
```
> OHLC 不折算——停牌日填充的前值等于前一日真实价格，不会突破实际价格区间。vol/amount 折算为 5 日等效值，避免含停牌周的成交量被人为压低。active_days < 3 的周，DWS 量能相关指标标记为 NULL。

### 12.17 DDE 数据源更正：`moneyflow` 替代 `moneyflow_dc`

> 经 tushare 实际调试验证：
> - `moneyflow_dc`：数据始于 2023-09-11，仅 ~1.5 年覆盖，且不含 DDX/DDY/DDZ 字段
> - `moneyflow`：数据始于 2015-01-05，~10 年覆盖，含买入/卖出双向按大中小单拆分的完整字段

DDX 代理计算公式：
```
DDX = (buy_lg_vol + buy_elg_vol - sell_lg_vol - sell_elg_vol) / total_vol
```
DDY/DDZ 因 tushare 无法提供逐单数据，从方案中移除。

#### 12.17.1 B4 DDE trend 双轨与有效区间

- **DDX/DDX2**：`moneyflow` 量口径（2015 起，§12.17）
- **dde_trend 日线**：`moneyflow_dc` + `circ_mv` 优先，缺则回退 `net_mf_amount` + `total_mv`
- **dde_trend 周线**：仅 `moneyflow_dc` + `circ_mv`（日级 resample-W），**禁止** net_mf 回退；`moneyflow_dc` 自 **2023-09-11**
- **运维**：dc/circ 历史不足时须 `cli backfill-dde-meta --sync-dwd` + weekly DDE `--force` FULL
- **合法 N/A**：BSE 无 moneyflow、上市不足 ~60 周 EMA 预热、`_skip_dde` 不完整周

### 12.18 架构图表数更正

> ODS 层实际为 7 表（ods_stock_basic/daily/daily_basic/moneyflow/trade_cal/concept_detail + ods_etl_log），DIM 层实际为 4 表（dim_stock/dim_date/dim_concept/dim_concept_stock）。dim_index/dim_index_member 延后至 ADS 板块功能实现时引入（12.63）。架构图已同步更新。

### 12.19 ods_weekly 表移除

> 周线 DWD 改为从 `ods_daily` 聚合生成后，`ods_weekly` 不再被任何 ETL 步骤引用。已从 ODS DDL 中移除。

### 12.20 MACD 结构背离（Level 2）

> MACD 背离改用 `divergence_structure.compute_macd_structure_divergence()`，对齐通达信「MACD 顶底结构」**Level 2**（直接 + 隔峰线背 ∧ 柱背），**非** 60 bar rolling。顶区锚点 = 金叉 `CROSS(DIF,DEA)`，自锚点起滚动 `CH1/DIFH1/MACDH1` 与 REF 前段/隔峰段比较；DIF 幅度经 **MDIF** 整数归一后判线背。钝化需红柱连续且柱背（`MACDH1` 低于前段红柱峰）；**仅结构形成日 TG** 写入 DWS（钝化 T 不写）。同类 **10 bar** 去重。`RECALC_SPEC` lookback=**250**、event_tail=**10** 以覆盖多段金/死叉历史。Volume 量价背离仍走 `compute_price_signal_divergence` 60 窗（§6.5）。

### 12.21 前复权公式修正（Independent Review 发现）

> 原公式 `price_qfq = price × adj_factor` 实际计算的是**后复权**。tushare 的前复权需要除以最新复权因子。
> 修正公式：`price_qfq = price × adj_factor / latest_adj_factor`，其中 `latest_adj_factor` = 每只股票的最大 trade_date 对应的 adj_factor。
> 影响：MACD/MA/K线形态的形状不变（比例缩放），但零轴兜底公式中依赖 close 绝对值的计算需要前复权修正后才能正确。

### 12.22 DuckDB 写入路径修正（Independent Review 发现）— v1.5 已废弃

> 原方案使用 DuckDB ATTACH SQLite + PyArrow + sqlite3 executemany 三次拷贝写入。v1.5 切换为纯 DuckDB 后，DuckDB 直接 INSERT INTO 自身持久化表，无需 sqlite_scanner 扩展，此修正不再适用。

### 12.23 增量策略改为 INSERT-only 快照模式（Independent Review 发现）

> DELETE+INSERT 破坏性滑动窗口与 calc_date 回测快照存在架构冲突——DELETE 会销毁旧快照。
> 改为 INSERT-only：每日跑批只 INSERT 新 calc_date 行。每只股票最近 60 日 window 内的行每个交易日产出一个新快照，60 日前的行冻结。所有 DWS 表 PK 扩展为 `(ts_code, trade_date, calc_date)`。
>
> **快照保留策略（2026-06-05 修订）**：原"旧 calc_date 永不清除"导致 DWS 无界增长（实测 12 表 10.96M 行 / 3.1 GB）。改为**窗口保留**：提供 `prune_dws_snapshots(con, keep_runs=N)`（默认 `keep_runs=5`）与 CLI `prune --keep N` 子命令。清理只删除被更新 calc_date 覆盖（superseded）的旧快照行，**绝不删除任一 `(ts_code, trade_date)` 的 `MAX(calc_date)` 行**——因此 `v_*_latest` 视图结果逐行不变（即使指纹跳过导致某键最新值落在旧 calc_date 也安全保留）。`keep_runs=1` 完全坍缩为纯 latest，`>1` 保留最近 N 次运行的审计窗口。清理为逻辑删除 + `CHECKPOINT`（文件内空间复用），不挂进 `run_calc`（避免隐式删除），由运维显式执行。

### 12.24 dim_stock.sector 推导规则

> `dim_stock.sector` 由 `ts_code` 前缀推导：
> - `60xxxx` → 主板（沪市）
> - `00xxxx` → 主板（深市）
> - `30xxxx` → 创业板
> - `68xxxx` → 科创板
> - `83xxxx`/`87xxxx`/`43xxxx` → 北交所

### 12.24b dim_stock.stock_code 来源

> `dim_stock.stock_code` 直接取自 `ods_stock_basic.symbol`（tushare 已提供去交易所后缀的纯数字代码）。
> 示例：`ts_code='000001.SZ'` → `symbol='000001'`，无需额外字符串拆分。
> 用途：ADS 宽表中作为标准代码字段，方便 Excel 导出后与其他系统（通达信/同花顺/聚宽）的纯数字代码对齐。
> 建索引 `idx_dim_stock_code` 支持按标准代码查询。

### 12.25 DDX 除零守卫

> 停牌日或 tushare 返回零成交时，`total_vol=0` 导致 DDX 计算除零。
> 加守卫：`total_vol=0` 时 `DDX=NULL`，DDX2 递推跳过该日（沿用前值）。

### 12.26 DuckDB 并发与运维

> DuckDB 的并发模型：多个 reader 可同时查询，单个 writer 会短暂阻塞所有 reader。每日 ETL 增量写入约 12.5 万行，INSERT 耗时 ~2 秒，阻塞窗口可接受（日线数据只需每天收盘后跑一次）。如果将来并发查询成为瓶颈，ETL 写主库 + FastAPI 从只读副本查询（`ATTACH` 或文件复制）。

**扩展预留**：

> 当数据库超过 10 GB 时考虑按年度拆分（`tradeanalysis_2020.duckdb`、`tradeanalysis_2021.duckdb`...），DuckDB 原生支持 ATTACH 多数据库文件做跨年分析。

### 12.27 周线涨跌幅计算修正

> 前复权价周同比率受分红影响失真。改为汇总日线 `pct_chg`（对数收益率累加）计算周涨跌幅。

### 12.28 涨跌停/熔断过滤（Quant Review 发现）

> A 股涨跌停制度产生 K 线形态假信号（涨停时机械触发阳包阴，跌停时机械触发避雷针）。熔断日（2016-01-04、2016-01-07）成交量异常低，污染百分位计算。
> - K线形态判定前检查 `pct_chg`：若 `|pct_chg| ≥ 9.9%`（含四舍五入）则当日所有形态=NULL
> - 熔断日从量能百分位计算中硬排除
> - ST 股票（5% 涨跌停）的形态判定阈值相应收紧至 `|pct_chg| ≥ 4.9%`

### 12.29 MACD/DDE 背离标注日（结构形成 TG）

> **标注日 = 结构形成 TG**，非钝化 T、非价格/DIF（或 DDX）极值当日。实现：前 bar 满足钝化 T（线背直接∨隔峰 ∧ 柱背），当日所用 MDIF 转向（顶背离 `MDIFT<REF(MDIFT,1)`；底背离 `MDIFB>REF(MDIFB,1)`）且结构未「消失」（顶：`DIFH1` 未再创新高超越对照峰；底：`DIFL1` 未再创新低）→ 写入 `top_divergence`/`bottom_divergence`。钝化日仅内部状态，**不落 DWS**。语义：结构在 DIF/DDX 转向瞬间确认，消除前视偏差（极值 bar 当天信号通常同步极值，需待转向才可观测）。DDE 同构，锚点为 `DDX/DDX2` 交叉。

### 12.30 走平阈值调整（Quant Review 发现）

> 原始 0.5% 阈值过严——MACD 柱体日间正常波动在 1-5%，0.5% 导致"走平"与"拐头"频繁交替。所有"走平"判定阈值从 0.5% 放宽至 **2%**。
> 影响范围：MACD 上升走平/下降走平、DDX2 上升走平/下降走平。均线走平已改为基于斜率（v1.4），|ma_slope| < 0.3%/3日视为走平。

### 12.31 量能趋势斜率对数归一化（Quant Review 发现）

> 原始 0.5%/日斜率直接用于原始成交量，量级随股票市值差异极大。
> 修正：先对 `MA5_vol` 取自然对数，再计算线性回归斜率。阈值相应改为 **0.005/日**（对数空间 ≈ 原始空间的 0.5% 连续复利）。

### 12.32 周线 EMA 种子窗口明确化（Quant Review 发现）

> 日线 EMA12 种子 = 前 12 日 SMA，EMA26 种子 = 前 26 日 SMA。
> 周线同理：**EMA12 种子 = 前 12 周 SMA，EMA26 种子 = 前 26 周 SMA**，DEA = 前 9 周 SMA(DIF)，DDX2 = 前 5 周 SMA(DDX)。

### 12.33 ST/IPO/复牌股票过滤（Quant Review 发现）

> - **ST 股票**：从 `ods_stock_basic.name` 中检测 'ST' 或 '\*ST' 前缀，写入 `dim_stock.is_st` 字段
> - **IPO 冷启动**：上市不足 **30 个交易日**的股票，所有 DWS 指标=NULL（原规则仅覆盖量能）
> - **复牌首日**：通过 `dwd_daily_quote.is_suspended` 连续计数判定停牌 ≥ 5 个交易日后复牌的首日，所有信号（金叉/死叉/背离/形态/趋势）强制=NULL

### 12.34 dim_stock 增加 is_st 字段

```sql
ALTER TABLE dim_stock ADD COLUMN is_st INTEGER DEFAULT 0;  -- 1=ST/*ST股票
```

### 12.35 DWS Latest 视图（Trader Review 发现）

> INSERT-only 快照导致同一 `(ts_code, trade_date)` 存在多个 `calc_date` 行。直接 SELECT 返回重复数据。每张 DWS 表需要一个 `v_*_latest` 视图，自动过滤最新 calc_date。

```sql
-- 以 dws_macd_daily 为例，其余 9 张表同理
CREATE VIEW v_dws_macd_latest AS
SELECT *
FROM dws_macd_daily d
WHERE calc_date = (
    SELECT MAX(calc_date) FROM dws_macd_daily 
    WHERE ts_code = d.ts_code AND trade_date = d.trade_date
);
-- 后续 API / 盘前查询统一走视图，避免重复行
```

> ⚠️ **查询约束**：所有 API 查询、离线分析、Excel 导出**必须通过 `v_*_latest` 视图**访问 DWS 数据，**禁止直接查询 DWS 基表**。基表中同一 `(ts_code, trade_date)` 存在多个 `calc_date` 快照行，直接查询会返回重复数据。旧快照行不会被标记为「已被替代」——只有 `v_*_latest` 视图能保证每个 `(ts_code, trade_date)` 返回唯一的、最新的快照。

### 12.36 冻结数据声明（Trader Review 发现）

> 距今超过 **60 个交易日** 的 DWS 行一旦冻结，不再因 tushare 数据修正（如财务报表重述导致 adj_factor 回溯调整）而重新计算。历史图表可能与实时行情软件（东方财富/同花顺）存在微小偏差——指标形状不变（前复权比例缩放），但零轴附近数值可能偏差 < 0.01%。如需修正，触发对应股票的全量历史重算。

### 12.37 ODS / DWD 索引补充（Data Architect Review 发现）

> DWD 层是 DWS 计算的数据源，缺失索引会导致 DuckDB 每次增量计算扫描全表定位数据。ODS 层增量拉取去重检查同样需要日期索引。新增 5 条索引：`dwd_daily_quote` / `dwd_daily_moneyflow` / `dwd_weekly_quote` 各一条复合索引 + `ods_daily` / `ods_daily_basic` / `ods_moneyflow` 各一条日期索引。

### 12.38 停牌日 EMA 窗口策略（Data Architect Review 发现）

> 原方案仅定义停牌日 DWD 行为（填充前值），未定义 EMA 递推是否计入停牌日。明确策略：**跳过停牌日，不消耗 EMA 窗口**。EMA 递推仅计入 `is_suspended=0` 的有效交易日，LAG 引用前一个有效交易日。DWD 新增 `is_suspended` 字段显式标记停牌日（替代 `vol=0` 启发式判断），复牌首日判定基于 `is_suspended` 连续计数。

### 12.39 数据到达延迟处理（Data Architect Review 发现）

> `daily_basic`（PE/市值）通常比 `daily` 晚到。新增跑批前完整性守卫：检查所有依赖表的最新 trade_date 一致后才跑批。不产出残缺 DWD 行。`ods_etl_log` 增加 `data_completeness` JSON 字段记录各数据源状态。

### 12.40 全量初始化分批策略（Data Architect Review 发现）

> 全量初始化 PARTITION BY ts_code 一次性查询可能导致 2-5 GB 内存占用。改为每批 100 只股票分批处理，每批在 DuckDB 中完成窗口函数计算后直接 INSERT INTO 写入，释放内存后处理下一批。

### 12.41 指数宽表参数化建议（Data Architect Review 发现）

> `v_ads_index_wide` 硬编码 `WHERE c.ts_code = '000001.SH'`。后续扩展多指数时建议从 `dim_index` 表动态读取，避免为每个指数手写 VIEW。

### 12.42 stock_code 标准代码字段（方案优化）

> tushare 的 `ts_code` 含交易所后缀（如 `000001.SZ`），不便与其他系统对齐。`dim_stock` 新增 `stock_code` 字段，直接取自 `ods_stock_basic.symbol`（tushare 已提供去后缀的纯数字代码，无需拆分）。ADS 宽表和指数宽表同步新增此字段。Excel 导出列顺序中将 `stock_code` 紧随 `ts_code` 之后。

### 12.43 ADS 层 K 线形态合并（方案优化）

> DWS 层保留 7 个独立 0/1 布尔列（同日可**多列并存**）。ADS 层合并为单一 `kpattern` 枚举（取值 `'yang_ke_yin'` / `'yang_bao_yin'` / `'contrarian_yin_ke_yang'` / `'contrarian_yin_bao_yang'` / ... / NULL）。CASE 优先级：买入子形态 `阳克阴 > 阳包阴`；阴包阳/阴克阳经回测重标为 `contrarian_*`（展示为反向买入，Excel 绿色高亮）；卖出 `墓碑线 > 避雷针 > 高开长阴`。`strength` 仍取自 DWS，与 `kpattern` 展示优先级独立（见 §6.1）。Excel 导出同步单列枚举高亮。

### 12.44 K 线形态量化参数（交易专家 Review）

> 原方案 7 种 K 线形态中，墓碑线/避雷针/高开长阴/阳克阴/阴克阳 存在模糊描述（"长上影线极长""小实体""显著上涨""高位"），无法直接编码。补全精确参数：墓碑线上影≥3x、Doji<0.5%或<振幅10%、高位=60日高点90%内或20日涨>15%；避雷针实体<振幅20%+实体在下1/3；高开长阴前涨>15%(10日)+量>MA5×1.5；阳克阴/阴克阴检测量能基准为 **MA5_vol×1.2**（非前日量），并叠加 MA10 趋势过滤。所有阈值集中 `kpattern_params.py`，标注 `[待回测调优]`。

### 12.45 周线涨跌幅公式统一（Data Architect Review 发现）

> 12.16 节旧公式使用复权价比率（含分红失真），与 5.2/12.27 节矛盾。统一为：汇总日线 pct_chg（对数收益率累加）。

### 12.46 指数宽表 SQL 代码围栏修复

> `v_ads_index_wide` 和 `v_ads_index_wide_weekly` 的 SQL 代码块缺少 markdown ` ```sql ` 围栏，已补全。

### 12.47 ETL Step 2 dim_stock 转换明细（Data Architect Review 发现）

> Step 2 原仅写 `ods_stock_basic → dim_stock`，未列出各字段的派生规则。补充 stock_code/sector/is_st/exchange 四条转换明细，实现时逐项核对。

### 12.48 dim_index 数据源补充（交易专家 Upgrade + Data Architect Review）

> 原方案 `dim_index` + `dim_index_member` 有 DDL 无数据源。补充 tushare `index_basic`（指数基本信息）和 `index_weight`（成分权重，月度）接口，纳入 1.2 数据源表、8. ETL 链路和 11. 接口依赖。注意 6000 积分级别 `index_weight` 可能受限，备选 `index_member` 接口。

### 12.49 DWD 停牌日显式字段（Data Architect Review 发现）

> `dwd_daily_quote` 新增 `is_suspended INTEGER DEFAULT 0` 字段，显式标记停牌日。停牌判定由 ODS→DWD ETL 对比 `dim_date` 交易日历实现（某股票在交易日无行情 = 停牌），替代 `vol=0` 启发式。EMA 递推/复牌首日过滤均基于该字段。

### 12.50 数据量预估修正（Data Architect Review 发现）

> DWS 日线 5 表原估算 6000 万行，未计入 INSERT-only 快照膨胀。修正为 ~1.05 亿行（基础 12.5M + 快照膨胀 ~8.5M → 21M/表 × 5），总存储 ~7.0 GB。仍在 DuckDB 可承受范围内。

### 12.51 stock_code 来源优化（Data Architect Review 发现）

> `dim_stock.stock_code` 原方案通过 `ts_code.split('.')[0]` 字符串拆分派生。tushare `stock_basic` 接口返回的 `symbol` 字段已是去后缀的纯数字代码，直接用 `ods_stock_basic.symbol` 更简洁，消除不必要的字符串操作。更新 4.1 DDL 注释、12.24b 节、ETL Step 2 三处。

### 12.52 DWS CHECK 约束补全（Data Architect Review 未优化项）

> 补全 12.10 节：dws_kpattern 布尔列 CHECK (0/1)、dws_volume pct_vol_rank 值域 CHECK [0,100] + zone/trend 枚举 CHECK、dws_ma 布尔列+枚举 CHECK、dws_dde 枚举 CHECK。v1.5 切换纯 DuckDB 后 CHECK 由数据库强制执行。

### 12.53 dws_ma.alignment NULL 语义明确

> alignment 取 NULL 的唯一场景：MA5、MA10 或斜率不可用（<11 bar、NaN）。`sideways` 为独立枚举（双斜率 |s|<0.08%/日 且非 tangle）。「一走平一趋势」过渡态由 Layer 3 **八格查表** fallback 归入 8 方向类（v2 映射见 §6.3 表），不再 NULL。

### 12.54 dim_date 补 is_year_end（Data Architect Review 未优化项）

> dim_date 新增 `is_year_end INTEGER` 字段，与已有的 `is_week_end`/`is_month_end` 对齐，用于年末统计（年线收盘价、年度涨幅等）。

### 12.55 total_vol 口径说明（Data Architect Review 未优化项）

> dwd_daily_moneyflow.total_vol 选用买入侧求和（buy_sm + buy_md + buy_lg + buy_elg），与 DDX 分子的大单+超大单净买入方向一致。注明了 tushare 买卖分类基于主动方向，两侧合计理论上等价但实际可能有微小偏差。

### 12.56 周划分改用 date_trunc('week')，修正跨年周切分（Data Architect Review 发现）

> 原 `dim_date.is_week_end`（build_dim）与 `dwd_weekly_quote` 滚动周线分区（build_dwd）均用 `strftime(dt, '%Y-%W')` 作为周键。`%Y` 为日历年、`%W` 年初不满一周记为第 `00` 周，导致**跨年自然周被切成两段**（如 2025-12-29 周一~2026-01-02 周五被分为 `2025-52` 与 `2026-00`）。后果：跨年周周线 bar 的 open/high/low/pct_chg/active_days 错误，且该周出现两个 `is_week_end=1`（上一年尾 + 新年头），下游 6 个周线指标在年初多算一根假周末 bar。每年元旦附近发生一次。
> **修复**：两处统一改为 `date_trunc('week', dt)`（周一锚点，跨年自然周天然归入同一分区）。`week_of_year` 展示字段无下游计算依赖，保留 `%W`。
> **实库副作用**：需重建 `dim_date` + `dwd_weekly_quote` 并重算周线 DWS，历史跨年周数据才会纠正。

### 12.56 ods_etl_log DDL 一致性修正（Data Architect Review 未优化项）

> 将 `data_completeness` 字段直接纳入 12.2 节 CREATE TABLE 主 DDL，消除与 12.8c ALTER TABLE 的冗余。

### 12.57 adj_factor 接口表述统一（Data Architect Review 未优化项）

> 1.2 节和 11 节统一表述：`adj_factor` 由 `daily` 接口返回，内含于日线数据中，无需独立 API 拉取。

### 12.58 K 线形态 strength 强度评分（交易专家 Review）

> dws_kpattern 新增 `strength REAL` 列（0.0~1.0）。7 种形态各定义加权公式；同日多形态时 `calc_kpattern._compute_strength()` 按固定 `if/elif` 顺序取第一个触发形态（阳包阴优先于阳克阴，与 ADS `kpattern` 子形态优先展示顺序不同）。阳克阴/阴克阴量能维度与检测对齐，使用 `当日量/MA5_vol/1.5`。ADS 宽表映射为 `kpattern_strength`。

### 12.59 均线指标重构（交易专家 Review）

> 删除 `ma5_flat`/`ma10_flat`/`ma_all_flat` 三个布尔列。新增 `bias_ma5`/`bias_ma10`（乖离率）、`ma5_slope`/`ma10_slope`（3 日斜率）。`alignment` 从 3 值扩展为 9 值枚举（bull_strong/bull_building/bull_weakening/bull_rolling/bear_strong/bear_building/bear_weakening/bear_rolling/tangle），基于位置+双斜率方向组合。DWS 层存英文码，ADS 层 CASE WHEN 翻译为中文展示文本。

### 12.60 DDE 结构背离信号（交易专家 Review → Level 2）

> dws_dde `divergence TEXT` 列（top_divergence/bottom_divergence/NULL）。调用 `compute_dde_structure_divergence()`（`divergence_structure.py`），与 MACD 同构：**锚点** = `CROSS(DDX,DDX2)`（顶）/ `CROSS(DDX2,DDX)`（底）；`fast=DDX`、`slow=DDX2`，柱背段内取 DDX 峰（顶区 `ddx>0`）。顶区 DDX 峰 **尖刺过滤**（邻域 `[peak-2,peak+3)` 内 ≥0.8×峰值 bar <2 → 剔除）；窗内 DDX 含 NaN → 该 bar 不判（`require_finite=True`）。标注日 = 结构形成 **TG**（§12.29）；dedup=**10**。`RECALC_SPEC` lookback=**250**。ADS 宽表映射为 `dde_divergence`。注：DDX 为 tushare moneyflow 代理，与东方财富终端数值可能有偏差。

### 12.61 v1.4.1 文档勘误（Data Architect Review 发现）

> 11 项文档问题修正：
> - 🔴 ADS 列数 50→46（手工枚举验证），7.1 节两处 + 指数宽表一处同步修正
> - 🔴 SQL 语法：`m.trade_date`→`c.trade_date`（日线）/ `cw.trade_date`（周线），周线 `q.*`→`qw.*`
> - 🔴 12.30 走平判定更新：移除对已删除的 ma5_flat/ma10_flat/ma_all_flat 引用
> - 🟡 ODS 表数 8→7，DIM 表数 7→6（架构图 + 12.18/12.19 同步修正）
> - 🟡 dim_stock.symbol 注释补充保留理由（tushare 原始数据溯源）
> - 🟡 dws_kpattern.strength 补充 CHECK (NULL OR [0.0,1.0])
> - 🟠 12.8c 标注为已纳入 12.2 主 DDL
> - 🟠 12.33 复牌首日判定引用 is_suspended 字段
> - 🟠 9.3 速查表 alignment 补充中文对应说明

### 12.62 周线停牌折算（CEO Review 发现）

> `dwd_weekly_quote` 新增 `active_days INTEGER`（该周实际交易天数）。vol/amount 改为折算公式 `SUM(vol)/active_days×5`，避免含停牌周成交量被人为压低。OHLC 不折算。12.16 聚合规则同步更新。

### 12.63 指数成分维表延后（CEO Review 发现）

> `dim_index` + `dim_index_member` 当前无下游消费者（ADS 板块表尚未实现），暂时移除。DDL 保留在 12.2 节（已注释），待 `ads_sector_breadth` 实现时引入。DIM 层从 6 表降为 4 表。1.2/2/8/11/10 节同步更新。

### 12.64 ETL 故障恢复与数据回补（CEO Review 发现）

> 新增 12.7b 节：ETL 重启时自动检测 ODS 断层，按断层天数（0-1/2-60/>60）分别走正常增量、窗口重算、全量重算三条恢复路径。数据恢复幂等，复用现有 UPSERT 逻辑。`ods_etl_log` 记录 recovery 状态。

### 12.65 DWD UPSERT + DuckDB 自检 + 分批优化 + WAL运维 + 单位标注（Eng Review）

> - T1: ETL Step 3 明确 DWD 写入使用 UPSERT（与 ODS 一致），确保 tushare 数据修正时 DWD 可覆盖
> - T2: 12.7 增量计算流程增加 Step 0 DuckDB ATTACH 连通性自检 + Step 3 明确每批 100 股一个事务
> - T3: 12.13 全量初始化改为分批查询+分批窗口函数+分批写入，避免 2-5 GB 内存占用
> - T4: 12.26 增加 WAL checkpoint 运维建议（每日 ETL 后 TRUNCATE）+ 10 GB 分库扩展预留
> - T5: 1.2 数据源表增加 vol/amount 单位标注（手/千元/万元）

### 12.66 存储引擎切换：SQLite → 纯 DuckDB（架构决策）

> v1.5 重大变更：从 SQLite（持久化）+ DuckDB（计算引擎）双引擎切换为纯 DuckDB。原因：新闻分析模块同样从零开始，不存在 SQLite 历史约束。纯 DuckDB 消除 DuckDB ATTACH → PyArrow → sqlite3 executemany 的三次数据拷贝，DWS 计算结果直接 INSERT INTO DuckDB 表。影响范围：1.3 技术架构、7.2 Excel 导出、8. ETL 链路、10. 数据量预估、12.7 增量流程、12.9 计算引擎、12.13 全量初始化、12.22/12.26（废弃/改写）、12.40 分批策略。DDL 和指标计算逻辑不变。

### 12.67 ADS 视图 INNER JOIN → LEFT JOIN（Independent Review Round 2 发现）

> `v_ads_analysis_wide_daily` / `v_ads_analysis_wide_weekly` / `v_ads_index_wide` / `v_ads_index_wide_weekly` 四个视图全部使用 INNER JOIN。若任意 DWS 表对某股票的某交易日缺少行（moneyflow 2015 年前数据缺失、IPO 窗口期、某表计算跳过），整只股票的该行从视图中静默消失。
> 修正：全部 20 处 JOIN 改为 LEFT JOIN。缺失 DWS 列填 NULL，与 DWS 层已有的 NULL 语义（IPO 过滤、种子期、停牌）保持一致。

### 12.68 全量初始加载 — API 限流与耗时估算（Independent Review Round 2 发现）

> 原方案未量化全量加载的 API 调用次数和耗时。新增 11.1 节：~7,500 次 API 调用，单线程预计 2-4 小时，3 线程 1-2 小时。补充分批断点续传策略（利用 `ods_etl_log` 记录进度，中断后从断点继续）。建议默认单线程（保守），提供 `--workers N` 参数。

### 12.69 DuckDB 最低版本声明（Independent Review Round 2 发现）

> 原方案未声明最低 DuckDB 版本。1.3 节新增 `DuckDB >= 1.0` 要求（依赖 ON CONFLICT UPSERT、CHECK 约束强制执行、ATTACH 多文件支持）。

### 12.70 周线 DWS DDL 模板生成（Independent Review Round 2 发现）

> 周线 5 张 DWS 表无独立 DDL。12.11 节补充实现方式：日线/周线 DDL、索引、CHECK、`v_*_latest` 视图均从同一模板渲染生成，避免手写两套 DDL 产生不一致。

### 12.71 DWS 基表查询约束（Independent Review Round 2 发现）

> INSERT-only 快照模式下同一 `(ts_code, trade_date)` 存在多个 `calc_date` 行。12.35 节补充查询约束：所有 API/离线分析/导出**必须走 `v_*_latest` 视图**，禁止直接查 DWS 基表。

### 12.72 文档修正 — 杂项（Independent Review Round 2 发现）

> - 8. ETL Step 2 exchange 映射补充完整映射表 `{'SSE': '上海', 'SZSE': '深圳', 'BSE': '北京'}`
> - 9.2 MACD 背离速查表补充 60 日回溯窗口参数和确认日标注
> - 12.14 EMA 种子补充 MACD 全字段可用时间线（第 36 个交易日起，对齐 IPO 30 日过滤）
> - 7.1 `v_ads_index_wide` 和 `v_ads_index_wide_weekly` 修正 `m.trade_date` / `m.ts_code` → `c.trade_date` / `c.ts_code`（别名未定义）

### 12.73 开发环境搭建（DX Review 发现）

> 原方案未集中声明开发环境依赖。新增 1.4 节：Python ≥3.10、DuckDB ≥1.0、tushare Pro 等依赖清单，环境变量表格（TUSHARE_TOKEN / DUCKDB_PATH / LOG_LEVEL / ETL_WORKERS），`requirements.txt` 内容，`.env` 文件约定。

### 12.74 快速开始指南（DX Review 发现）

> 新开发者的 TTHW（Time to Hello World）从无定义到 ~5 分钟。新增 1.5 节：5 步操作序列（克隆→配置→连通性检查→单股拉取→第一条 MACD 查询），每步标预计耗时，附常见错误提示。

### 12.75 ETL CLI 接口设计（DX Review 发现）

> 原方案描述了 ETL 内部流程但未定义外部触发接口。新增 8.1 节：统一 `backend/cli.py` 入口，5 个子命令（check / etl / query / export / status），每个子命令完整的 `--help` 输出、参数说明、使用示例。

### 12.76 ETL 错误分级策略（DX Review 发现）

> 原方案 `ods_etl_log` 仅记录 status='failed'，未定义错误严重级别和对应的处理动作。新增 8.2 节：FATAL/ERROR/WARN/INFO 四级分类，FATAL 条件清单（DuckDB 不可读写 / 磁盘不足 / 版本不满足），ERROR 恢复策略（单股跳过+自动重试），WARN 阈值示例（复权因子跳变 / vol=0 非停牌）。

### 12.77 FastAPI 端点设计（DX Review 发现）

> 原方案仅在 1.3 节提了一句"FastAPI Python client 直连"，未设计 API 端点。新增 11.2 节：5 个端点（单股指标 / 历史序列 / 多条件选股 / 全市场概况 / 健康检查），完整的请求/响应 JSON schema，错误响应格式（code + message + cause + fix + doc_url），错误码约定表（400/404/503/500），`freshness` 标记贯穿所有响应。

### 12.78 DuckDB AUTOINCREMENT 语法修正（/autoplan Final Review 发现）

> 12.2 节 `ods_etl_log` DDL 使用 `INTEGER PRIMARY KEY AUTOINCREMENT`——`AUTOINCREMENT` 是 SQLite 语法，DuckDB 不识别。v1.5 从 SQLite 切换到纯 DuckDB 时遗漏修正。改为 `INTEGER PRIMARY KEY`（DuckDB 默认自增）。

### 12.79 文档遗留引用修正（/autoplan Final Review 发现）

> 12.50 节末句"仍在 SQLite 可承受范围内"→ 改为"仍在 DuckDB 可承受范围内"。12.61 节 `m.trade_date` 修正记录补充说明：周线 `v_ads_index_wide_weekly` 同步修正。

### 12.80 CLI 标志命名统一（/autoplan Final Review 发现）

> 8.1 节 export 子命令 `--no-st` 标志存在歧义（可解读为"排除 ST"或"不过滤 ST"）。改为默认排除 ST 股票，通过 `--include-st` 选项显式包含，与 Python API `filter_st=True` 默认行为一致。

### 12.81 ETL 错误分级补充 DEGRADED 级别（/autoplan Final Review 发现）

> 8.2 节原 FATAL/ERROR/WARN/INFO 四级分级缺少对"数据降级"（12.7b `status='degraded'`、11.2 `freshness.status='stale'`）的映射。新增 `DEGRADED` 级别：数据可用但新鲜度下降，写 `ods_etl_log.status='degraded'`，API 返回 `freshness.status='stale'`，ETL 继续运行。

### 12.82 K 线形态 spec 与实现对齐（v1.9）

> §6.1 / §9.1 按 `calc_kpattern.py` + `kpattern_params.py` 重对齐：
> - 新增「通用前提」：阴阳判定、min 30 行、涨跌停过滤、周线周末 bar、DWS 多列并存语义
> - 阳克阴/阴克阴：检测量能基准改为 **MA5_vol×1.2** + MA10 趋势过滤（修正旧版「前日量×1.2」表述）
> - 阳包阴：注明 `ma10_filter`/`vol_filter` 已在 params 预留、检测尚未接入
> - `strength`：阳克阴/阴克阴量能维度改为 MA5；明确 `if/elif` 固定顺序与 ADS 展示优先级分离
> - ADS `kpattern` CASE：阴包阳/阴克阳映射为 `contrarian_*`（与 `schema.py` / `export_wide.py` 一致）

### 12.83 MACD/DDE 背离 spec 与实现对齐（v1.10）

> §6.2 / §9.2 / §12.20 / §12.29 / §12.60 按 `calc_macd.py` + `base.compute_price_signal_divergence()` 重对齐：
> - 新增 §6.2「背离」专节：98%/102% 近高/近低位容忍、DIF 回升 >10%、价低 ≥3 bar、5 bar 去重、确认日 `argmax/argmin < window-1`
> - §6.2 同步修正：trend 5-bar 加权回归(decay=0.15, ±0.001)、near 交叉判定、alert 连涨/跌 2 日、`trend_strength` 列
> - DDE 背离：信号列 **DDX**（非 DDX2）+ 尖刺过滤 + 有限窗要求

### 12.84 MA/DDE/Volume/PricePosition spec 与实现对齐（v1.11）

> §6.3–§6.6 / §9.3–§9.6 按各 Calculator 重对齐：
> - **MA**：斜率改为 5-bar OLS %/日；alignment 增 `sideways`、阈值 ±0.08%/日、tangle 2.99%；near 改为 0.5% 小间距或 3 日收敛预估
> - **DDE**：trend 8-bar 加权(decay=0.20)；增 `trend_strength`；alert 基于 DDX2 连涨/跌 2 日
> - **Volume**：DDL 增 `volume_ratio`/`trend_strength`/`divergence`；zone 基于 `pct_vol_rank` 迟滞；trend 为 ln(vol) 10-bar 加权 ±0.008（非 zone 内子集）
> - **Price Position**：新增 §6.6 / §9.6（原 spec 缺失）；DWS 6 类×2 频 = 12 表
> - §6.2 背离：补充 MACD/DDE/Volume 三模块调用差异表

### 12.85 MACD/DDE 结构背离 Level 2（v1.12）

> §6.2 / §9.2 / §9.4 / §12.20 / §12.29 / §12.60 按 `divergence_structure.py` + `calc_macd.py` / `calc_dde.py` 重对齐：
> - MACD/DDE 背离由 60 bar rolling `compute_price_signal_divergence` 改为通达信 **Level 2 顶底结构**（金叉/死叉锚点、直接+隔峰 MDIF 线背、柱背、TG 标注、dedup=10）
> - 新增 `backend/etl/divergence_structure.py`：`compute_macd_structure_divergence` / `compute_dde_structure_divergence`
> - MACD/DDE `RECALC_SPEC` lookback 60→**250**、event_tail→**10**
> - Volume 量价背离仍用 `base.compute_price_signal_divergence`（window=60, dedup=5）

---

## GSTACK REVIEW REPORT

| Review | Trigger | Runs | Status | Findings |
|--------|---------|------|--------|----------|
| CEO Review | `/plan-ceo-review` | 1 | CLEAR | 11 决定, 0 critical gaps |
| Eng Review | `/plan-eng-review` | 1 | CLEAR | 6 issues → 6 resolved |
| DataEng Review | 专家审查 | 1 | CLEAR | 5 issues → 5 resolved |
| Architect Review | 系统架构师 | 1 | CLEAR | 2 issues → 2 resolved |
| Independent Review | Claude subagent | 1 | CLEAR | 16 findings → 7 critical resolved |
| Quant Review | Claude subagent | 1 | CLEAR | 15 findings → 7 critical resolved |
| Trader Review | Claude subagent | 1 | CLEAR | 7 findings → 3 critical resolved (横截面索引/latest视图/冻结声明) |
| Data Architect Review (Round 1) | Claude subagent | 1 | CLEAR | 9 findings → 9 resolved (ODS/DWD索引/停牌窗口/数据延迟/分批策略/主键/指数参数化 + stock_code/K线合并) |
| Data Architect Review (Round 2) | Claude subagent | 1 | CLEAR | 5 findings → 5 resolved (周线公式/代码围栏/ETL明细/dim_index数据源/停牌字段/数据量) |
| **Trading Expert Review (Round 1)** | **Claude subagent** | **1** | **CLEAR** | **1 finding → 1 resolved (K线形态量化参数)** |
| **Trading Expert Review (Round 2)** | **Claude subagent** | **1** | **CLEAR** | **3 findings → 3 resolved (strength强度/均线重构/DDE背离)** |
| **Data Architect Review (Round 3)** | **Claude subagent** | **1** | **CLEAR** | **11 findings → 11 resolved (文档勘误: 列数/SQL别名/表数/走平引用/symbol注释/CHECK/12.8c/12.33/9.3)** |
| **CEO Review (Round 2)** | `/gstack-plan-ceo-review` | **1** | **CLEAR** | **3 findings → 3 resolved (周线停牌折算/ETL故障恢复/指数成分延后)** |
| **Eng Review** | `/gstack-plan-eng-review` | **1** | **CLEAR** | **5 issues → 5 resolved (DWD UPSERT/DuckDB自检/分批优化/WAL运维/单位标注)** |
| **Independent Review (Round 2)** | Claude (autoplan) | **1** | **CLEAR** | **6 findings → 6 resolved (INNER JOIN→LEFT JOIN/全量限流/DuckDB版本/周线DDL/基表查询约束/文档修正)** |
| **DX Review** | `/plan-devex-review` | **1** | **CLEAR** | **5 findings → 5 resolved (开发环境/快速开始/ETL CLI/错误分级/API端点)** |
| **/autoplan Final Review** | `/gstack-autoplan` | **1** | **CLEAR** | **4 findings → 4 resolved (AUTOINCREMENT语法/遗留引用/CLI命名/错误分级DEGRADED)** |
| **v1.5 架构切换** | **架构决策** | **—** | **CLEAR** | **SQLite → 纯 DuckDB（新闻模块未实施，历史约束不存在）** |

UNRESOLVED: 0

VERDICT: 17 轮审查 CLEARED — 95 项决定全部确认，0 critical gaps。纯 DuckDB 架构，可以进入实现阶段。
