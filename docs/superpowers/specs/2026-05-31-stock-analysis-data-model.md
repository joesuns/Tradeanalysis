# 个股技术分析数据模型设计

> 日期: 2026-06-01 | 状态: 已确认 | 版本: v1.8

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
       │         dws_kpattern  dws_macd  dws_ma    dws_dde   dws_volume
       │         (K线形态)     (MACD)    (均线)     (DDE)     (量能)
       │           │            │          │          │          │
       │           │            │   各含日线/周线两个粒度           │
       └───────────┴────────────┴──────────┴──────────┴──────────┘
                                       │
                                       ▼  (后续扩展)
                              ADS 层 — 综合评分 / 信号共振 / 选股 / 板块宽度
```

---

## 3. ODS 层 — 原始贴源层

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

---

## 6. DWS 层 — 技术指标汇总层

> 按指标类型拆分为 5 个子表（日线 + 周线各一张，共 10 表），所有计算使用**前复权价格**。

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

#### 计算口径

> 以下参数为初始默认值，标注 `[待回测调优]` 的阈值应在实现后通过历史回测验证并调整。

| 形态 | 类型 | 判定逻辑 | 关键参数 |
|------|------|---------|---------|
| **阳包阴** | 买入 | 前日阴线 + 当日阳线；当日开 ≤ 前日收 **且** 当日收 ≥ 前日开（实体吞没，不看影线） | — |
| **阳克阴** | 买入 | 量学"量价双向胜阴"：①当日量 > 前日量 × 1.2 **且** ②当日实顶[max(开,收)] > 前日实顶。单向胜 = 不触发 | 量能放大 ≥ 1.2x `[待回测]` |
| **墓碑线** | 卖出 | ① 上升趋势高位：close 处于近 60 日最高价 10% 以内，或近 20 日累计涨幅 > 15% `[待回测]`；② 无实体 Doji：\|O-C\|/前收 < 0.5%，或实体 < 全日振幅 10%；③ 长上影：(H - max(O,C)) / 实体 ≥ 3x（实体近零时改用 (H-max(O,C)) / 全日振幅 > 60%） | 高位: 60日高点10%内 或 20日涨>15% `[待回测]`；Doji: <0.5%或<振幅10%；上影≥3x实体 |
| **避雷针** | 卖出 | ① 上升趋势高位（同上）；② 小实体位于当日价格区间底部：实体 < 全日振幅 20%，且实体中心处于全日区间下 1/3 `[待回测]`；③ 上影线 ≥ 实体 3 倍 | 高位: 同上；实体<振幅20%；上影≥3x实体 |
| **高开长阴** | 卖出 | ① 显著上涨后高位出现：近 10 日累计涨幅 > 15% `[待回测]`；② 跳空高开：当日开 > 前日收；③ 长阴实体 ≥ 5%（\|C-O\|/O ≥ 5%）；④ 成交量放大：vol > MA5_vol × 1.5 | 前序涨幅>15%(10日)；阴体≥5%；量>MA5×1.5 |
| **阴包阳** | 卖出 | 前日阳线 + 当日阴线；当日开 ≥ 前日收 **且** 当日收 ≤ 前日开（实体吞没） | — |
| **阴克阳** | 卖出 | 量学"量价双向胜阳"：①当日量 > 前日量 × 1.2 **且** ②当日实底[min(开,收)] < 前日实底。单向胜 = 不触发 | 量能放大 ≥ 1.2x `[待回测]` |

#### 强度评分 (strength)

> `strength` 字段输出 0.0~1.0 的连续值。触发哪个形态就用该形态公式计算；多重触发（阳包阴↔阳克阴、阴包阳↔阴克阳）取子形态公式——子形态条件更严，强度值天然高于父形态。无形态触发时为 NULL。

**买入形态强度公式**：

| 形态 | 维度 | 公式 | 权重 |
|------|------|------|:--:|
| **阳包阴** | 吞没幅度 | `min(1.0, 当日实体 / 前日实体 / 2)` | 0.5 |
| | 量能配合 | `min(1.0, 当日量 / MA5_vol / 1.5)` | 0.3 |
| | 收盘位置 | `(收-低) / (高-低)` — 光头上线趋近 1.0 | 0.2 |
| **阳克阴** | 实顶超越 | `min(1.0, (当日实顶-前日实顶) / 前日实顶 / 0.02)` | 0.4 |
| | 量能超越 | `min(1.0, (当日量/前日量 - 1.2) / 0.8)` | 0.4 |
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
| | 量能超越 | `min(1.0, (当日量/前日量 - 1.2) / 0.8)` | 0.4 |
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
    calc_date      TEXT,               -- 计算日期
    PRIMARY KEY (ts_code, trade_date, calc_date)
);
```

