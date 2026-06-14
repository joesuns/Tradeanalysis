# 日更 fetch 新鲜度修复 — 实施方案

> **Goal:** 修复 partial ODS 导致全市场 fetch 跳过、calc auto-fetch 不补 latest day、`run` 未真正 fetch 的链式缺陷。

**Root cause:** 20260605 实库 ODS 623/5524 → DWD 485 → 导出 305（去 ST）。date-global 增量误判 + completeness 仅查 warmup 行数。

---

## Task 0 — analysis_date 贯通

| 文件 | 改动 |
|------|------|
| `backend/etl/orchestrator.py` | `run_calc(calc_date=..., skip_stale_fetch=...)` |
| `backend/cli.py` | `calc --date`；`run` 传同一 date 给 calc |

## Task 1 — 全市场 fetch per-stock 增量

| 文件 | 改动 |
|------|------|
| `backend/cli.py` | `fetch_by_date_range_parallel(..., ts_codes=codes)` |
| `backend/fetch/ods_daily.py` | parallel/range 在 `ts_codes=None` 时自动 `get_all_active_codes` |

## Task 2 — stale ODS 检测 + calc 兜底

| 文件 | 改动 |
|------|------|
| `backend/etl/orchestrator.py` | `find_stale_ods_codes`、`_auto_fetch_stale_ods`、`find_stale_dwd_codes` |
| `tests/test_etl/test_orchestrator.py` | stale 检测单测 |

## Task 3 — run 编排 + 导出门禁

| 文件 | 改动 |
|------|------|
| `backend/cli.py` | `run`: fetch → rebuild DWD → calc(skip_stale) → export + WARNING |
| `CLAUDE.md` | 更新 run/calc 说明 |

## 验收标准

1. partial day 623/5524 → fetch 后该日 ODS ≥95% 活跃股
2. `run --date`：calc_date = export_date
3. 仅 `calc`（未 fetch）：stale 兜底 date-batched
4. `run` 不重复 stale fetch（skip_stale_fetch=True）
