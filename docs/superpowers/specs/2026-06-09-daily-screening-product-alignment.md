# 日终选股产品对齐 Spec

> 日期：2026-06-09 | 状态：**已审批** | 上游：Grill-me 会话（2026-06-09）
> 关联：`2026-05-31-stock-analysis-data-model.md` (v1.11)、`2026-06-07-calc-append-only-design.md`

---

## 1. 产品目标（已锁定）

### 1.1 一句话

每日收盘后一条命令产出 **全市场最新交易日** 技术指标宽表 Excel（**左侧日线 + 右侧周线合并列**），用于选股；**准确优先**；稳态日更完整 `run` ≤ **30 分钟**。优化与验收 **日线、周线同等优先级**，不得只做日线。

### 1.2 决策表

| ID | 维度 | 决策 |
|----|------|------|
| **A** | 交付粒度 | 主交付：每股 × **最新交易日** 一截面（非全历史逐 bar 库） |
| **A3** | 库内历史 | **双轨 tail**：日线 DWS **250 交易日**；周线 DWS **250 个真周末 bar**（对齐 `RECALC_SPEC_WEEKLY` max lookback=250） |
| **D1** | 主入口 | Excel 宽表 `exports/analysis_{date}_gen{now}.xlsx` |
| **C3** | SLA | **稳态**完整 `run`（fetch + DWD + calc + export）墙钟 ≤ **30min**；不含 `--force`、大规模补拉 |
| **B5** | 准确性分层 | 主：**B2** + **B4**；扩展列：**E3** 软层 |
| **E3** | B4 范围 | **12 列硬门禁**（日周各 6，不含 `ma_alignment`）；`ma_alignment` 为 TA 自研软层；其余 B2 + 人工抽检 |
| **F3** | 跑批门禁 | B4 golden 失败 **硬阻断**；`health_check` 其余 **软告警** |
| **G2** | Golden 冻结 | **5 代表日** × **分层约 500 股**（共用 `sample_500.csv`） |
| **P3** | 实施顺序 | B4 通过 → 冻结 G2 → 压 30min → **删项目 123**；性能与对齐 **可并行**，但 **未冻结 golden 前不认 30min KPI** |

### 1.3 B4 列（extract 14 列；硬门禁 12 列）

**硬门禁（日 6 + 周 6，对称）：**

| 123 列名 | Tradeanalysis 字段 | Excel 中文列名 |
|----------|-------------------|----------------|
| `short_macd_trend` | `macd_trend` | MACD趋势 |
| `short_macd_signal` | `macd_zone` | MACD区域 |
| `daily_rev_macd_hist_turn` | `macd_alert` | MACD警惕 |
| `short_dde_trend` | `dde_trend` | DDE趋势 |
| `daily_rev_ddx2_slope_reversal` | `dde_alert` | DDE警惕 |
| `short_volume_trend` | `vol_trend` | 量能趋势 |

**软层（extract/Excel 保留，不对 123 硬比）：**

| 123 列名 | Tradeanalysis 字段 | Excel 中文列名 | 说明 |
|----------|-------------------|----------------|------|
| `short_ma_regime` | `ma_alignment` | 均线形态 | **2026-06-09 决策**：保留 TA MA5/MA10 十值枚举，不迁 123 `ma_regime`（MA5/10/20/30 + 不同阈值） |

### 1.3.1 周线 B4（与日线对称，硬门禁一半）

周线 7 列与上表 **同字段名**，来源：

- 视图：`v_ads_analysis_wide_weekly`
- 锚点：`week_end = MAX(dim_date.trade_date WHERE trade_date ≤ 分析日 AND is_week_end=1)`
- 导出：`export_wide` 对周线指标列加 `__w__` 前缀合并（如 `__w__macd_trend`）

Golden / diff 每条记录须含：

- `trade_date`（分析日，日线锚点）
- `week_end`（周线锚点，可与分析日不同）
- 日线 6 列硬门禁 + 周线 6 列硬门禁（共 **12** 列数值门禁）
- 可选：`ma_alignment` / `w_ma_alignment` 写入 extract 供 Excel，**不进** golden 硬比

123 周线列名若仍为 `short_*` / `daily_rev_*`，diff 脚本按 **freq=weekly** 映射，不按列名里的 `daily` 字面义。

**软层（不进 B4 硬门禁）**：`ma_alignment`（TA 自研）、MACD/DDE 结构背离、转折/金叉死叉、K 形态强度、价格分位、量比/区域、量价背离/信号等（日线+周线扩展列均软层）。

### 1.3.2 周线日更语义（验收必写清）

