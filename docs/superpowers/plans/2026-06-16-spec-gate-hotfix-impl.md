# Spec Gate Hotfix 实施计划

**日期：** 2026-06-16  
**状态：** 已实施（M0–M4 代码；S2 实库运维待执行）  
**审批：** 同意 — 2026-06-16

---

## ① 背景与根因（证据链）

| # | 问题 | 代码证据 | 业务影响 |
|---|------|----------|----------|
| A | 幂等闸门 12 表全库 `ROW_NUMBER` | `has_spec_stale_indicators` → `count_dws_spec_stale_by_indicator(con)` 无 scope | 同日复跑 SLA 失效 |
| B | DWS stale 语义 ≠ export 读路径 | `v_dws_*_latest`：`PARTITION BY ts_code, trade_date`（schema.py L508–511）；gate 用 `PARTITION BY ts_code`（calc_spec_gate.py / base.py `load_latest_spec_versions`） | 误拦幂等 / 漏检截面 |
| C | auto refresh 无视 Wave5 | `run_auto_spec_refresh_if_needed(con, None, ts_codes)` 无 `indicator_filter` | 列收窄失效 |
| D | `CALC_BATCH_FULL=0` 空转 | `run_batch_full_phase` 直接 return 0 | stale 永存 + 幂等永不通 |
| E | 测试/PR 卫生 | `test_calc_spec_gate.py` 未 track；无 orchestrator 3a 集成测 | 回归不可见 |
| F | export layout 与 spec 同 diff | export_wide 大改混 PR | bisect 困难 |

**已确认保留（不回滚）：** S0 `batch_append_{macd,volume,dde}` 补 `spec_version`（根因 A 写库侧）。

**决策基线（架构师默认，可推翻须书面记录）：**

1. 幂等 / L0：**state 聚合 + 当日 `trade_date` 截面** fast gate；禁止全库 DWS 扫描。  
2. auto refresh：**`indicator_filter` + `trade_date=calc_date` 截面** stale 子集窄窗 FULL。  
3. Phase：**R1 → R2 → R3 → R4**；R3 可与 R1/R2 **同 PR**（同 touch `calc_spec_gate.py` + schema）。  
4. **export layout / column comments 拆独立 PR**（本 plan 不含）。

---

## ② 目标与非目标

### 目标

- 同日复跑：当日分析截面 spec 已齐 → **恢复秒级 idempotent skip**。  
- 新日 / 首次 calc：auto refresh **最小指标×最小股×窄窗 FULL**。  
- Export gate / health：可观测 **anchor trade_date 截面** spec 新鲜度。  
- 测试锁定语义差异（全局 latest vs trade_date 截面）。

### 非目标

- 不改 DWD / 不 `rebuild_all_dwd`。  
- 不改 Calculator 算法 / `SPEC_VERSION` 值。  
- 不在本 plan 做全历史 backlog 自动清（仍走 **S2 一次性运维**）。  
- 不合并 export layout PR。

---

## ③ 里程碑总览

```text
M0  PR 拆分 + 基线冻结
M1  R1 Fast Gate + 语义修正          → DA+SA Review #1
M2  R2 Auto Refresh 收窄 + 运维契约   → DA+SA Review #2
M3  R3 DQ 视图 + 观测 scoped         → DA+SA Review #3
M4  R4 测试补全 + 文档 + S2 验收      → DA+SA Final Sign-off
```

| 里程碑 | 交付物 | 墙钟预估（ dev ） |
|--------|--------|-------------------|
| M0 | 分支策略、scope 文档 | 0.5h |
| M1 | `calc_spec_gate` API + 调用点 | 3–4h |
| M2 | `calc_spec_refresh` + orchestrator 3a | 2–3h |
| M3 | `v_dq_spec_freshness` + export/health | 2–3h |
| M4 | 测试 + runbook + 实库 S2 | 2h + 运维 |

---

## M0 — PR 拆分与基线（开工前）

### 任务

- [ ] **M0.1** 确认 spec governance 代码 PR **不含** export layout / column comments（或 revert 至独立 branch）。  
- [ ] **M0.2** `git add tests/test_etl/test_calc_spec_gate.py`（当前 untracked）。  
- [ ] **M0.3** 记录实库基线（只读）：  
  ```bash
  # 分析日截面 dde spec 分布（示例 20260616）
  python -c "..."  # 或 SQL on v_dws_dde_daily_latest WHERE trade_date='20260616'
  ```
