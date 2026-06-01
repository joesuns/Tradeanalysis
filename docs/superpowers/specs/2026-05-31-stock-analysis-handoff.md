# Handoff: TradeAnalysis 个股技术分析数据模型

## 会话摘要

通过 `/grill-me` 技能完成了一轮完整的指标体系口径对齐 + 数据模型设计访谈。用户是数据开发专家，拥有 tushare 6000+ 积分权限，目标是为 TradeAnalysis 项目构建个股技术分析数据模型。

## 最终交付物

完整设计文档已沉淀到项目文件：
**[docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md](/Users/joesun/Trae/Tradeanalysis/docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md)**

## 关键技术决策

### 技术架构
- **存储**：SQLite 持久化 + DuckDB 分析查询（不引入 PostgreSQL）
- **部署**：与现有新闻分析模块共享 FastAPI 进程，独立表 + 独立路由 + 独立前端页面
- **粒度**：先实现日线/周线，后续扩展 30分钟/月线
- **历史窗口**：5-10 年全 A 股数据（~5GB）

### 数仓分层
```
tushare API → ODS(8表) → DIM(3表) + DWD(3表) → DWS(10表：5日线+5周线) → ADS(后续)
```

### 指标类别（5 大类，全部口径已对齐）

| 类别 | DWS 表 | 子类覆盖 |
|------|--------|---------|
| K线形态 | `dws_kpattern` | 阳包阴、阳克阴（买入）；墓碑线、避雷针、高开长阴、阴包阳、阴克阳（卖出） |
| MACD | `dws_macd` | 背离（严格口径）、区域（柱体正负）、转折点（金叉/死叉+即将15%）、警惕点（拐头+走平）、趋势 |
| 均线(MA5/MA10) | `dws_ma` | 多头/空头/缠绕、走平(0.5%)、金叉/死叉+即将(15%) |
| DDE | `dws_dde` | DDX2=EMA(DDX,5)，趋势、拐头+走平(0.5%) |
| 量能 | `dws_volume` | 区域（120日百分位P90/P10，迟滞进出）、趋势（回归斜率0.5%/日） |

### 核心计算参数
- 前复权：`price × adj_factor`
- EMA 种子：前 N 日 SMA
- 趋势判定 N=3（MACD/DDE）
- 走平阈值：0.5%
- 即将金叉/死叉：15%
- 量能区域 P90/P10，迟滞 P75/P25
- 均线缠绕：10日窗口，交叉≥2，间距<3%

## 下一阶段建议

### 优先任务（按顺序）

1. **搭建 ODS 层**：编写 tushare 数据拉取脚本（8 张表），设计增量更新策略
2. **构建 DIM + DWD**：ETL 清洗 + 前复权计算 + 维度表构建
3. **实现 DWS 计算引擎**：用 DuckDB 实现 5 大类指标的计算逻辑
4. **周线版本**：在日线稳定后，复用逻辑构建周线表
5. **集成到 FastAPI**：新增 API 路由，DuckDB 查询服务

### 建议使用的技能
- `/writing-plans` — 将实现拆分为分步执行计划
- `superpowers:subagent-driven-development` — 多子代理并行开发

### 项目结构参考
现有后端在 `/Users/joesun/Trae/Tradeanalysis/backend/`，FastAPI + SQLite 架构已完成。新增个股分析模块时建议新增文件（如 `stock_data.py`、`indicator_calc.py` 等），不修改现有新闻分析模块代码。

## 未解决/待对齐事项

1. **K 线形态"空中楼阁"**：用户原 prompt.md 中存在此形态，访谈中替换为其他形态——需确认 prompt.md 是否需要更新
2. **ADS 层**：用户明确表示"暂时没有 ADS 分析需求，后续会有"——暂不实现
3. **30分钟/月线级别**：后续扩展，当前仅日线+周线
4. **前端页面**：个股分析的前端展示页面尚未设计
5. **概念板块归属（DIM）**：用户用的是申万行业还是同花顺行业？当前设计默认申万