| 场景 | 日线 | 周线 |
|------|------|------|
| 非周末交易日 `run` | 每股 APPEND 1 根日线 | **无新 week-end bar** → 12 指标 weekly 多为 **SKIP**（正常） |
| 周五/分析日=week_end | APPEND 1 日线 | 成熟股 APPEND 1 周末 bar（6 计算器 × weekly） |
| 导出 | `trade_date = 分析日` | 取 `week_end ≤ 分析日` 的最新周末截面 |

30min SLA 与 batch APPEND 验收须 **同时统计** `(indicator, daily)` 与 `(indicator, weekly)`；仅日线达标不算完成。

### 1.4 B4 生命周期（≠ 每日对 live 123 diff）

| 阶段 | B4 做法 | 123 |
|------|---------|-----|
| 过渡期 | `diff_vs_123`：5 日 × 500 股，修到 mismatch≈0 | 活参照 |
| 冻结期 | 写入 `tests/fixtures/b4_gate/golden_{date}.csv` | 只读快照 |
| 稳态日更 | **不跑** B4；仅 B2 + F3 软层 | 已删除 |
| 再触发 | 改 **12 列硬门禁**相关算法 / adj 大回填 / 周线修复后 | 无；跑 pytest `test_b4_gate` |

`diff_vs_123` **不挂** `cli run` 默认路径；仅 CI / 发布前 / `--verify-b4`。

### 1.5 G2 分层抽样（约 500 股，5 日共用）

| 桶 | 约数量 | 说明 |
|----|--------|------|
| 主板成熟 | ~200 | `dwd_rows≥250` |
| 创业板 | ~100 | 300xxx |
| 科创板 | ~80 | 688xxx |
| 上市不足 1 年 | ~50 | partial 指标 |
| 高换手活跃 | ~50 | 量能/DDE 有信号 |
| 北交所 `.BJ` | ~20 | DDE 空；DDE 周线 **单独 bucket**，不与 123 比 DDE |

代表日类型（具体 `YYYYMMDD` 首次 B4 通过时写入 `dates.txt`）：最近稳态日、跨年附近、长假后首日、下跌段样本、上涨段样本。

---

## 2. 与 v1.11 数据模型的关系

| 主题 | v1.11 现状 | 本产品目标 | 处理 |
|------|-----------|-----------|------|
| DWS 存储 | 全历史 + 多 `calc_date` INSERT-only 快照 | 日/周 **双轨 tail** + 日更 1 bar（周仅在 week_end） | **推倒重来（存储收敛）**，见 §4.2 |
| 历史深度 | 2015 至今全量 DWS | ODS/DWD 长历史；DWS 日线 250td + 周线 250 week-end | 分阶段：先双频 APPEND 吃满，再 tail prune |
| 验收 | pytest + health_check | + B4 golden + F3 分层门禁 | **新增**，不改 v1.11 指标公式定义 |
| API screening | 存在 | 非主交付 | 保留，不优先 |

v1.11 中指标 **公式与字段语义** 仍以 data-model spec 为准；本 spec 只约束 **产品契约、验收与性能 SLA**。

---

## 3. 现状审计（2026-06-09）

### 3.1 已对齐或接近目标（保留）

| 能力 | 位置 | 说明 |
|------|------|------|
| 一站式 `run` | `backend/cli.py` | fetch → rebuild DWD → calc → export |
| DWD 增量 | `build_dwd.py`, `DWD_INCREMENTAL` | stale 子集 tail rebuild；qfq 漂移检测 |
| Calc APPEND 双路径 | `calc_router`, `append_calculate`, `run_batch_append_phase` | 日+周 12 表路由；`dws_calc_state` PK 含 `freq` |
| DWD 周线 | `dwd_weekly_quote`, `find_stale_dwd_codes` 三表 | stale 含 weekly；`repair-weekly` 一次性运维 |
| 周线 batch | `calc_batch_append.py` `weekly_tails`, `dde_weekly` | 与日线同批 APPEND 相位 |
| Excel 周线合并 | `export_wide.py` week_end 锚点 | D1 交付含 `__w__*` 列 |
| 同日复跑短路 | 幂等闸门、fast skip、force batch reuse | 与 C3 稳态相关 |
| Excel 宽表 | `export_wide.py`, `v_ads_analysis_wide_*` | D1 主交付 |
| 质量体检 | `scripts/health_check.py` | B2；尚未按 F3 接入 `run` |
| 进度观测 | `progress.py`, `ods_etl_log` | 30min 验收依赖 |

### 3.2 不满足目标（需改或推倒）

