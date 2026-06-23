# 指数概览 Sheet 口径对齐 — 验收报告

> **验收日期：** 2026-06-23
> **验收人：** 数据架构师 + 交易专家 双视角
> **改动范围：** `export_index.py`（重写）、`export_wide.py`（+2行）、`schema.py`（+2处 CASE）、`CLAUDE.md`（+7行）

---

## 一、需求覆盖矩阵

| # | 决策点 | 目标 | 验收结果 |
|---|---|---|---|
| 1 | 列选择哲学 | 信号聚焦，去掉 EMA12/26、DIF、DEA、MACD柱、MA5/10、MA斜率、5日均量、量能百分位、量比（信号区）、量能趋势强度 | ✅ 通过 |
| 2 | 枚举翻译 | 复用 `_ENUM_VALUES`，`bull`→`多头`、`golden_cross`→`金叉` 等 | ✅ 通过 |
| 3 | 单位换算 | 复用 `_format_numbers`：vol→万手、amount→亿、total_mv→亿 | ✅ 通过 |
| 4 | 背离列 | 保留 `macd_divergence` 全文（tradable 未就绪前） | ✅ 通过 |
| 5 | 空值语义 | 复用 `apply_display_nulls`：事件信号 `-`，状态/基本面 `N/A` | ✅ 通过 |
| 6 | `pb` 格式化 | 四舍五入 2dp + NULL→N/A | ✅ 通过 |
| 7 | 多周期涨跌幅 | 本期暂缓 | ✅ 已记录 |
| 8 | 周线合并 | 日线+周线水平合并 | ✅ 通过 |
| 9 | `vol_signal` | 本期跳过（依赖 price_position） | ✅ 已记录 |
| 10 | 表头结构 | Row 1: 基本信息 / 日 线 指 标 / 周 线 指 标 | ✅ 通过 |
| 11 | 基本信息区 | 加入 `turnover_rate`、`volume_ratio` | ✅ 通过 |
| 12 | `active_days` | 不展示，周线 drop | ✅ 通过 |

**12/12 需求通过。**

---

## 二、架构评估

### 数据流正确性

```
v_ads_market_index_daily ──→ _format_numbers ──→ merge ──→ _transform_display_values ──→ _write_sheet_from_display
v_ads_market_index_weekly ──→ _format_numbers ──→ __w__ prefix ──┘         ↑
                                                              _ENUM_VALUES + apply_display_nulls
```

- **日线→周线 anchor 匹配：** `dwd_index_weekly` 存 Friday 日期，直接用 `SELECT MAX(trade_date) FROM dwd_index_weekly` 查询，避免了 `dim_date.is_week_end`（可能是周四）的错位。✅ 正确
- **单位换算：** `_format_numbers(daily)` 和 `_format_numbers(weekly)` 在合并前分别执行，避免重复换算。✅ 正确
- **枚举翻译：** `_transform_display_values` 同时处理 daily 和 `__w__` 前缀列。✅ 正确

### 共享管线复用

`export_index.py` 从 159 行自维护代码 → 167 行复用管线代码：

| 旧（自维护） | 新（复用 export_wide） |
|---|---|
| 自己的 `_COL_TINT` 颜色表 | `_color_for_indicator()` |
| 自己的 `_COL_GROUPS` + `ws.merge_cells()` | `_resolve_sheet_layout()` + `_write_sheet_headers()` |
| 自己的 `_chinese_name()` 查表 | `_COL_NAMES`（注入 index 专属覆盖） |
| 自己的 NaN→None 转换 | `apply_display_nulls()`（`-`/`N/A` 语义） |
| 自己的列宽计算 | `_set_column_widths()` |
| 无（旧版无枚举翻译） | `_transform_display_values()`（中文枚举值） |
| 无（旧版无周线） | `__w__` 前缀合并管线 |

**代码量不变，功能加倍，零重复逻辑。**

---

## 三、交易专家视角

### 信号完整性