#### 计算口径

| 字段 | 计算方法 | 参数 |
|------|---------|------|
| **EMA 初始值** | 前 N 日 SMA 做种子 | — |
| **基础公式** | EMA(Close,12), EMA(Close,26), DIF = EMA12-EMA26, DEA = EMA(DIF,9), MACD柱 = 2×(DIF-DEA) | α=2/(N+1) |
| **顶背离** | 价格创 60 日新高 + DIF 峰顶**未**创 60 日新高 | 60 日回溯窗口 |
| **底背离** | 价格创 60 日新低 + DIF 谷底**未**创 60 日新低 | 60 日回溯窗口 |
| **多方区域** | MACD柱 > 0 | 纯柱体符号 |
| **空方区域** | MACD柱 < 0 | 纯柱体符号 |
| **金叉** | DIF 上穿 DEA（MACD柱从负翻正日） | — |
| **死叉** | DIF 下穿 DEA（MACD柱从正翻负日） | — |
| **即将金叉** | DIF < DEA **且** 差值收窄 **且** \|DIF-DEA\|/\|DEA\| < 15% | 三条件同时满足 |
| **即将死叉** | DIF > DEA **且** 差值收窄 **且** \|DIF-DEA\|/\|DEA\| < 15% | 三条件同时满足 |
| **上升拐头** | 此前连续上升（柱体连续3日向上）→ 今日柱体 < 昨日 | 趋势终结首日 |
| **下降拐头** | 此前连续下降（柱体连续3日向下）→ 今日柱体 > 昨日 | 趋势终结首日 |
| **上升走平** | 此前连续上升 → 今日\|柱体-昨日\|/昨日\| ≤ 2% | 动量消失 |
| **下降走平** | 此前连续下降 → 今日\|柱体-昨日\|/昨日\| ≤ 2% | 动量消失 |
| **趋势（上升）** | MACD柱 连续 3 日递增 | N=3 |
| **趋势（下降）** | MACD柱 连续 3 日递减 | N=3 |
| **趋势（走平）** | 不满足上升或下降条件 | — |

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
    -- 斜率（3日间隔）
    ma5_slope      REAL,               -- (MA5_t - MA5_{t-3}) / MA5_{t-3} × 100
    ma10_slope     REAL,               -- (MA10_t - MA10_{t-3}) / MA10_{t-3} × 100
    -- 趋势（位置 + 双斜率方向组合，DWS 层存英文码）
    alignment      TEXT,               -- 'bull_strong' / 'bull_building' / ... / 'tangle' / NULL
    -- 转折点
    turning_point  TEXT,               -- 'golden_cross' / 'dead_cross' /
                                       -- 'near_golden' / 'near_dead' / NULL
    calc_date      TEXT,
    PRIMARY KEY (ts_code, trade_date, calc_date)
);
```

#### 计算口径

**基础值**：

| 字段 | 计算方法 | 参数 |
|------|---------|------|
| **MA5** | SMA(Close, 5) | 前复权收盘价 |
| **MA10** | SMA(Close, 10) | 前复权收盘价 |
| **bias_ma5** | `(close_qfq - ma_5) / ma_5 × 100` | — |
| **bias_ma10** | `(close_qfq - ma_10) / ma_10 × 100` | — |
| **ma5_slope** | `(ma_5_t - ma_5_{t-3}) / ma_5_{t-3} × 100` | 3 日间隔 `[待回测]` |
| **ma10_slope** | `(ma_10_t - ma_10_{t-3}) / ma_10_{t-3} × 100` | 同上 |

**alignment 9 值枚举**（基于 MA5 vs MA10 位置 + 双斜率方向，斜率正负分界 ±0.3%/3 日 `[待回测]`）：

| DWS 存储 | MA5/MA10 | ma5_slope | ma10_slope | 交易含义 |
|----------|:--------:|:---------:|:----------:|------|
| `bull_strong` | > | > 0 | > 0 | 两线同步上行，持仓舒适区 |
| `bull_building` | > | > 0 | < 0 | MA5 已拐头向上，MA10 惯性下行，多头初建 |
| `bull_weakening` | > | < 0 | > 0 | MA5 先拐头向下，即将死叉前兆 |
| `bull_rolling` | > | < 0 | < 0 | 两线均下行，死叉边缘 |
| `bear_strong` | < | < 0 | < 0 | 两线同步下行，持币观望区 |
| `bear_building` | < | < 0 | > 0 | 死叉后 MA10 惯性未消，下跌中继 |
| `bear_weakening` | < | > 0 | < 0 | MA5 尝试上拐，空方减弱 |
| `bear_rolling` | < | > 0 | > 0 | 两线均上行，金叉边缘 |
| `tangle` | — | — | — | 近 10 日交叉 ≥2 次 + 间距<3%（此时忽略斜率） |
| NULL | — | — | — | MA5 或 MA10 值不可用（IPO <10 日、停牌复牌后窗口不足） |

**转折点**（不变）：

| 字段 | 计算方法 |
|------|---------|
| **金叉** | MA5 上穿 MA10 |
| **死叉** | MA5 下穿 MA10 |
| **即将金叉** | MA5 < MA10 + 差值收窄 + \|MA5-MA10\|/\|MA10\| < 15% |
| **即将死叉** | MA5 > MA10 + 差值收窄 + \|MA5-MA10\|/\|MA10\| < 15% |

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
    -- 警惕点（基于 DDX2）
    alert          TEXT,               -- 'upturn_reverse' / 'downturn_reverse' /
                                       -- 'upturn_flat' / 'downturn_flat' / NULL
    -- 背离（DDX2 vs 价格）
    divergence     TEXT,               -- 'top_divergence' / 'bottom_divergence' / NULL
    calc_date      TEXT,               -- 计算日期（回测快照）
    PRIMARY KEY (ts_code, trade_date, calc_date)
);
```