| 缺口 | 根因 | 严重度 | 处理策略 |
|------|------|--------|----------|
| **无 B4 回归** | 未建 `diff_vs_123` / `b4_gate` fixtures / pytest | P0 | 新建；轨 A 优先 |
| **稳态 run 未接 F3** | `cmd_run` 未调 `health_check`；无 B4 硬阻断 | P0 | `run` 末尾软告警；B4 仅发布路径 |
| **30min 未验收** | 实库新日仍可能 FULL 风暴 / fetch 拖尾 | P0 | 轨 B；golden 冻结后认 KPI |
| **周线 APPEND 等价性未锁** | `test_append_calc.py` 仅 **daily** 路径 | P0 | 扩展 weekly 等价测试（6 计算器） |
| **周线 FULL 风暴** | week_end 日若 weekly state 失效 → DDE 周线历史曾 ~50min | P0 | 与日线同吃 batch APPEND；观测 weekly APPEND 比 |
| **DWS 全历史多快照** | v1.11 INSERT-only；12 表 × 日/周双轨行数 | P1 | **推倒存储策略**（§4.2），日周分别 tail |
| **B4 周线未显式验收** | golden 若只存日线 6 列则漏一半硬门禁 | P0 | golden 必须 **12 列硬门禁** + `week_end` |
| **API 非主入口但仍占心智** | `router.py` screening | P2 | 不删，文档降级 |
| **结构背离 vs 123** | MACD/DDE 已结构法；123 可能 rolling | — | 已在 E3 软层；不进 B4 硬门禁 |

### 3.3 可保留但需验证的「优化层」

以下在 B4 未通过前 **只作工程优化，不替代 B4**：

- `CALC_INCREMENTAL` 窄窗指纹跳过（同日有效，新日无效）
- `CALC_FAST_SKIP` / partial skip v2（同日复跑）
- `prune --keep N`（运维手动；未实现 250 日自动 tail）

---

## 4. 目标架构（审批后实施）

### 4.1 日更数据流（稳态，日+周双轨）

```
tushare → ODS(当日)
       → DWD 增量 daily + weekly + moneyflow（stale 子集）
       → calc：
           daily：每股每指标 APPEND 1 交易日 bar（常态）
           weekly：仅 week_end 日 APPEND 1 周末 bar（非周末 SKIP）
       → v_dws_*_daily_latest + v_dws_*_weekly_latest
       → export：日线 + week_end 周线 __w__ 列
       → health_check（含 Section I 周线填充，WARNING only）
```

B4 golden：仅 `pytest tests/test_b4_gate*.py` 或 `python -m scripts.verify_b4_gate`（发布/改算法）；**必须比对 12 列硬门禁**。

### 4.2 存储收敛（可推倒重来，日周分轨）

**目标**：每个 `(ts_code, freq, indicator)`：

| freq | 保留行数（tail） | 日更写入 |
|------|------------------|----------|
| `daily` | ≤ **250 交易日** | 每个交易日 1 bar |
| `weekly` | ≤ **250 真周末 bar** | 仅 `is_week_end=1` 日 1 bar |

另：**单一有效 calc 快照**（或 `prune --keep 1` 等价语义）。周线 tail 用 `dim_date.is_week_end` 计数，**禁止**用 250 交易日近似折算周数。

**证据（代码 RECALC_SPEC_WEEKLY lookback）**：

| 指标 | weekly lookback |
|------|-----------------|
| MACD / DDE / PricePosition | 250 |
| Volume | 120 |
| KPattern | 60 |
| MA | 10 |

fetch 门禁成熟股 **120 week-end**（`check_data_completeness.weekly_fetch`）独立于 DWS tail 250w，ODS/DWD 周线历史须覆盖两者之大。

**建议方案（待实施计划细化）**：

1. **Phase 1（不改表结构）**：`prune --keep 1`；ODS/DWD 长历史；验收日周 APPEND 比例。
2. **Phase 2（推倒写入契约）**：
   - daily / weekly **分别** INSERT-only 新 bar + DELETE 超 tail；
   - 弱化默认同 `trade_date` 多 `calc_date` 快照；
   - `repair-weekly --execute` 后须重算 weekly 并刷新 golden 周线列。

**明确推倒范围**：若 Phase 2 落地，以下「为多快照服务」的复杂度可简化或删除：

- 同 `trade_date` 多 `calc_date` 行的 **默认写入**（保留 latest 一行即可）
- 依赖「窄窗 255 行重写」的 **FULL 默认路径**（除权/adj 触发 FULL 后仍需要，但常态应几乎全 APPEND）

**不推倒**：`dws_calc_state`、`append_calculate`、batch append、`v_*_latest` 视图模式。

### 4.3 验收门禁（F3）

