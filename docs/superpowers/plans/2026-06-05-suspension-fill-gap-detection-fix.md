# 停牌填充缺陷修复 — gap 检测对齐交易日历

**日期：** 2026-06-05
**范围：** 修 `build_dwd_daily_quote` Step 2 gap 检测；改正 `test_suspension_detection` 为内部缺口场景
**不在本批：** 生产库全量重建（破坏性/耗时，单独请示）；导出空周线 KeyError（独立小修）

## 根因（证据）

`backend/etl/build_dwd.py:96-111` gap 检测用 `dwd.n < ods.n`：

```96:102:backend/etl/build_dwd.py
    gap_rows = con.execute("""
        SELECT q.ts_code
        FROM (SELECT ts_code, COUNT(*) AS n FROM dwd_daily_quote GROUP BY ts_code) q
        JOIN (SELECT ts_code, COUNT(*) AS n FROM ods_daily GROUP BY ts_code) o
          ON q.ts_code = o.ts_code
        WHERE q.n < o.n
    """).fetchall()
```

step1 是 ODS→DWD 的 1:1 插入，`dwd.n == ods.n` 恒成立 → `gap_stocks` 恒空 → 填充分支被 `continue` 跳过。**结论：内部停牌缺口在 DWD 永不被填。**

实测：ods 有 0101、0103（缺 0102），build 后 0102 未填（应填为 is_suspended=1）。

注：`test_suspension_detection` 原用例测「尾部缺口」（只有 0101 期望 0102 填），但填充上限 `cal.trade_date <= max(该股 ods 日期)` 决定尾部本就不填 —— **原测试用例错误**，需改为内部缺口。

## 爆炸半径（已核实）

- 全部 6 个日线计算器均 `WHERE is_suspended = 0`（macd/ma/volume/price_position/kpattern/dde 已 grep 确认）→ 补停牌行**不进入任何指标输入** → 指纹不变 → **DWS 指标值不变**。
- `build_dwd_weekly_quote` 亦 `WHERE is_suspended = 0` → 周线不变。
- 受影响：`dwd_daily_quote` 行数增加；`v_ads_analysis_wide_daily`/导出会正确纳入当日停牌股（is_suspended=1, vol=0）。
- 修复仅限 `build_dwd_daily_quote` Step 2，填充 SQL 本身（LATERAL + `NOT EXISTS` + `prev.close_qfq IS NOT NULL` + `<= max(ods)`）逻辑正确，不改。

## 实施步骤（TDD）

### Step 1 — RED：改正测试为内部缺口
`tests/test_etl/test_build_dwd.py::test_suspension_detection`
- ods 插入 0101、0103（缺 0102）；trade_cal 三天。
- 断言：0102 被填，is_suspended=1，vol=0，close_qfq=prev(0101) close。
- 先运行确认失败（当前代码不填内部缺口）。

### Step 2 — GREEN：修 gap 检测
`backend/etl/build_dwd.py` Step 2：
- gap 检测改为「每股 ODS 实际行数 < 其 [min,max] 区间内交易日历期望天数」：
```sql
SELECT o.ts_code
FROM (SELECT ts_code, COUNT(*) AS n, MIN(trade_date) mn, MAX(trade_date) mx
      FROM ods_daily GROUP BY ts_code) o
JOIN dim_date dd ON dd.is_trade_day = 1
                AND dd.trade_date >= o.mn AND dd.trade_date <= o.mx
GROUP BY o.ts_code, o.n
HAVING o.n < COUNT(dd.trade_date)
```
- 门控简化为 `if ts_code not in gap_stocks: continue`（移除恒真的 `len(gap_stocks) < len(codes_to_fill)` 冗余分支）。
- 运行确认转绿 + 不回归（`test_qfq_formula` 无缺口仍 2 行）。

### Step 3 — 全量测试
`pytest tests/ -v` 全绿（目标 0 failed）。

## 文档
- `CLAUDE.md` 注意事项更新：停牌填充触发条件（按交易日历检测内部缺口）。

## 待请示（不在本批执行）
- 生产库 `data/tradeanalysis.duckdb`（3.1G）是否做一次干净全量重建，让停牌行落库。
- 导出 `export_wide_to_excel` 空周线 `KeyError: 'ts_code'` 健壮性小修。