#### 计算口径

| 字段 | 计算方法 | 参数 |
|------|---------|------|
| **net_mf_amount** | 直接取自 `dwd_daily_moneyflow` | — |
| **DDX** | `(buy_lg_vol + buy_elg_vol - sell_lg_vol - sell_elg_vol) / total_vol` | 大单+超大单净买入量占比 |
| **DDX2** | EMA(DDX, 5) | α = 2/(5+1)，种子 = SMA(DDX, 5) |
| **趋势（上升）** | DDX2 连续 3 日递增 | N=3 |
| **趋势（下降）** | DDX2 连续 3 日递减 | N=3 |
| **趋势（走平）** | 不满足上升或下降条件 | — |
| **上升拐头** | 此前 DDX2 连续上升 → 今日 < 昨日 | 单日判定 |
| **下降拐头** | 此前 DDX2 连续下降 → 今日 > 昨日 | 单日判定 |
| **上升走平** | 此前 DDX2 连续上升 → 今日\|变化\|/昨日\| ≤ 2% | 单日判定 |
| **下降走平** | 此前 DDX2 连续下降 → 今日\|变化\|/昨日\| ≤ 2% | 单日判定 |
| **顶背离** | ① close 创近 60 日新高 **且** ② DDX2 < 近 60 日 DDX2 最大值 | 标注在确认日（与 MACD 背离 12.29 一致） |
| **底背离** | ① close 创近 60 日新低 **且** ② DDX2 > 近 60 日 DDX2 最小值 | 同上 |