- [ ] **M0.4** 冻结验收日 `ACCEPTANCE_DATE=20260616`（与 S2 一致）。

### M0 验收（DA + SA）

| 角色 | 检查项 | Pass 标准 |
|------|--------|-----------|
| DA | PR scope | spec gate 文件与 export layout 无混 diff |
| SA | 基线 SQL 可复现 | 截面 stale 股数有记录 |
| 共同 | 回滚策略 | 可单独 revert M1–M3 而不动 S0 |

---

## M1 — R1 Fast Gate + 语义修正

### M1.1 API 设计（`backend/etl/calc_spec_gate.py`）

新增/调整（命名可微调，语义不可变）：

```python
def dws_latest_view(indicator: str, freq: str) -> str:
    # 复用 calc_indicators.dws_latest_view 或本地 thin wrapper

def has_spec_stale_on_trade_date(con, trade_date: str) -> bool:
    """12 路由 EXISTS；每路由：
       SELECT 1 FROM v_dws_{prefix}_{freq}_latest
       WHERE trade_date=? AND COALESCE(spec_version,'v1')<>expected LIMIT 1"""

def count_dws_spec_stale_on_trade_date(con, trade_date, ts_codes=None) -> Dict[str,int]:
    """scoped count；供 data_completeness / export gate"""

def has_spec_stale_indicators(con, trade_date: Optional[str] = None) -> bool:
    """trade_date 给定：state_stale OR trade_date截面 stale（fast）
       trade_date None：仅 state（export/health 全库轻量路径，禁止 12 表全扫）"""
```

**废弃/降级路径：**

- 幂等 / L0 / calc 收尾：**禁止**无参 `count_dws_spec_stale_by_indicator(con)`。  
- 保留 `find_dws_spec_stale_codes(..., trade_date=)` 供 refresh；内部改查 **view @ trade_date**，不再 `PARTITION BY ts_code` 全表。

**异常处理：**

```python
except duckdb.CatalogException:
    ...
# 禁止 bare except Exception → 0
```

### M1.2 调用点

| 文件 | 函数 | 改动 |
|------|------|------|
| `orchestrator.py` | `_should_skip_calc_idempotent` | `has_spec_stale_indicators(con, calc_date)` |
| `pipeline_context.py` | `compute_skip_dwd_calc` | `has_spec_stale_indicators(con, analysis_date)` |
| `export_wide.py` | `EXPORT_SPEC_GATE` 块 | `has_spec_stale_on_trade_date(con, trade_date)` |

### M1.3 测试（M1 最小集）

| 测试 | 断言 |
|------|------|
| `test_trade_date截面_stale_阻断` | v1 @ trade_date → gate True |
| `test_其他trade_date_v1_不阻断幂等` | trade_date=T 全 v3；T-1 有 v1 → gate False |
| `test_state_stale_仍阻断` | state v1 → True |
| `test_CatalogException_不吞掉其他错误` | mock 非 catalog 错误应 raise |

### M1 验收门禁（DA + SA Review #1）

**数据架构师（DA）**

- [ ] stale 定义与 spec §9 / export 读 `v_*_latest @ trade_date` **一致**。  
- [ ] 幂等闸门不误放行「当日截面 v1 仍 export 出去」的场景。  
- [ ] `find_dws_spec_stale_codes` 与 `load_latest_spec_versions` 路由域关系 **文档化**（路由仍可用 per-ts_code global latest；export gate 用 trade_date 截面）。

**系统架构师（SA）**

- [ ] 幂等路径 SQL：**12 条 EXISTS + LIMIT 1**，无全表 window scan。  
- [ ] `pytest tests/test_etl/test_calc_spec_gate.py -v` 全绿。  
- [ ] 现有 `test_cli.py` / idempotent 相关 mock 仍兼容（或更新 mock 签名）。

**共同 Sign-off：** M1 merge 前 grep 确认无 `count_dws_spec_stale_by_indicator(con)` 裸调用（除 scoped 实现内部）。

---

## M2 — R2 Auto Refresh 收窄

### M2.1 代码

| 文件 | 改动 |
|------|------|
| `calc_spec_refresh.py` | `run_auto_spec_refresh_if_needed(..., indicator_filter=None, trade_date=calc_date)` |
| | `find_spec_stale_codes_merged(..., trade_date=)` 合并 state + **截面** DWS |
| | `refreshed==0` 且 stale 非空 → WARNING（`CALC_BATCH_FULL=0` 明示） |
| `orchestrator.py` step 3a | 传 `indicator_filter`；日志 `auto_spec_refresh.indicators` |

