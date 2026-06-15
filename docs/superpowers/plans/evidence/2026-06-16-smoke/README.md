# Post-merge acceptance smoke evidence

**Plan:** `docs/superpowers/plans/2026-06-16-post-merge-acceptance-gates.md` (P1-3)  
**Date:** 2026-06-15  
**ANALYSIS_DATE:** 20260612  
**DB:** `data/tradeanalysis.duckdb`

## 归档文件

| 文件 | 模式 | 耗时 |
|------|------|------|
| `smoke-readonly.txt` | 只读 + dry-run + health_check | ~96s |
| `smoke-run-all.txt` | `--run-all`（§1 pipeline_shortcut + §4 refresh ma） | ~230s |
| `smoke-run-wave5.txt` | `--run-wave5`（circ_mv→dde narrow） | ~339s |
| `smoke-run-force.txt` | `run --force` 穿透 L0 shortcut | ~8s |
| `smoke-ods-close-chain.txt` | §2 ODS close 变更连锁 | ~426s |
| `smoke-refresh-spec-ma.txt` | §3 `calc --refresh-spec ma`（mock v1→v2） | ~7s |
| `smoke-force-hard.txt` | `CALC_FORCE_HARD=1 run --force`（000001.SZ） | ~8s |

## 结果摘要

### ✅ 通过项

| # | 检查 | 证据 |
|---|------|------|
| 1 | 同 day 二次 run → `pipeline_shortcut=true` | run-all §1：`ods_rows_written=0`，`Skipped calc (pipeline shortcut)` |
| **P0-1** | `run --force` 穿透 L0 | 进入 `Step 2/3`（非 pipeline shortcut）；L1 `force_same_day_skip` 符合设计 |
| **§2** | ODS close 变更连锁 | `ods_rows_written=1`，`run_rebuild_dwd skipped=false`，`changed_field_events_count=1` |
| **§3** | `calc --refresh-spec ma` | `calc_refresh_spec` log；`full_by_indicator={ma_daily:1, ma_weekly:1}`；state v1→v2 |
| **L1 HARD** | `CALC_FORCE_HARD=1 run --force` | 无 `force_same_day_skip`；`calc_dws` 992 rows（000001.SZ 子集） |
| 4 | `refresh --indicator ma` | run-all §4：`dws_ma_daily=1335726`，`dws_ma_weekly=1255163` |
| 5–6 | refresh dry-run 12 路由 | 5524 stocks × 12 routes = 65654 est |
| 7 | Wave5 narrow | `run_indicator_filter=["dde"]`，`calc_routes_narrowed=true`，无 `macd_*` in full_by_indicator |
| 8 | ODS/DWD/DWS 不变量 | health_check A–I、K 全 PASS |

### ⚠️ 已知 FAIL（与本次验收无关）

- **health_check Section J：** `ma alignment bull_strong+s5flat+s10up: 1` — MA alignment fallback WIP（`test_calc_ma` 同源），非 change-driven refresh 回归。

### ⬜ 未在本 smoke 覆盖

- **全市场 `CALC_FORCE_HARD=1 run --force`**（运维例外，墙钟 ~15min+；子集 000001.SZ 已验 L1 硬重算路径）

## 复现命令

```bash
export ANALYSIS_DATE=20260612
export DUCKDB_PATH=data/tradeanalysis.duckdb
./scripts/smoke_change_driven_refresh.sh
./scripts/smoke_change_driven_refresh.sh --run-all
./scripts/smoke_change_driven_refresh.sh --run-wave5
python3 -m backend.cli run --date 20260612 --force --skip-export   # P0-1 L0
python3 -m backend.cli calc --date 20260612 --refresh-spec ma --ts-code 000001.SZ  # §3（需先 mock v1 stale）
CALC_FORCE_HARD=1 python3 -m backend.cli run --date 20260612 --force --skip-export --ts-code 000001.SZ
```

**状态：** ✅ 实库 smoke 全套完成（Section J MA 1 FAIL 为 WIP，与 change-driven refresh 无关）
