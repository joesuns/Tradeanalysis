# 数据准确性加固（第一批）— ODS 校验门禁 + adj_factor 护栏

**日期：** 2026-06-05
**范围：** 仅 Fix 1（接入 `_validate_ods_batch`）+ Fix 2（adj_factor 缺失护栏）
**不在本批：** Fix 3 周线 ISO 周改造（独立评估，因需全量重建周线层）

## 背景与证据

审计实测当前库（`data/tradeanalysis.duckdb`，2026-06-05）：

| 检查项 | 实测 |
|---|---|
| `ods_daily` adj_factor NULL | 0 |
| `ods_daily` OHLC NULL | 0 |
| `ods_daily` high<low | 0 |
| `dwd_daily_quote` close_qfq NULL/≤0 | 0 |

结论：本批为**预防性加固**——当前数据干净，但写库路径无任何护栏，tushare 偶发异常批次会静默入库污染全链路。

## 问题与受影响文件

### Fix 1：`_validate_ods_batch` 未接入
- `backend/fetch/ods_daily.py:192` — 函数已实现但**仅返回计数、不过滤**，且无任何调用方。
- 三条 `daily` 写库路径均未校验：
  - `fetch_by_date_range`（`ods_daily.py:52`，单线程 date-batched）
  - `fetch_by_date_range_parallel._fetch_chunk`（`ods_daily.py:454`，多线程主路径）
  - `fetch_stocks_incremental`（`ods_daily.py:322`，逐股增量）

### Fix 2：adj_factor 缺失静默产 NULL 价
- fetch 层 `adj_map.get(...)`（`ods_daily.py:58/459/325`）→ 缺失即 NULL。
- DWD 层 `build_dwd.py:43-60` `d.adj_factor / la.latest_adj`：`latest_adj` 为 NULL/0 时静默产 NULL/除零，且无告警。

## 实施步骤

### Step 1 — 改造 `_validate_ods_batch` 为「过滤器」
`backend/fetch/ods_daily.py`
- 签名改为返回 `(valid_recs: list[dict], invalid_count: int)`（原为 `(valid_count, invalid_count)`）。
- 校验逻辑不变：必需字段 `open/high/low/close/vol/amount` 非空 + `high >= low`。
- 仅用于 `daily` 接口记录（OHLCV）；`daily_basic`/`moneyflow` 无 OHLC，不校验。

### Step 2 — 三条路径接入过滤
在每条路径 `recs = client.call("daily", ...)` 之后立即：
```python
recs, n_invalid = _validate_ods_batch(recs, "daily", trade_date)
if n_invalid:
    logger.warning("ODS daily %s: dropped %d invalid rows", trade_date, n_invalid)
```
再用过滤后的 `recs` 构建 `daily_data`。
- Path A `fetch_by_date_range`：line 52 之后。
- Path B `_fetch_chunk`：line 454 之后（多线程，logger 线程安全）。
- Path C `fetch_stocks_incremental`：line 322 之后，`trade_date` 用 `f"{seg_start}~{seg_end}"`。

### Step 3 — DWD 层 adj_factor 护栏
`backend/etl/build_dwd.py` `build_dwd_daily_quote`
- Step 1 INSERT 的 `WHERE 1=1 {code_filter}` 追加：
  `AND d.adj_factor IS NOT NULL AND la.latest_adj IS NOT NULL AND la.latest_adj <> 0`
- INSERT 前跑诊断 COUNT，统计被排除行数（adj_factor NULL 或 latest_adj NULL/0），>0 时 `logger.warning` 打印行数 + 样本 ts_code（让"某股 qfq 不可用"可见）。

## 测试

`tests/test_fetch/test_ods_daily.py`
- 更新现有 3 个 `_validate_ods_batch` 测试（150-196）适配新返回 `(list, int)`：断言改用 `len(valid_recs)`。
- 新增：含 1 条 high<low + 1 条 OHLC NULL 的 batch → 返回有效列表剔除这 2 行，`invalid_count==2`。

`tests/test_etl/test_build_dwd.py`
- 新增：构造 1 条 adj_factor=NULL 的 ods_daily 行 → `build_dwd_daily_quote` 后该行不进 DWD，且无 NULL close_qfq。

`pytest tests/ -v` 全绿。

## 文档更新
- `CLAUDE.md:212`：把「`_validate_ods_batch` 在 ODS INSERT 前校验」改为准确描述（已接入 daily 路径，拒绝行 WARN 日志）。
- `CLAUDE.md` 注意事项新增：DWD 构建对 adj_factor 缺失行的排除 + 告警行为。

## 不做 / 风险
- 不改周线口径（Fix 3 另议）。
- 不改 `daily_basic`/`moneyflow` 校验（无 OHLC，超范围）。
- adj_factor「以最新交易日为基准、分红后全历史漂移」是**设计正确**行为，不修。
- 爆炸半径：仅 fetch + DWD 构建；不触发 DWS 重算；现有数据无脏行，行为对历史无影响。
