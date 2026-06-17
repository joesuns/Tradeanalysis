# DDE Content Invalidation after dc/circ Patch (P0)

**Date:** 2026-06-17  
**Status:** 已实施（M1–M3 @ 2026-06-17）  
**Trigger:** Anchor L2 根因调查 @ 000550.SZ — `net_amount_dc` 晚到 → calc 写错 trend → `refresh_state` 对齐 fp 但不 invalid DWS → 无新 bar SKIP

---

## Goal

`net_amount_dc` / `circ_mv` ODS 补洞后，**dde trend 内容**在 export anchor 上与当前 B4 算法一致；禁止 `refresh_state`  alone 造成 state/DWS 分叉。

## 非目标

- 全市场 `CALC_FORCE_HARD`
- 改 `dde_trend` 算法或 `SPEC_VERSION`
- 修 MA alignment audit 脚本（独立项）

---

## 根因（已证实）

```
fetch 补 net_amount_dc → DWD 变
calc（dc=NULL）已写 DWS trend=down
refresh_state 更新 history_fp（不重算 DWS）
同 bar 无新 bar → dde SKIP
oracle: stored ≠ B4 recompute
```

Wave 5 已有 `column_indicator_deps`: `ods_moneyflow.*` / `circ_mv` → `dde`，但 **refresh_state 后 calc 仍可能 SKIP**。

---

## 方案 P0-a（推荐）

**触发：** `run` 路径 ODS column patch 事件含 `net_amount_dc` 或 `circ_mv`，且 DWD rebuild 实际写入。

**动作（refresh_state 之后、calc 之前）：**

1. 解析受影响 `(ts_code, trade_date)` 子集（来自 `FetchResult.changed_field_events`）
2. 对子集调用 `invalidate_dde_daily_snapshots(con, calc_date, ts_codes=...)` **仅 calc_date 批次**（已有 API）
3. 对子集 `invalidate_dde_daily_calc_state` 或强制 dde 路由 FULL（窄窗）

**不改：** quote 指标、全库 rebuild。

### 触及模块

| 文件 | 改动 |
|------|------|
| `backend/etl/orchestrator.py` | run Step1 后 merge patch 子集 → dde invalidation |
| `backend/etl/backfill_dde_recalc.py` | 可选：`invalidate_dde_for_column_patch(con, calc_date, events)` |
| `backend/etl/column_indicator_deps.py` | 导出 `dde_patch_events(events)`  helper |
| `tests/test_etl/` | 集成：patch 事件 → invalidation 调用；SKIP 后 trend 更新 |

### 验收

- 单测：mock patch 事件 → dde state 删除 + 下一 calc 写新 trend
- 实库：000550 类场景 replay（dc NULL → calc → dc 补 → run → trend 更新）
- `audit_dde_trend_oracle` full population @ anchor mismatch=0

---

## 方案 P0-b（备选，更小 diff）

`refresh_calc_state_fingerprints` 后：对 dde SIGNATURE_COLS，若新 `state_signature` ≠ 写入 DWS 时 fp，打 `dde_content_stale` 标志，`classify_calc_mode` 对 dde 强制 FULL @ 该 bar。

**风险：** 需读 DWS 比对，比 P0-a 复杂。

---

## 运维（存量 repair；P0 已自动防新发）

```bash
# oracle 定范围（默认全历史；sample 500 日常抽检）
python3 -m scripts.audit_dde_trend_oracle --date YYYYMMDD --freq daily --sample 500
# 定点 repair（非 spec 升级；存量脏 trend）
python3 -m backend.cli ops repair-dde-trend --date YYYYMMDD --freq daily --ts-code ...
# repair 后检查 MA v2 副作用
python3 -m backend.cli refresh --date YYYYMMDD --indicator ma --ts-code ...
```

**新发 dc/circ patch：** 日常 `cli run` / `cli refresh` 已自动 invalid dde/daily @ calc_date，无需手工 repair。

---

## 里程碑

| # | 交付 | 验收 |
|---|------|------|
| M1 | oracle 全历史 fix | 603089/300275 不再假阳性；pytest 绿 | ✅ 582 bar matched=0 |
| M2 | P0-a invalidation 接线 | patch 单测 + 1 股 replay | ✅ `maybe_invalidate_dde_after_column_patch` @ run/refresh |
| M3 | runbook + CLAUDE.md + spec §9.4 | 文档对齐 | ✅ |
