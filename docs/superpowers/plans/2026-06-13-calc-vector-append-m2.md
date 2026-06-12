# M2 Vector Append Implementation Plan

> **For agentic workers:** Subagent-Driven，每波 PR 独立；**禁止与 M1 同 PR**。

**Goal:** 将真新日 `batch_compute`（MACD 806s + 量能 944s + DDE 746s）压到 SLA 可接受区间；M1+M2 E2E 目标 **15–20min**（含 export）。

**实库基线（run_id `0fd66428`，20260611）：** E2E 68min；`batch_compute` 2496s；路由 `chunk=0`。

**父计划：** `2026-06-09-calc-fundamental-performance.md` 附录 M2

**验收合同：**

| 级别 | 内容 | 门槛 |
|------|------|------|
| L1 | `pytest tests/ -v` | 全绿 |
| L2 | `test_append_calc.py` | `atol=1e-9` |
| L3 | `test_divergence_structure.py` + golden CSV | 离散列完全相等 |
| M2-KPI | `batch_compute` 分项 | MACD+DDE+Volume 合计 **<1200s**（实库签字） |

**禁止：** 关 `DWD_REBUILD_REFRESH_STATE`；缩 `SIG_WINDOW`；`vector/` 内复制 `b4_macd.py`。

---

## 波次

| 波次 | 内容 | 文件 |
|------|------|------|
| **M2b** | 结构背离 cross 索引 O(n) + `target_indices` 输出裁剪 | `divergence_structure.py` |
| **M2a** | MACD/DDE 跨股 EMA 矩阵递推 | `backend/etl/vector/macd_batch.py`、`dde_batch.py` |
| **M2a-vol** | Volume `pct_rank` 批算 | `vector/volume_batch.py` |
| **M2c** | `volume_trend_v2` profiling | spike only，不阻塞发布 |
| **M2d** | MACD weekly B4 `target_indices`（`b4_weekly_series_from_daily`） | `b4_macd.py` + `calc_macd.py` + `calc_batch_append.py`；计划 `2026-06-13-calc-macd-b4-weekly-m2d.md` |

---

### Task M2b-1: Cross 索引 O(n) 预计算

**Files:** `backend/etl/divergence_structure.py`, `tests/test_etl/test_divergence_structure.py`

- [x] `_cross_index_arrays(fast, slow)` → `(last_gc, last_dc)` 长度 n
- [x] `compute_macd_structure_divergence` / `compute_dde_structure_divergence` 替换 `_last_cross_index` 调用
- [x] 可选 `target_indices: Optional[Set[int]]` — 全窗算状态，仅对目标 index 写 `result[i]`
- [x] 既有 golden + pytest 全绿

### Task M2a-1: MACD 跨股 EMA 向量化

**Files:** `backend/etl/vector/macd_batch.py`, `backend/config.py`, `calc_batch_append.py`, `tests/test_etl/test_vector_append.py`

- [x] `ema_seeded_matrix(values, period, seeds)` — `(n_stocks, n_bars)` 批递推
- [x] `batch_macd_ema_core(ts_codes, quote_groups, seeds_by_code)` → ema12/26/dea/dif/bar
- [x] `CALC_VECTOR_APPEND=1` 开关；`batch_append_macd` 热路径调用
- [x] golden：`test_vector_append.py` vs 逐股 oracle，`atol=1e-9`

### Task M2a-2: DDE ddx2 EMA 批算

- [x] `vector/dde_batch.py` + `batch_append_dde` 热路径
- [x] golden：`test_batch_ddx_ddx2_core_matches_compute_dde_core` + `test_batch_append_dde_matches_per_stock_append`

### Task M2a-vol: Volume pct_rank 批算

- [x] `vector/volume_batch.py` + `batch_append_volume` 热路径
- [x] golden：`test_batch_volume_rolling_core_matches_compute_volume_core` + batch append 等价

### Task M2c: `volume_trend_v2` profiling（spike）

**Files:** `scripts/profile_volume_trend_v2.py`

- [x] 微基准：500 股 × 245 bar（外推 5389 股）
- [x] cProfile：确认热点在 `compute_volume_trend_series` → `volume_trend_v2`
- [x] last-bar-only spike：等价性 50/50 通过
- [x] **M2c+ 已落地**：APPEND `target_indices` 仅算 new_bars（~184× trend 段）

**实库外推（500 股实测 × 5389/500，2026-06-13 复跑）：**

| 组件 | 500 股实测 | 5389 股外推 | 占 volume derived |
|------|-----------|------------|-------------------|
| `trend_v2` expanding | 66.6s | **~718s (~12min)** | **98.7%** |
| `trend_v2` last-bar only | 0.36s | **~3.9s** | — |
| vector rolling core | 0.07s | ~0.8s | — |
| zone+strength+divergence | 0.86s | ~9.3s | 1.3% |

**APPEND 优化收益（相对 0fd66428 量能 944s）：** last-bar-only → 外推 volume batch **~12s**（**~184×** trend 段；全 derived **~13s**）。

**根因：** `compute_volume_trend_series` 对每根 bar 调用 `volume_trend_v2(vol[:i+1])` → 每股 **O(n²)**（245 bar ≈ 186 次 × 递增长窗）。

**cProfile（200 股）：** 37200 次 `volume_trend_v2`；热点 `np.percentile` + `pandas rolling.mean`。

**建议（M2c+，✅ 已落地）：** APPEND 仅对 `new_bars` 索引调用 `volume_trend_v2`（`compute_volume_trend_series(..., target_indices=...)`）；FULL 路径保持全序列。

```bash
python3 scripts/profile_volume_trend_v2.py --stocks 500 --bars 245
python3 scripts/profile_volume_trend_v2.py --stocks 200 --cprofile
```

---

## Execution Handoff

**M2a/M2b/M2c+：** ✅ 已落地  
**M2c+ remediation：** ✅ `require_trend_target_indices` fail-fast + golden 补齐  
**下一项：** 实库 M1+M2+M2c+ benchmark 签字
