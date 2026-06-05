# ADS View Anchor DWD — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `v_ads_analysis_wide_daily` 和 `v_ads_analysis_wide_weekly` 的 FROM 主表从 MACD 下沉到 DWD，消除"MACD 缺失 → 整只股票消失"的脆性依赖。

**Architecture:** 视图锚点从 DWS 层（`v_dws_macd_latest`）下沉到 DWD 层（`dwd_daily_quote` / `dwd_weekly_quote`），所有 DWS 表改为 LEFT JOIN。DWD 在 ETL 流程中先于 DWS 构建，数据最完整。

**Tech Stack:** DuckDB SQL (CREATE OR REPLACE VIEW)

---

## File Structure

| 文件 | 操作 | 职责 |
|------|------|------|
| `backend/db/schema.py` | 修改 | 替换 2 视图的 FROM/JOIN 顺序 |
| `backend/export_wide.py` | 不变 | 零改动 |
| 其他 | 不变 | 无 API 消费者 |

---

### Task 1: 修改 `v_ads_analysis_wide_daily` 视图 SQL

**Files:**
- Modify: `backend/db/schema.py:544-640`

- [ ] **Step 1: 定位要替换的 SQL 块**

当前代码（544-640 行）WHERE 之前的 FROM/JOIN 部分：

```sql
FROM v_dws_macd_daily_latest c
LEFT JOIN dim_stock s              ON c.ts_code = s.ts_code
LEFT JOIN v_dws_kpattern_daily_latest k ON c.ts_code = k.ts_code AND c.trade_date = k.trade_date
LEFT JOIN v_dws_ma_daily_latest      a ON c.ts_code = a.ts_code AND c.trade_date = a.trade_date
LEFT JOIN v_dws_dde_daily_latest     d ON c.ts_code = d.ts_code AND c.trade_date = d.trade_date
LEFT JOIN v_dws_volume_daily_latest  v ON c.ts_code = v.ts_code AND c.trade_date = v.trade_date
LEFT JOIN v_dws_price_position_daily_latest pp ON c.ts_code = pp.ts_code AND c.trade_date = pp.trade_date
LEFT JOIN dwd_daily_quote            q ON c.ts_code = q.ts_code AND c.trade_date = q.trade_date
```

替换为：

```sql
FROM dwd_daily_quote q
LEFT JOIN dim_stock s                  ON q.ts_code = s.ts_code
LEFT JOIN v_dws_macd_daily_latest           c ON q.ts_code = c.ts_code AND q.trade_date = c.trade_date
LEFT JOIN v_dws_kpattern_daily_latest      k ON q.ts_code = k.ts_code AND q.trade_date = k.trade_date
LEFT JOIN v_dws_ma_daily_latest            a ON q.ts_code = a.ts_code AND q.trade_date = a.trade_date
LEFT JOIN v_dws_dde_daily_latest           d ON q.ts_code = d.ts_code AND q.trade_date = d.trade_date
LEFT JOIN v_dws_volume_daily_latest        v ON q.ts_code = v.ts_code AND q.trade_date = v.trade_date
LEFT JOIN v_dws_price_position_daily_latest pp ON q.ts_code = pp.ts_code AND q.trade_date = pp.trade_date
```

注意：SELECT 子句中所有引用不变（`c.ema_12`, `k.strength` 等），只改 FROM/JOIN。

- [ ] **Step 2: 执行 Edit 替换**

在 `backend/db/schema.py` 中执行 exact string replacement：
- `old_string`: 原始 FROM ... LEFT JOIN dwd_daily_quote 块
- `new_string`: 新的 FROM ... LEFT JOIN pp 块

- [ ] **Step 3: 重建视图并验证**

```bash
python3 -c "
import duckdb
con = duckdb.connect('data/tradeanalysis.duckdb')

# 手动执行新 SQL 验证
con.execute('''CREATE OR REPLACE VIEW v_ads_analysis_wide_daily AS
SELECT ... FROM dwd_daily_quote q LEFT JOIN ... ''')

# 验证：6 只标的在 20260603 应该都有数据
rows = con.execute(\"SELECT COUNT(*) FROM v_ads_analysis_wide_daily WHERE trade_date = '20260603'\").fetchone()
print(f'20260603: {rows[0]} stocks (expected 6)')

rows = con.execute(\"SELECT COUNT(*) FROM v_ads_analysis_wide_daily WHERE trade_date = '20260604'\").fetchone()
print(f'20260604: {rows[0]} stocks (expected 6)')

con.close()
"
```

预期输出：
```
20260603: 6 stocks (expected 6)
20260604: 6 stocks (expected 6)
```

- [ ] **Step 4: Commit**

