# Fetch 停牌缺口跳过（交易区间收口）

- **日期：** 2026-06-08
- **状态：** 已完成
- **类型：** 性能修复（fetch 空转）
- **关联诊断：** `scripts/diag_weekly_fetch.py`（只读，本次新建）

## 一、问题与根因（已实测确认）

`calc --date 20260602` 实库观测：auto-fetch 宽桶 `[20240202~20260602]` 对 **538 股**跑了 **324s，写入 0 行**。

只读诊断拆桶（539 tdays 窗口）：

| 桶 | 数量 | 占比 |
|---|---|---|
| 无缺口 | 0 | 0% |
| **仅内部缺口（停牌型，拉了拿空）** | **527** | **98%** |
| 有尾部缺口（ODS_max 停在 4 月底，长期停牌） | 11 | 2% |

被判「缺失日」共 **4235 个**，全部 fetch 拿空。

**根因：** `_get_missing_days_for_stock` 用「日历交易日 − 该股 ODS 已有日」做减法。**停牌日是开市交易日但该股无 ODS 行**，于是被永久判为「缺失、可拉取」，每次 calc 都对这些段重发 `adj_factor/daily/daily_basic/moneyflow` 四个 API，tushare 对停牌日返回空 → 0 行 → 下次再来一遍（自我永动空转）。

铁证样本 `000016.SZ`：ODS 已覆盖到 `20260605`（比 calc_date 还新，根本不落后），却被判出 11 个缺失日 `20241230~20250113`——一段连续约两周的停牌，`dim_date` 全是交易日。

**关键论证（为何跳过零损失）：** 若某内部缺口是「可拉取的真数据」，它在历史上某次 fetch 覆盖该区间时**早就被拉到了**。既然每次重拉都返回 0 行，说明源端确实无数据，重拉**可证明无用**。故跳过内部缺口不丢任何可得数据。内部缺口本就由 DWD 层的停牌填充（`build_dwd` `Suspension fill`）处理，ODS 保持稀疏是既定设计。

## 二、方案（方案 1：交易区间收口）

把每股缺失日按其 **ODS 实际交易区间 `[first_ods, last_ods]`** 分三类，只拉两端、丢内部：

- **Head 缺口** `d < first_ods`：可拉取的历史（如 warmup 窗口前移）→ **保留**
- **Internal 缺口** `first_ods ≤ d ≤ last_ods` 且无 ODS 行：停牌 → **丢弃**
- **Tail 缺口** `d > last_ods`：近端新数据 / 停牌复牌探测 → **保留**

无任何 ODS 行的股（首次拉取）→ 全部保留（range 拉取，源端给什么收什么）。

**预期效果：** 527 只内部缺口股 → 0 ranges → `continue`（仅一次本地 SQL，无 API）；11 只尾部停牌股 → 仍探测尾部（必要，用于发现复牌），代价极小。324s/0 行 → 数秒。

**为何不动门禁口径：** 门禁（`week_end_bars` vs `available_we`）只是触发器，把含停牌周末的股选进 `weekly_fetch`。即使修门禁，fetch 层对停牌的误判仍在（其它入口同样空转）。治本点在 fetch 层。门禁口径优化留作后续独立项（可选）。

## 三、实施步骤（TDD）

### 1. 新增内部缺口过滤 helper（`backend/fetch/ods_daily.py`）

```python
def _drop_suspension_gaps(con, ts_code, missing_days):
    """丢弃落在该股交易区间 [first_ods, last_ods] 内部的缺失日（停牌，
    tushare 不返回）。保留 head(<first_ods) 与 tail(>last_ods) 缺口。
    无 ODS 行 → 首次拉取，全部保留。"""
    if not missing_days:
        return missing_days
    row = con.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM ods_daily WHERE ts_code = ?",
        (ts_code,)
    ).fetchone()
    first_ods = row[0] if row else None
    last_ods = row[1] if row else None
    if not first_ods or not last_ods:
        return missing_days
    return [d for d in missing_days if d < first_ods or d > last_ods]
```

### 2. 接入 `_get_missing_ranges_per_stock`

在 `missing = _get_missing_days_for_stock(...)` 之后、合并 range 之前插入：
```python
missing = _drop_suspension_gaps(con, ts_code, missing)
if not missing:
    return []
```
（`_get_missing_days_for_stock` 本身**不改**，保持「窗口内日历缺失日」纯语义，过滤只在 ranges 层做，便于单测与复用。）

### 3. 测试（`tests/test_fetch/test_ods_daily.py` 新增）

- `test_drop_suspension_gaps_internal_dropped`：first=01、last=05，中间缺 02/03 → 返回空。
- `test_drop_suspension_gaps_keeps_head_and_tail`：first=03、last=05，缺 01/02(head) 与 06/07(tail) → 全保留。
- `test_drop_suspension_gaps_no_ods_keeps_all`：该股无 ODS 行 → 原样返回。
- `test_get_missing_ranges_skips_internal_suspension`：端到端，ODS 有 01 与 05、缺 02/03/04（内部）→ ranges 为空。
- `test_get_missing_ranges_tail_still_fetched`：ODS 至 04、窗口到 06 → ranges=[(05,06)]。
- 回归：现有 `test_get_missing_days_for_stock` / `test_get_missing_ranges_merges_consecutive`（head 场景）/ `_no_gap` / `_skips_when_complete` 应仍通过。

### 4. 实库验收（手动）

`calc --date <新交易日>` 观察 auto-fetch 宽桶日志：538 股桶应从 ~324s 降到数秒，仍 0 行（语义不变，只是不再空转）；calc 结果行数与修复前一致。

### 5. 文档更新

- `CLAUDE.md`：「停牌填充」「Fetch 覆盖率」相关条目补充：per-stock 增量缺口判定**仅拉 head/tail，跳过交易区间内部停牌缺口**（避免反复空转）。
- 本 plan 状态改「已完成」。

## 四、风险与边界

- **长期停牌股（11 只尾部）**：仍每次探测尾部 → 少量 API。这是发现复牌的必要代价，可接受；后续可加「停牌记忆」进一步省掉（本期不做）。
- **Head 缺口扩张**：warmup 窗口前移时 head 缺口会被拉取（正确行为，真历史）。若该 head 段也是 IPO 初期停牌，会拉一次空——但有界、罕见、且拉一次后区间收口即不再重试。
- **date-batched 并行路径不受影响**：本改动只在 `fetch_stocks_incremental → _get_missing_ranges_per_stock`，date-global skip 逻辑（`_get_trading_days`）不碰。
- **首次拉取不受影响**：无 ODS 行 → 全保留。

## 五、不做（本期范围外）

- 门禁口径（`week_end_bars`/`available_we`）重构。
- 停牌日持久化记忆表 / `ods_stock_basic.suspend` 接入。
- DWD 层停牌填充逻辑（已有，不动）。