| 检查 | 稳态 `run` 默认 | 失败行为 |
|------|----------------|----------|
| B4 golden pytest | 否 | CI/发布：exit≠0 |
| `health_check` | 是（`run` 末尾） | WARNING + `ods_etl_log` |
| export 行数 <80% 活跃股 | 是 | 已有 WARNING |
| health_check Section I（成熟股周线 volume） | 是（`run` 末尾） | WARNING |
| B4 硬匹配（若显式 `--verify-b4`） | 可选 | exit≠0；**日线 7 + 周线 7** |

---

## 5. P3 实施阶段

### 阶段 0 — 脚手架（轨 A + 轨 B 并行）

- `tests/fixtures/b4_gate/`：`dates.txt`、`sample_500.csv`、**列映射（12 列硬门禁 + week_end）**、枚举表
- `scripts/diff_vs_123.py`（过渡期，日+周）
- `scripts/verify_b4_gate.py` + `tests/test_b4_gate_regression.py`
- 实库墙钟模板：`run_fetch` / `run_rebuild_dwd` / `calc`（分 daily/weekly APPEND 计数）/ `run_export`
- `tests/test_etl/test_append_calc.py`：**补充 weekly** 等价用例（与 daily 同 atol）

### 阶段 1 — B4 对齐（轨 A，闸门 1）

- 5×500 diff vs 123 → **日线 7 + 周线 7** mismatch≈0（BSE DDE 日周均单独 bucket）
- 至少 1 个代表日为 **week_end**（验证周线 APPEND 路径）
- **未通过不得冻结 golden**

### 阶段 2 — 冻结 G2（闸门 2）

- `golden_{date}.csv`：`ts_code, trade_date, week_end` + **12 列**硬门禁字段
- pytest 锁定日+周；123 可退役准备

### 阶段 3 — 30min SLA（轨 B，闸门 3）

- 稳态新日 `run` ×3 日 ≤30min（含周线 calc + 导出 `__w__` 列）
- 观测：`batch_append` daily/weekly APPEND 占比、DWD daily+weekly skip、`repair-weekly` 后无 weekly FULL 风暴
- week_end 日与 non-week_end 日各至少测 1 次（非周末周线 SKIP 属正常）
- **仅 golden 冻结后认 KPI**

### 阶段 4 — 稳态运维

- `run` 接 `health_check`（F3 软层）
- 123 删除；B4 仅 CI/改算法
- 可选：Phase 2 存储收敛

---

## 6. 受影响模块清单（全量审计）

| 模块 | 文件/区域 | 阶段 |
|------|-----------|------|
| B4 回归（12 列硬门禁） | `scripts/diff_vs_123.py`, `verify_b4_gate`, `tests/fixtures/b4_gate/` | 0–2 |
| CLI `run` | `backend/cli.py`（health_check Section I、verify-b4） | 3–4 |
| 导出周线 | `export_wide.py`, `v_ads_analysis_wide_weekly` | 0–2 |
| Calc 日+周 | `orchestrator.py`, `calc_router.py`, `calc_batch_append.py`, 6×`calc_*.py` | 1–3 |
| DWD 周线 | `build_dwd.py` `dwd_weekly_quote`, `find_stale_dwd_codes` | 3 |
| 周线运维 | `repair_weekly.py`, weekly `dws_calc_state` 清空 | 1 后按需 |
| APPEND 等价 | `tests/test_etl/test_append_calc.py`（**补 weekly**） | 0–1 |
| DWS 双轨存储 | `insert_dws_batch*`, `prune_*`, 12 张 DWS 表 | 4 |
| health_check | `scripts/health_check.py` Section I | 3–4 |
| 文档 | `CLAUDE.md`, v1.11 §双轨 tail 说明 | 各阶段收尾 |

---

## 7. 非目标（本期不做）

- 30 分钟 **K 线** freq（后续独立 spec；**本期周线 W 频已纳入**）
- 将结构背离 MACD/DDE 纳入 B4 硬门禁
- 每日自动对 live 123 diff
- 多进程 calc（DuckDB 单写进程限制）

---

## 8. 审批检查项

审批本 spec 即同意：

1. 产品契约以 §1 为准（**含周线 7 列硬门禁与双轨 tail**），可与 v1.11「全历史 DWS 快照」**有意偏离**（§4.2）。
2. P3 顺序：**B4（12 列硬门禁）→ golden → 30min（日+周）→ 删 123**。
3. 对不满足目标的实现 **允许推倒重来**（存储、缺 weekly 测试、仅优化日线路径）。
4. 下一阶段输出 **实施计划** `docs/superpowers/plans/2026-06-09-daily-screening-impl.md`（不写代码直至计划再审批）。

---

**实施计划：** `docs/superpowers/plans/2026-06-09-daily-screening-impl.md`（2026-06-09 审批后撰写）。