**决策树合规：**

- 仅 spec 变 → stale 子集 × 指标窄窗 FULL；**无 DWD rebuild**。  
- Wave5 active 时 refresh 指标 ⊆ `indicator_filter ∪ state_stale 指标`。

### M2.2 运维契约（文档，M2 同步 runbook 草案）

```bash
# 首次启用 CALC_AUTO_SPEC_REFRESH 前（一次性）
python -m backend.cli calc --refresh-spec dde --date 20260616

# 迁移窗口可选
CALC_AUTO_SPEC_REFRESH=0 python -m backend.cli run --date 20260616
```

### M2.3 测试

| 测试 | 断言 |
|------|------|
| `test_auto_refresh_respects_indicator_filter` | filter=`['dde']` 时不刷 ma |
| `test_auto_refresh_trade_date_scoped` | 仅 trade_date 截面 stale 触发 |
| `test_batch_full_disabled_warns` | CALC_BATCH_FULL=0 + stale → WARNING |

### M2 验收门禁（DA + SA Review #2）

**DA**

- [ ] auto refresh 范围 ≤ merged stale 子集；无「习惯性 12 路由全刷」。  
- [ ] S2 命令与 acceptance date 写入 runbook。

**SA**

- [ ] step 3a 后 `preflight_ctx=None` 行为仍正确（有 refreshed 时）。  
- [ ] `indicator_filter` 与 `run_batch_append_phase` 一致。  
- [ ] `pytest tests/test_etl/test_calc_spec_refresh.py -v` 全绿。

---

## M3 — R3 DQ 视图 + 观测 scoped

### M3.1 `v_dq_spec_freshness`（`backend/db/schema.py`）

补 governance plan Task 2.1（当前代码库 **缺失**）：

```sql
-- 每 (indicator, freq) 一行；anchor_trade_date = 参数化或 MAX(trade_date) 由 health 传入
-- 列：indicator, freq, anchor_trade_date, total, spec_ok, spec_stale, expected_spec
-- 数据源：v_dws_*_latest @ anchor_trade_date
```

实现选项（SA 选型，默认 A）：

- **A（推荐）：** 视图按 `anchor_trade_date` 列 + health/export 传 date 过滤。  
- **B：** 纯 SQL 函数 `dq_spec_freshness(con, trade_date)` 不建视图。

### M3.2 观测收窄

| 位置 | 改动 |
|------|------|
| `orchestrator.py` log_etl_end | `dws_spec_stale_counts` → `count_dws_spec_stale_on_trade_date(con, calc_date, codes_to_calc)` |
| `export_spec_freshness_warnings` | 用 trade_date 截面 count |
| `scripts/health_check.py` | Section J：`v_dq_spec_freshness` 或等价查询 |

### M3.3 测试

- [ ] `tests/test_schema.py` 或新建：`v_dq_spec_freshness` 可 CREATE  
- [ ] health_check Section J smoke（memory db fixture）

### M3 验收门禁（DA + SA Review #3）

**DA**

- [ ] DQ 视图指标与 `CALC_ROUTE_SPECS` 12 路由对齐。  
- [ ] `expected_spec` = `Calculator.SPEC_VERSION` 单一来源（`INDICATOR_SPEC_VERSIONS`）。

**SA**

- [ ] calc 收尾无全库 12 表 scan。  
- [ ] schema 变更后 `create_all_tables` 测试通过。  
- [ ] `EXPORT_SPEC_GATE=1` 仅查截面，墙钟可接受（<5s 实库抽样）。

---

## M4 — R4 测试补全 + 文档 + 实库 S2

### M4.1 集成测试

| 文件 | 内容 |
|------|------|
| `tests/test_etl/test_run_calc_auto_spec_refresh.py`（新） | mock `_execute_spec_stale_batch_full`；断言 3a 调用、filter 传递、preflight 清空 |
| `tests/test_export/test_export_spec_gate.py`（新） | EXPORT_SPEC_GATE WARNING |
| 扩展 idempotent 测试 | 旧日 v1 + 当日 v3 → skip OK |

### M4.2 文档

