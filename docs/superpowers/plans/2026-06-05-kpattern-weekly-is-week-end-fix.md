# kpattern 周线 is_week_end 采样修复

**日期：** 2026-06-05
**范围：** `backend/etl/calc_kpattern.py` weekly 查询补 `is_week_end=1` 过滤；新增测试

## 根因
`dwd_weekly_quote` 为**滚动周线**（每交易日一行），真周末 bar 由 `dim_date.is_week_end=1` 标记。
其余 5 个周线计算器（MACD/MA/DDE/Volume/PricePosition）weekly 路径均 `JOIN dim_date ... WHERE dd.is_week_end=1`。
唯独 `calc_kpattern.py` 直接查全表，导致：
1. **膨胀**：1.37M 行 / 342 trade_date（其他周线表 285k / 73）。
2. **错误**：在同一周的部分累积 bar 上做形态识别（比较 `bar[i-1]/bar[i]`）无意义。

## 修复
`KPatternCalculator.calculate()` 的查询拆分 daily/weekly 分支（对齐 MACD）：
- weekly：`JOIN dim_date dd ON d.trade_date = dd.trade_date WHERE d.ts_code=? AND dd.is_week_end=1`
- daily：`WHERE ts_code=? AND is_suspended=0`

## 测试（TDD）
`tests/test_etl/test_calc_kpattern_weekly.py`（新建）
- RED：构造 dwd_weekly_quote 多根滚动 bar（同周多日）+ dim_date 标记部分 is_week_end=1，跑 `KPatternCalculator(freq="weekly").calculate`，断言 `dws_kpattern_weekly` 的 trade_date 全部 ⊆ is_week_end 日期集合（intra-week 滚动 bar 不入库）。
- GREEN：补 is_week_end 过滤后通过。

## 存量数据补救（实库手动，本批不含）
`v_*_latest` 取 MAX(calc_date)，旧污染 intra-week 行不会被新批次覆盖 → 残留为幽灵。
修复部署后须 `DELETE FROM dws_kpattern_weekly` 再重算（用户执行）。

## 不在本批
- 不在 run_calc/CLI 加自动清理。
- 不动 `%Y-%W` 跨年周切分（Fix 3，另议）。