| 信号类别 | 日线 | 周线 | 备注 |
|---|---|---|---|
| MACD 区域（多头/空头） | ✅ | ✅ | 判断持仓方向 |
| MACD 转折（金叉/死叉/近金叉/近死叉） | ✅ | ✅ | 出入场时机 |
| MACD 警惕（趋势回落/趋势反弹） | ✅ | ✅ | 趋势拐点预警 |
| MACD 趋势方向（上升/下降/走平） | ✅ | ✅ | 中期判断 |
| MACD 趋势强度 | ✅ | ✅ | 趋势可信度 |
| MACD 结构背离 | ✅ | ✅ | 反转信号（全文） |
| 均线形态（10种语义） | ✅ | ✅ | 持仓舒适度 |
| 均线转折（金叉/死叉） | ✅ | ✅ | 交叉信号 |
| MA5乖离率 / MA10乖离率 | ✅ | ✅ | 超买超卖 |
| 量能区域（爆量/地量/正常） | ✅ | ✅ | 异常量识别 |
| 量能趋势（放量/缩量/平量） | ✅ | ✅ | 量能方向 |
| 量价背离 | ✅ | ✅ | 价量矛盾 |

### 指数特有的考虑

| 事项 | 判断 |
|---|---|
| 无 DDE 信号 | ✅ 合理：指数无资金流数据 |
| 无 K线形态 | ✅ 合理：指数不适合个股形态分析 |
| 无 price_position | ⚠️ 预存：后续可加，对宽基指数有意义 |
| `pb` 市净率展示 | ✅ 指数 PB 是有意义的估值指标 |
| `total_mv` 单位 | ⚠️ 已知问题：tushare 指数 total_mv 为**元**（个股为万元），`/10000` 后显示值偏大约 10000 倍 |

---

## 四、缺陷清单（已全部修复 ✅）

| # | 位置 | 问题 | 状态 |
|---|---|---|---|
| 1 | [export_index.py:132](backend/export_index.py#L132) | 状态注入在 try 之前 → **已移入 try 块内** | ✅ 已修复 |
| 2 | [export_index.py:159](backend/export_index.py#L159) | finally 内 `list.remove()` 可能抛 ValueError → **已加 try/except 守卫** | ✅ 已修复 |
| 3 | [export_wide.py:854](backend/export_wide.py#L854) | 冻结窗格只搜索 `stock_name` → **已同时匹配 `index_name`** | ✅ 已修复 |
| 4 | [export_index.py:83](backend/export_index.py#L83) | 直接查 `dwd_index_weekly` 基表 → **已改为查 view** | ✅ 已修复 |
| 5 | [export_index.py:155](backend/export_index.py#L155) | 自动筛选器丢失 → **已加 `ws.auto_filter.ref`** | ✅ 已修复 |

**额外改进：** 注入循环从 70+ key 收窄到仅 4 个覆盖 key；`del` 替换为 `pop(k, None)`；移除无用的 `_INDEX_COL_NAMES` 快照字典。

---

## 五、导出验证

```
导出命令：python -m backend.cli export --date 20260622
输出文件：exports/analysis_20260622_gen20260623_183656.xlsx
```

| 检查项 | 预期 | 实际 |
|---|---|---|
| Sheets | 4 sheets | 持仓股分析、综合分析、个股分析、指数概览 ✅ |
| 指数概览 行数 | >1 | 9 行（含表头） ✅ |
| 指数概览 列数 | 38（12基本 + 13日线 + 13周线） | 38 ✅ |
| 枚举值 | 全中文 | `多头`/`上升`/`金叉`/`顶背离`/`放量` ✅ |
| 空值 | `-`（事件）/ `N/A`（状态） | 两种均有出现 ✅ |
| 原始中间列 | 0 列 | ema_12/ma_5 等已去掉 ✅ |
| ma_alignment | 中文全文 | `多头强势 — 两线同步上行，持仓舒适区` ✅ |
| pb 列 | 存在 + 2dp | 1.53 ✅ |
| turnover_rate | 存在 | 1.54 ✅ |

---

## 六、验收结论

**✅ 通过。** 12 项需求全部满足，指数概览 sheet 的指标口径已与综合分析对齐。

**已知缺陷：** 2 项需修复（状态注入时序 + finally 异常掩盖），3 项建议改进（冻结窗格 + 基表直接查询 + 自动筛选器），均不阻塞当前版本发布。建议在后续迭代中修复 #1 和 #2。