```bash
git add backend/db/schema.py
git commit -m "fix: v_ads_analysis_wide_daily anchor from MACD to DWD(to prevent missing stocks on partial DWS data)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 修改 `v_ads_analysis_wide_weekly` 视图 SQL

**Files:**
- Modify: `backend/db/schema.py:733-740`

- [ ] **Step 1: 定位要替换的 SQL 块**

当前代码（733-740 行）的 FROM/JOIN 部分：

```sql
FROM v_dws_macd_weekly_latest cw
LEFT JOIN dim_stock s                  ON cw.ts_code = s.ts_code
LEFT JOIN v_dws_kpattern_weekly_latest kw ON cw.ts_code = kw.ts_code AND cw.trade_date = kw.trade_date
LEFT JOIN v_dws_ma_weekly_latest      aw ON cw.ts_code = aw.ts_code AND cw.trade_date = aw.trade_date
LEFT JOIN v_dws_dde_weekly_latest     dw ON cw.ts_code = dw.ts_code AND cw.trade_date = dw.trade_date
LEFT JOIN v_dws_volume_weekly_latest  vw ON cw.ts_code = vw.ts_code AND cw.trade_date = vw.trade_date
LEFT JOIN v_dws_price_position_weekly_latest ppw ON cw.ts_code = ppw.ts_code AND cw.trade_date = ppw.trade_date
LEFT JOIN dwd_weekly_quote            qw ON cw.ts_code = qw.ts_code AND cw.trade_date = qw.trade_date
```

替换为：

```sql
FROM dwd_weekly_quote qw
LEFT JOIN dim_stock s                      ON qw.ts_code = s.ts_code
LEFT JOIN v_dws_macd_weekly_latest           cw ON qw.ts_code = cw.ts_code AND qw.trade_date = cw.trade_date
LEFT JOIN v_dws_kpattern_weekly_latest      kw ON qw.ts_code = kw.ts_code AND qw.trade_date = kw.trade_date
LEFT JOIN v_dws_ma_weekly_latest            aw ON qw.ts_code = aw.ts_code AND qw.trade_date = aw.trade_date
LEFT JOIN v_dws_dde_weekly_latest           dw ON qw.ts_code = dw.ts_code AND qw.trade_date = dw.trade_date
LEFT JOIN v_dws_volume_weekly_latest        vw ON qw.ts_code = vw.ts_code AND qw.trade_date = vw.trade_date
LEFT JOIN v_dws_price_position_weekly_latest ppw ON qw.ts_code = ppw.ts_code AND qw.trade_date = ppw.trade_date
```

注意：SELECT 子句中所有引用不变（`cw.ema_12`, `kw.strength` 等），只改 FROM/JOIN。

- [ ] **Step 2: 执行 Edit 替换**

- [ ] **Step 3: 重建视图并验证**

```bash
python3 -c "
import duckdb
con = duckdb.connect('data/tradeanalysis.duckdb')

con.execute('''CREATE OR REPLACE VIEW v_ads_analysis_wide_weekly AS
SELECT ... FROM dwd_weekly_quote qw LEFT JOIN ... ''')

rows = con.execute(\"SELECT COUNT(*) FROM v_ads_analysis_wide_weekly WHERE trade_date = '20260529'\").fetchone()
print(f'20260529: {rows[0]} stocks (expected 6)')

con.close()
"
```

预期输出：
```
20260529: 6 stocks (expected 6)
```

- [ ] **Step 4: Commit**

```bash
git add backend/db/schema.py
git commit -m "fix: v_ads_analysis_wide_weekly anchor from MACD to DWD (to match daily)"

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 端到端验证

- [ ] **Step 1: 跑全量测试**

```bash
pytest tests/ -v
```

确认：192 passed, 5 预存失败（与本次无关）

- [ ] **Step 2: 跑一次完整导出验证**

```bash
python3 fetch_stocks.py --no-export  # 只跑 ETL，不导出
```

运行完成后检查：
```bash
python3 -c "
import duckdb
con = duckdb.connect('data/tradeanalysis.duckdb')
# 验证日线视图
r = con.execute(\"SELECT COUNT(DISTINCT ts_code) FROM v_ads_analysis_wide_daily WHERE trade_date = (SELECT MAX(trade_date) FROM v_ads_analysis_wide_daily)\").fetchone()
print(f'Daily latest: {r[0]} distinct stocks')
# 验证周线视图
r = con.execute(\"SELECT COUNT(DISTINCT ts_code) FROM v_ads_analysis_wide_weekly WHERE trade_date = (SELECT MAX(trade_date) FROM v_ads_analysis_wide_weekly)\").fetchone()
print(f'Weekly latest: {r[0]} distinct stocks')
con.close()
"
```

- [ ] **Step 3: Commit**

```bash
git commit -m "test: end-to-end verification — views rebuild correctly with DWD anchor" --allow-empty
```

---

### Task 4: 文档更新

- [ ] **Step 1: Update CLAUDE.md**

在 CLAUDE.md 的 "已知问题和注意事项" 或 "关键技术细节" 部分添加：

```markdown
- **ADS 视图以 DWD 为锚点：** `v_ads_analysis_wide_daily/weekly` 的 FROM 主表为 `dwd_daily_quote`/`dwd_weekly_quote`，所有 DWS 表 LEFT JOIN。某类指标缺失不影响股票在导出中的可见性。
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md — ADS views anchored at DWD layer"

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
