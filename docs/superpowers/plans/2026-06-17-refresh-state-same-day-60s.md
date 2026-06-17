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

## 验收

| 级别 | 门槛 |
|------|------|
| L1 | `pytest tests/test_etl/test_calc_state_refresh.py -v` |
| L2 | 同日复跑 `--skip-export` ≤15s（L0） |
| R2 | `refresh-state --date X` 墙钟 ≥40% 降幅（5389 股） |