> **DDX 说明**：tushare 不直接提供东方财富 DDX/DDY/DDZ。本方案使用 `moneyflow` 接口的大单+超大单净买入占比作为 DDX 代理——语义等价（主力资金方向），且与 DDX 的量纲一致（-1 到 +1）。DDY 和 DDZ 因需要逐单数据无法从 tushare 计算，从方案中移除。

---

### 6.5 dws_volume_daily / dws_volume_weekly — 量能

```sql
CREATE TABLE dws_volume_daily (
    ts_code        TEXT,
    trade_date     TEXT,
    ma_vol_5       REAL,               -- MA(vol, 5)，用于区域判定
    pct_vol_rank   REAL,               -- MA5_vol 在近 120 日的百分位排名
    -- 区域
    zone           TEXT,               -- 'explosive' / 'low_volume' / 'normal'
    -- 区域内趋势
    trend          TEXT,               -- 'expanding' / 'shrinking' / 'flat'
    calc_date      TEXT,               -- 计算日期
    PRIMARY KEY (ts_code, trade_date, calc_date)
);
```

#### 计算口径

**区域判定**（基于 MA5_vol 在近 120 日的百分位）：

| 切换方向 | 条件 | 参数 |
|---------|------|------|
| **进入爆量区** | MA5_vol > P90(近120日 vol) **且** 连续 2 日满足 | 120日, P90, M=2 |
| **退出爆量区** | MA5_vol < P75(近120日 vol) **且** 连续 2 日满足 | 迟滞阈值 P75 |
| **进入地量区** | MA5_vol < P10(近120日 vol) **且** 连续 5 日满足 | P10, M=5 |
| **退出地量区** | MA5_vol > P25(近120日 vol) **且** 连续 2 日满足 | 迟滞阈值 P25 |
| **正常区** | 不处于爆量区也不处于地量区 | — |

> 进出阈值不对称（P90 进 / P75 出）形成迟滞效应，防止在阈值边界反复切换。

**区域内趋势判定**（线性回归斜率）：

| 趋势 | 判定 |
|------|------|
| **放量** | 区域内 MA5_vol 序列线性回归斜率 > 0.5%/日 |
| **缩量** | 区域内 MA5_vol 序列线性回归斜率 < -0.5%/日 |
| **平量** | 斜率在 [-0.5%/日, 0.5%/日] 之间 |

> 回归斜率天然同时捕捉"首尾差异 + 过程方向一致性"，避免首尾比 + 单调比例方案在"冲高回落"型序列上的误判。

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