- [ ] `CLAUDE.md`：fast gate 语义、`trade_date` 截面、迁移 SOP  
- [ ] `daily-runbook.md`：S2 必跑、迁移窗口 `CALC_AUTO_SPEC_REFRESH=0`  
- [ ] 更新 `2026-06-14-calc-spec-version-governance.md`：标记 hotfix 完成项

### M4.3 实库 S2（运维，非 CI）

```bash
python -m backend.cli calc --refresh-spec dde --date 20260616
python -m backend.cli export --date 20260616
EXPORT_SPEC_GATE=1 python -m backend.cli export --date 20260616  # 无 WARNING
python -m backend.cli run --date 20260616 --skip-export          # 第二次：idempotent skip
```

### M4 Final Sign-off（DA + SA）

| 检查 | 命令/证据 |
|------|-----------|
| 全量 pytest | `pytest tests/ -v`（或 CI 子集 + etl/export 全绿） |
| 截面 spec | `dde_daily spec_stale=0 @ 20260616`（DQ 视图或 SQL） |
| 幂等 | 同日复跑 log 含 `idempotent_skip` 或 `force_same_day_skip` |
| 墙钟 | 幂等路径无 12 表 full scan（grep + 日志无异常慢查询） |
| 文档 | CLAUDE + runbook 与代码一致 |

---

## ④ 双架构师 Review 模板（每里程碑复用）

### 数据架构师（DA）Checklist

1. **语义：** stale 检测域 = export/calc 消费域？  
2. **最小范围：** 是否仅 stale 子集 FULL，无全库 rebuild？  
3. **spec 单一来源：** `INDICATOR_SPEC_VERSIONS` / `Calculator.SPEC_VERSION` 无硬编码分叉？  
4. **截面正确：** 日线 `trade_date`；周线 export 用 `week_end ≤ trade_date`（若 M3 export weekly gate 需单独说明 — **待 M3 确认是否扩 weekly anchor**）。  
5. **运维：** S2 与日常 auto refresh 分工清晰？

### 系统架构师（SA）Checklist

1. **性能：** 热路径（幂等/L0/calc 收尾）无 O(全库 DWS) 扫描？  
2. **集成：** orchestrator 3a → 3b → chunk 无重复 FULL？  
3. **配置矩阵：** `CALC_AUTO_SPEC_REFRESH` × `CALC_BATCH_FULL` × `CALC_COLUMN_NARROW` 行为表？  
4. **失败模式：** 无 bare `except`；`CALC_BATCH_FULL=0` 有 WARNING？  
5. **测试：** 单测 + 集成覆盖；CI 可重复？  
6. **回归：** append/FULL 等价性测试未破坏？

### Review 结论格式

```markdown
## Review #{N} — M{N} — YYYY-MM-DD
- DA: APPROVE / REQUEST CHANGES — （1 句话）
- SA: APPROVE / REQUEST CHANGES — （1 句话）
- Blockers: （列表或 None）
- 允许进入 M{N+1}: Yes/No
```

---

## ⑤ 风险登记

| 风险 | 概率 | 缓解 |
|------|------|------|
| 周线截面 anchor 与日线混用 | 中 | M3 DA 明确 weekly gate 用 `week_end`；export 已有逻辑复用 |
| S2 未跑即开 auto refresh | 高 | runbook 强制序；可选迁移期默认 `CALC_AUTO_SPEC_REFRESH=0` |
| `v_dq_spec_freshness` DDL 与实库 drift | 中 | `create_all_tables` + 运维一次性 schema init |
| 路由域 vs 截面域双轨理解成本 | 中 | calc_spec_gate 模块 docstring + CLAUDE 一节 |

---

## ⑥ 执行顺序（审批后）

1. 用户回复 **「同意」** 本 plan。  
2. M0 → M1 → **Review #1** → M2 → **Review #2** → M3 → **Review #3** → M4 → **Final**。  
3. 每 milestone：**先 pytest 绿 → 再发 Review → 通过后 merge/commit**。  
4. **禁止**跨 milestone 合并未验收代码。

---

## ⑦ 待架构师确认项（可选 override）

| # | 默认 | 问题 |
|---|------|------|
| 1 | 同 PR 交付 M1+M2+M3 | 是否拆成两个 PR（hotfix / dq-view）？ |
| 2 | weekly export gate 不在 M3 | 是否纳入 M3 周线 `week_end` 截面检测？ |
| 3 | `ACCEPTANCE_DATE=20260616` | 实库 S2 验收日是否更换？ |

**审批：** _____________ 日期：__________
