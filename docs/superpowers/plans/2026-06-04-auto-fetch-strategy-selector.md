# Auto-Fetch 策略选择器 — date-batched vs stock-batched 分发

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `run_calc()` auto-fetch 全市场场景走单线程 stock-batched 的性能问题，增加策略选择器：大 bucket 走并行 date-batched，小 bucket 走 stock-batched。

**Architecture:** 在 `run_calc()` 的 range_buckets 循环中加入策略分发。决策依据：当 bucket 股票数 > range 内交易日数时，date-batched 的 API 调用次数更少，选 date-batched。`fetch_by_date_range_parallel` 已导入且功能完备（支持 ts_codes 过滤 + 增量跳过 + 3 线程 + 三表全覆盖）。

**Tech Stack:** Python 3.9, DuckDB WAL mode, tushare, 现有 `fetch_by_date_range_parallel` / `fetch_stocks_incremental`

---

## 文件改动清单

| 文件 | 改动类型 | 职责 |
|------|:----:|------|
| `backend/etl/orchestrator.py` | 修改 | 增加策略选择器 + `_count_trading_days` 辅助函数 |
| `tests/test_etl/test_orchestrator.py` | **新建** | 策略选择逻辑测试 |

---

### Task 1: 增加 `_count_trading_days` 辅助函数

**Files:**
- Modify: `backend/etl/orchestrator.py`

- [ ] **Step 1: 在 `_compute_fetch_range` 之后插入辅助函数**

在 `orchestrator.py` 第 298 行（`_compute_fetch_range` 函数结束后）插入：

```python


def _count_trading_days(con, start: str, end: str) -> int:
    """Return number of trading days between start and end (inclusive)."""
    row = con.execute("""
        SELECT COUNT(*) FROM dim_date
        WHERE is_trade_day = 1 AND trade_date >= ? AND trade_date <= ?
    """, (start, end)).fetchone()
    return row[0] if row else 0
```

- [ ] **Step 2: 验证导入不报错**

```bash
python3 -c "from backend.etl.orchestrator import _count_trading_days; print('OK')"
```

- [ ] **Step 3: 提交**

```bash
git add backend/etl/orchestrator.py
git commit -m "feat: add _count_trading_days helper for strategy selector"
```

---

### Task 2: 在 `run_calc` auto-fetch 循环中增加策略分发

**Files:**
- Modify: `backend/etl/orchestrator.py:438-441`

- [ ] **Step 1: 替换 auto-fetch 循环逻辑**

将 orchestrator.py 第 438-441 行：

```python
                for (seg_start, seg_end), bucket_codes in range_buckets.items():
                    try:
                        rows = fetch_stocks_incremental(
                            client, con, bucket_codes,
                            start=seg_start, end=seg_end)
```

替换为：

```python
                for (seg_start, seg_end), bucket_codes in range_buckets.items():
                    try:
                        tdays = _count_trading_days(con, seg_start, seg_end)
                        # Date-batched wins when: N_stocks > N_tdays
                        # Stock-batched: N_stocks × 4 API calls
                        # Date-batched: N_tdays × 4 API calls
                        use_date_batched = len(bucket_codes) > tdays

                        if use_date_batched:
                            logger.info(
                                "Auto-fetch bucket [%s~%s]: %d stocks, %d tdays "
                                "→ date-batched parallel mode",
                                seg_start, seg_end, len(bucket_codes), tdays,
                            )
                            rows = fetch_by_date_range_parallel(
                                seg_start, seg_end, workers=3,
                                ts_codes=bucket_codes, con=con,
                            )
                        else:
                            logger.info(
                                "Auto-fetch bucket [%s~%s]: %d stocks, %d tdays "
                                "→ stock-batched sequential mode",
                                seg_start, seg_end, len(bucket_codes), tdays,
                            )
                            rows = fetch_stocks_incremental(
                                client, con, bucket_codes,
                                start=seg_start, end=seg_end)
```

- [ ] **Step 2: 验证导入不报错**

```bash
python3 -c "from backend.etl.orchestrator import run_calc; print('OK')"
```

- [ ] **Step 3: 提交**

```bash
git add backend/etl/orchestrator.py
git commit -m "feat: add strategy selector — date-batched for large buckets, stock-batched for small"
```

---

### Task 3: 端到端验证

**Files:**
- None (验证 only)

- [ ] **Step 1: 运行 20260601 全市场 analysis（用新策略）**

先确认当前 ODS 已有 659 只股票的增量数据不会被浪费——date-batched 模式的 `_get_trading_days` 自带增量跳过。

```bash
python -m backend.cli calc --all 2>&1 | head -20
```

预期日志输出：
```
Auto-fetch bucket [20250526~20260604]: 4863 stocks, 250 tdays → date-batched parallel mode
Fetching XX trading days with 3 threads (20250526~20260604) (4863 stocks)
```

- [ ] **Step 2: 监控进度 — date-batched 应在几分钟内完成**

```bash
# 观察日志，确认 ODS fetch 在 2-5 分钟内完成，随后进入 DWD rebuild 和 DWS calc
```

- [ ] **Step 3: 验证数据完整性**

```bash
python -m backend.cli status
```

预期：DWD 和 DWS 表行数大幅增长（接近全市场股票数 × 交易日）。

- [ ] **Step 4: 导出验证**

```bash
python -m backend.cli export --date 20260601 --output exports/analysis_20260601.xlsx
```

预期：导出全市场股票分析 Excel。

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "chore: end-to-end verification — strategy selector"
```

---

### Task 4: 更新 CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 更新 auto-fetch 描述**

将 CLAUDE.md 中 calc 层的描述：

```
calc（计算层）
├── 退市股过滤
├── 前置检查 check_data_completeness()
├── 缺数据 → 无条件自动补拉（warmup=250 tdays，熔断器）
```

更新为：

```
calc（计算层）
├── 退市股过滤
├── 前置检查 check_data_completeness()
├── 缺数据 → 自动补拉（策略选择器：股票数>交易日数 → date-batched并行，否则 stock-batched串行）
├── warmup=250 tdays，熔断器：连续 5 次失败中止
```

- [ ] **Step 2: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md — auto-fetch strategy selector"
```

---

## 策略选择器设计说明

### 决策公式

```python
use_date_batched = len(bucket) > trading_days
```

- stock-batched: N_stocks × 4 API 调用
- date-batched: N_tdays × 4 API 调用
- 选 API 调用更少的一方

### API 调用次数对比

| 场景 | N_stocks | N_tdays | stock API | date API | 选择 |
|------|---------|---------|-----------|----------|------|
| 全市场补拉 | 5000 | 250 | 20,000 | 1,000 | **date** |
| 少量股票 | 50 | 250 | 200 | 1,000 | **stock** |
| 多股票短区间 | 500 | 10 | 2,000 | 40 | **date** |
| 少股票长区间 | 10 | 500 | 40 | 2,000 | **stock** |
| 边界 | 250 | 250 | 1,000 | 1,000 | **stock**（等同时 stock 更精准） |

---

## 自检

- ✅ 单一文件改动（orchestrator.py），风险可控
- ✅ `fetch_by_date_range_parallel` 已导入、已支持 ts_codes 过滤、已支持增量跳过
- ✅ 增量跳过确保已入库 ODS 数据不会重复拉取
- ✅ 纯数学决策公式，无魔数阈值
- ✅ 不影响 CLI fetch 层（fetch 层已有正确的分发逻辑）
