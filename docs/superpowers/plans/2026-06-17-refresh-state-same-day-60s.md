# refresh_state 同日复跑 ≤60s 优化

**日期：** 2026-06-17  
**状态：** 完成（R1/R2/R2b 已落地，2026-06-17）  
**KPI：** 稳态第二次 `run --date X --skip-export` ≤60s（辅助 KPI）

---

## 根因

| 路径 | refresh_state | 墙钟 |
|------|---------------|------|
| L0 pipeline_shortcut | 不跑 | ~2–15s（仅 fetch） |
| calc 全路径 | ~120–305s | ~256s+ |

≤60s 主杠杆是 **L0 稳定触发**；refresh_state 优化降真新日 ~305s 与 DWD rebuild 过渡路径。

---

## 包 R1 — L0 门禁可观测

- `compute_skip_dwd_calc` 打 INFO 级 gate 失败原因
- 实库诊断：`run --date 20260617 --skip-export` 第二次

## 包 R2 — refresh_state 性能（已落地 v1）

1. **`return_artifacts=False` 跳过 post-preflight**（CLI `refresh-state` 不再白算 modes）
2. **upsert 后内存 patch `state_map`**，去掉 `load_calc_state_batch` 重载
3. **并行 tail SQL** — 暂缓（DuckDB 写连接持有期间无法开第二连接；见 R2b）

### R2b（✅ 2026-06-17）

- `cli refresh-state` 分相：只读 setup → 释放写锁 → **isolated 并行 tail load** → 短写连接 upsert/checkpoint
- `REFRESH_STATE_PARALLEL=1`（默认）：5 路 read_only 连接并行 load_state + 4 tails
- `run` 热路径仍 `tail_load=inline`（写连接已持有，无法并行）

**实库 dry-run @ 20260617（5524 股）：** 墙钟 **229.4s**（`tail_load=isolated`，无 upsert/preflight）；指纹扫描 ~170s

## R1 诊断（2026-06-17）

第一次 `run --date 20260617 --skip-export`：

| 项 | 值 |
|----|-----|
| L0 失败原因 | `fetch_rows_written=10693`（非 spec_stale） |
| refresh_state | 454.8s（5110 keys updated，含 preflight） |

**结论：** 稳态同日复跑须 **fetch=0** 才触发 L0；refresh_state 优化不替代 fetch 幂等。

## 验收（✅ 2026-06-17）

| 级别 | 门槛 | 实测 | 判定 |
|------|------|------|------|
| L1 | `pytest tests/test_etl/test_calc_state_refresh.py -v` | **9/9 PASS** | ✅ |
| L2 | 稳态同日复跑 `run --skip-export` ≤15s（L0） | **7.6s** @ 20260617（fetch=0，`pipeline_shortcut`） | ✅ |
| R2b load | isolated 并行 tail load | **59.4s**（5524 股，5 路 read_only） | ✅ |
| R2b dry-run | `cli refresh-state --dry-run` | **229.4s**（无 upsert/preflight；指纹扫描 ~170s） | ✅ |
| R2 降幅 | refresh-state 墙钟 ≥40% 降幅 | load 59s vs 原串行 ~80s+；CLI 跳过 preflight；**全量 dry-run 未达 40%**（扫描主导） | ⚠️ 部分 |

**Commit：** `e8983b2` — `perf(refresh-state): L0 gate logs + isolated parallel tail load`

**Plan status：** **签字完成**（2026-06-17）。同日复跑 ≤60s 辅助 KPI 由 **L0** 满足；refresh_state 真新日路径仍见 pipeline 附录 F。