> 将 5 张 DWS 表 + dim_stock 通过 `v_*_latest` 视图 JOIN 为一张大宽表，每个交易日每只股票一行，包含全部技术指标 + 基础信息。数据可直接导出 Excel 做离线分析。

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

    -- K线形态合并为单一字段（子形态优先：阳克阴 > 阳包阴，阴克阳 > 阴包阳）
    CASE
        WHEN k.yang_ke_yin = 1    THEN 'yang_ke_yin'
        WHEN k.yang_bao_yin = 1   THEN 'yang_bao_yin'
        WHEN k.yin_ke_yang = 1    THEN 'yin_ke_yang'
        WHEN k.yin_bao_yang = 1   THEN 'yin_bao_yang'
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

    -- K线形态合并为单一字段（子形态优先）
    CASE
        WHEN kw.yang_ke_yin = 1    THEN 'yang_ke_yin'
        WHEN kw.yang_bao_yin = 1   THEN 'yang_bao_yin'
        WHEN kw.yin_ke_yang = 1    THEN 'yin_ke_yang'
        WHEN kw.yin_bao_yang = 1   THEN 'yin_bao_yang'
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

    -- K线形态合并为单一字段（子形态优先）
    CASE
        WHEN k.yang_ke_yin = 1    THEN 'yang_ke_yin'
        WHEN k.yang_bao_yin = 1   THEN 'yang_bao_yin'
        WHEN k.yin_ke_yang = 1    THEN 'yin_ke_yang'
        WHEN k.yin_bao_yang = 1   THEN 'yin_bao_yang'
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
        WHEN k.yin_ke_yang = 1    THEN 'yin_ke_yang'
        WHEN k.yin_bao_yang = 1   THEN 'yin_bao_yang'
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

    # 信号高亮：K线形态（单一枚举列）
    kpattern_colors = {
        'yang_bao_yin': green, 'yang_ke_yin': green,
        'mu_bei_xian': red, 'bi_lei_zhen': red,
        'gao_kai_chang_yin': red, 'yin_bao_yang': red, 'yin_ke_yang': red,
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
            '均线缠绕': blue,
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
  dwd_weekly_quote       → 对应的 5 张周线表

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
| 阳包阴 | 买入 | 实体吞没阴线，不看影线，不强制量能 | — |
| 阳克阴 | 买入 | 量价双向胜：量>前量×1.2 且 实顶>前实顶 | 量能放大≥1.2x |
| 墓碑线 | 卖出 | 高位无实体Doji+长上影(≥3x实体) | 高位:60日高点10%内或20日涨>15%; Doji:<0.5%或<振幅10% |
| 避雷针 | 卖出 | 高位小实体(<振幅20%)+长上影(≥3x实体)，实体在下1/3 | 高位:同上; 实体<振幅20% |
| 高开长阴 | 卖出 | 高位高开(前涨>15%/10日)，长阴(≥5%)，放量(>MA5×1.5) | 前涨>15%(10日); 阴体≥5%; 量>MA5×1.5 |
| 阴包阳 | 卖出 | 实体吞没阳线，不看影线 | — |
| 阴克阳 | 卖出 | 量价双向胜：量>前量×1.2 且 实底<前实底 | 量能放大≥1.2x |
| **strength** | 强度 | 触发形态的加权评分 (0.0~1.0)，无形态=NULL。权重: 吞没/影线/量能/高位确认 | `[待回测]` |

### 9.2 MACD

| 子类 | 指标 | 判定 |
|------|------|------|
| 基础 | DIF/DEA/MACD柱 | EMA(12,26,9)，前N日SMA种子 |
| 背离 | 顶/底背离 | 严格口径：价新高+DIF未新高 / 价新低+DIF未新低（60 日回溯窗口，标注在确认日） |
| 区域 | 多方/空方 | MACD柱 > 0 / < 0 |
| 转折点 | 金叉/死叉 | MACD柱正负号翻转 |
| | 即将金叉/死叉 | 差值<DEA×15% + 收敛中 |
| 警惕点 | 上升拐头/下降拐头 | 趋势→逆转（单日） |
| | 上升走平/下降走平 | 趋势→停滞（单日变化≤2%） |
| 趋势 | 上升/下降/走平 | MACD柱连续3日同向 |

### 9.3 均线

| 子类 | 指标 | 判定 |
|------|------|------|
| 基础 | MA5/MA10 | 前复权收盘价 SMA |
| | 乖离率 bias_ma5/bias_ma10 | (close-MA)/MA×100 |
| | 斜率 ma5_slope/ma10_slope | 3日间隔变化率 `[待回测]` |
| 趋势 | alignment 9 值 | DWS: bull_strong/bull_building/.../tangle; ADS: 多头强势/多头初建/.../均线缠绕 |
| 转折点 | 金叉/死叉 | MA5上穿/下穿MA10 |
| | 即将金叉/死叉 | 差值<MA10×15% + 收敛中 |

### 9.4 DDE

| 子类 | 指标 | 判定 |
|------|------|------|
| 基础 | DDX（代理）| (大单+超大单净买入)/总成交量 |
| | DDX2 | EMA(DDX, 5), SMA种子 |
| | net_mf_amount | 主力净流入额（万元） |
| 趋势 | 上升/下降/走平 | DDX2连续3日同向 |
| 警惕点 | 上升拐头/下降拐头 | 趋势→逆转（单日） |
| | 上升走平/下降走平 | 趋势→停滞（单日变化≤2%） |
| 背离 | 顶/底背离 | 价创60日新高/低 + DDX2未跟随。标注在确认日 |

### 9.5 量能

| 子类 | 指标 | 判定 |
|------|------|------|
| 区域 | 爆量区 | MA5_vol > P90(120日)，连续2日进入，P75退出 |
| | 地量区 | MA5_vol < P10(120日)，连续5日进入，P25退出 |
| | 正常区 | 非爆量非地量 |
| 趋势 | 放量 | 区域内 ln(MA5_vol) 回归斜率 > 0.005/日 |
| | 缩量 | 区域内 ln(MA5_vol) 回归斜率 < -0.005/日 |
| | 平量 | 斜率在 [-0.005, 0.005]/日 |

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
  2. DuckDB 窗口函数计算 DWS（直接读 DWD 表）
  3. INSERT INTO DWS 表（新 calc_date 行），每批 100 只股票一个事务
  4. 最近 60 个交易日产生新的 calc_date 快照（同一 trade_date 可有多个 calc_date）
  5. 60 日之前的 DWS 行冻结不变（不再产生新 calc_date）
  6. calc_date = 当前跑批日期
```

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

### 12.18 架构图表数更正

> ODS 层实际为 7 表（ods_stock_basic/daily/daily_basic/moneyflow/trade_cal/concept_detail + ods_etl_log），DIM 层实际为 4 表（dim_stock/dim_date/dim_concept/dim_concept_stock）。dim_index/dim_index_member 延后至 ADS 板块功能实现时引入（12.63）。架构图已同步更新。

### 12.19 ods_weekly 表移除

> 周线 DWD 改为从 `ods_daily` 聚合生成后，`ods_weekly` 不再被任何 ETL 步骤引用。已从 ODS DDL 中移除。

### 12.20 MACD 背离回溯窗口

> 顶背离/底背离的峰值比较窗口指定为 **60 个交易日**。"价格创新高"指价格突破近 60 日最高收盘价，"DIF 未创新高"指当前 DIF 峰值低于近 60 日 DIF 峰值。底背离同理。

### 12.21 前复权公式修正（Independent Review 发现）

> 原公式 `price_qfq = price × adj_factor` 实际计算的是**后复权**。tushare 的前复权需要除以最新复权因子。
> 修正公式：`price_qfq = price × adj_factor / latest_adj_factor`，其中 `latest_adj_factor` = 每只股票的最大 trade_date 对应的 adj_factor。
> 影响：MACD/MA/K线形态的形状不变（比例缩放），但零轴兜底公式中依赖 close 绝对值的计算需要前复权修正后才能正确。

### 12.22 DuckDB 写入路径修正（Independent Review 发现）— v1.5 已废弃

> 原方案使用 DuckDB ATTACH SQLite + PyArrow + sqlite3 executemany 三次拷贝写入。v1.5 切换为纯 DuckDB 后，DuckDB 直接 INSERT INTO 自身持久化表，无需 sqlite_scanner 扩展，此修正不再适用。

### 12.23 增量策略改为 INSERT-only 快照模式（Independent Review 发现）

> DELETE+INSERT 破坏性滑动窗口与 calc_date 回测快照存在架构冲突——DELETE 会销毁旧快照。
> 改为 INSERT-only：每日跑批只 INSERT 新 calc_date 行，旧 calc_date 永不清除。每只股票最近 60 日 window 内的行每个交易日产出一个新快照，60 日前的行冻结。所有 DWS 表 PK 扩展为 `(ts_code, trade_date, calc_date)`。

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

### 12.29 MACD 背离确认日标注（Quant Review 发现）

> 顶背离的日期标注从"价格创60日新高的那一天"改为"DIF确认未跟随的**确认日**"——即 DIF 已从峰值回落但价格尚未跌破前高的第一天。消除前视偏差（peak bar 当天 DIF 通常也创新高，背离事实上要到 2-5 天后才可观测）。

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

> 原方案 7 个 1/0 布尔列（`yang_bao_yin`, `yang_ke_yin`, `mu_bei_xian`...）稀疏且冗余——同一交易日最多触发一种 K 线形态。ADS 层合并为单一 `kpattern` 枚举字段（取值 `'yang_bao_yin'` / `'yang_ke_yin'` / ... / NULL）。CASE 优先级顺序作为多重触发的兜底策略（买入形态优先 → 卖出形态）。Excel 导出代码同步改为单列枚举高亮。DWS 层保持原布尔列结构不变（计算层与展示层分离）。

### 12.44 K 线形态量化参数（交易专家 Review）

> 原方案 7 种 K 线形态中，墓碑线/避雷针/高开长阴/阳克阴/阴克阳 存在模糊描述（"长上影线极长""小实体""显著上涨""高位"），无法直接编码。补全精确参数：墓碑线上影≥3x、Doji<0.5%或<振幅10%、高位=60日高点10%内或20日涨>15%；避雷针实体<振幅20%+实体在下1/3；高开长阴前涨>15%(10日)+量>MA5×1.5；阳克阴/阴克阳量能放大≥1.2x。所有阈值标注 `[待回测调优]`。

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

### 12.53 dws_ma.alignment NULL 语义明确（Data Architect Review 未优化项）

> alignment 取 NULL 的场景：MA5 或 MA10 值不可用（IPO 上市不足 10 日 MA10 尚未计算，或停牌复牌后 MA 窗口内有效交易日不足）。

### 12.54 dim_date 补 is_year_end（Data Architect Review 未优化项）

> dim_date 新增 `is_year_end INTEGER` 字段，与已有的 `is_week_end`/`is_month_end` 对齐，用于年末统计（年线收盘价、年度涨幅等）。

### 12.55 total_vol 口径说明（Data Architect Review 未优化项）

> dwd_daily_moneyflow.total_vol 选用买入侧求和（buy_sm + buy_md + buy_lg + buy_elg），与 DDX 分子的大单+超大单净买入方向一致。注明了 tushare 买卖分类基于主动方向，两侧合计理论上等价但实际可能有微小偏差。

### 12.56 ods_etl_log DDL 一致性修正（Data Architect Review 未优化项）

> 将 `data_completeness` 字段直接纳入 12.2 节 CREATE TABLE 主 DDL，消除与 12.8c ALTER TABLE 的冗余。

### 12.57 adj_factor 接口表述统一（Data Architect Review 未优化项）

> 1.2 节和 11 节统一表述：`adj_factor` 由 `daily` 接口返回，内含于日线数据中，无需独立 API 拉取。

### 12.58 K 线形态 strength 强度评分（交易专家 Review）

> dws_kpattern 新增 `strength REAL` 列（0.0~1.0）。7 种形态各定义加权公式，触发哪个形态用哪个公式；子形态（阳克阴/阴克阳）权重侧重量能超越，天然强于父形态。ADS 宽表映射为 `kpattern_strength`。

### 12.59 均线指标重构（交易专家 Review）

> 删除 `ma5_flat`/`ma10_flat`/`ma_all_flat` 三个布尔列。新增 `bias_ma5`/`bias_ma10`（乖离率）、`ma5_slope`/`ma10_slope`（3 日斜率）。`alignment` 从 3 值扩展为 9 值枚举（bull_strong/bull_building/bull_weakening/bull_rolling/bear_strong/bear_building/bear_weakening/bear_rolling/tangle），基于位置+双斜率方向组合。DWS 层存英文码，ADS 层 CASE WHEN 翻译为中文展示文本。

### 12.60 DDE 背离信号（交易专家 Review）

> dws_dde 新增 `divergence TEXT` 列（top_divergence/bottom_divergence/NULL）。判定逻辑与 MACD 背离一致，将 DIF 替换为 DDX2：价格创 60 日新高/低 + DDX2 未跟随。标注在确认日消除前视偏差。ADS 宽表映射为 `dde_divergence`。

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
