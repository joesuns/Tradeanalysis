# Post-Merge 验收补洞 — 8 条要求完全满足（双架构定稿）

**Date:** 2026-06-16  
**Status:** P0+P1 代码/单测已实施 — **G10 实库 smoke 待签字；8/8 验收表未全绿前禁止宣称收尾**  
**Parent:** [`2026-06-15-change-driven-refresh-cli.md`](2026-06-15-change-driven-refresh-cli.md)（已 merge PR #7）  
**Related:** [`2026-06-15-wave5-column-indicator-deps.md`](2026-06-15-wave5-column-indicator-deps.md)

**背景：** PR #7 已合入 `main`，Wave 1–5 主链路可跑，但双架构 review 认定 **8 条用户要求尚未全部可验收**。本 plan 为 **唯一收尾基线**。

---

## 0. 收尾定义（Hard Gate）

**只有下表全部为 ✅，才允许：**

- 更新 parent plan §1.1 为真实 ✅  
- 在 runbook 标注「change-driven refresh 生产签字」  
- 关闭本 plan  

**任一 ❌ 或 ⬜ → 禁止收尾。**

| 门禁 | 负责人 | 状态 |
|------|--------|------|
| G0 用户批准本 plan | 用户 | ✅ |
| G1–G8 八条要求验收表（§4） | 双架构 + 用户 | ⬜ |
| G9 全量 `pytest tests/ -v` 绿 | CI | ⬜ |
| G10 实库 smoke §9 + wave5 归档 | 运维 | ✅（Section J MA 1 FAIL 为 WIP） |
| G11 文档三处一致（CLAUDE / runbook / spec） | 开发 | ⬜ |

---

## 1. 问题陈述（双架构共识）

### 1.1 已交付（不必重做）

- ODS diff + `FetchResult` + 三表比对 + D2 patch 事件  
- `PipelineContext` + pipeline_shortcut（0 diff 仍 fetch）  
- `cli refresh` R1 + 日期范围 + ops 子命令  
- Wave 5 `column_indicator_deps` + batch/chunk 收窄接线  
- CI pytest 全绿（merge 时）

### 1.2 阻止「完全满足」的缺口（P0）

| ID | 缺口 | 影响要求 |
|----|------|----------|
| **P0-A** | `run --force` 未穿透 L0 `pipeline_shortcut`，calc 整段跳过 | #7 #8 |
| **P0-B** | Wave 5 narrow **无** narrow vs full 等价性 pytest | #2 #6 #7 |
| **P0-C** | `calc --refresh-spec` 文档/spec 有，CLI 未接线；`calc_spec_refresh.py` 无测试 | #3 #8 |
| **P1-D** | `FetchResult.to_completeness` 无列级观测 | #1 审计 |
| **P1-E** | 实库 smoke §9 / `--run-wave5` 未签字 | 全链路 |
| **P1-F** | ODS float 容差（`1e-4`）与 plan 叙事（`1e-9`）未分层定稿 | #1 #7 |
| **P1-G** | `PipelineContext.indicator_filter` 与 `RunCalcHandoff` 双轨 | 维护性（系统债，P1 可选收敛） |

### 1.3 明确 Out of Scope（不算未完成）

- combo 全历史自动 backfill  
- per-stock 指标 narrow（`changed_codes` 预留）  
- etl_log 级 mutation v2  
- contextvars 替代 global handoff（单进程运维可接受，二期）

---

## 2. 目标态数据流（定稿，实施不得偏离）

```text
FETCH（run/refresh 必达）
  → FetchResult(rows_written, changed_pairs, changed_field_events)
  → PipelineContext / RunPlan
       skip_dwd_calc = f(rows_written, spec_stale, stale_dwd, prior_calc, force)  ← P0-A 修复 force
       indicator_filter = resolve_run_calc_indicator_filter(...)  [可选收敛进 Context]
  → [skip_dwd_calc ? export : DWD narrow rebuild]
  → [calc: run_calc(force, indicator_filter)]
       L1 idempotent / L2 batch shortcut / L5 narrow — 均受 force & spec_stale 约束
  → DWS INSERT-only (calc_date = 分析日)
  → export / ADS v_*_latest
```

**两类变更（正交验收）：**

| 类型 | 发现 | calc 路径 | 运维命令 |
|------|------|-----------|----------|
| 源数据 | ODS diff / patch | DWD 窄链 + 路由/narrow | `run` |
| 算法 spec | `dws_calc_state.spec_version` 落后 | 该指标窄窗 FULL | `calc --refresh-spec` 或 `refresh --indicator` |

---

## 3. 实施波次（严格顺序）

### Phase P0 — 行为与质量门禁（必须先绿）

#### Task P0-1：`run --force` 穿透 pipeline shortcut（系统 P0-A）

**根因：** `_cmd_run_single_day` 未将 `args.force` 传入 shortcut 决策；`skip_dwd_calc=True` 时 calc 不调用。

**方案（最小范围）：**

```python
# pipeline_context.compute_skip_dwd_calc 或 from_fetch 增 force_recalc 参数
if force_recalc:
    return False  # 不 skip DWD+calc 决策中的 calc 段；DWD 仍仅在 changed∪stale 时写

# _cmd_run_single_day
pipeline_ctx = PipelineContext.from_fetch(..., force_recalc=getattr(args, "force", False))
# 或: if args.force: skip_dwd_calc = False
```

**禁止：** `rebuild_all_dwd` 全市场；force 只打开 calc 机会，仍走 `_should_skip_calc_idempotent(force=True)`。

**文件：**

- `backend/etl/pipeline_context.py`
- `backend/cli.py`
- `tests/test_cli.py` — 新增 `test_cmd_run_force_bypasses_pipeline_shortcut`

**验收：**

- [ ] 同 day 0 ODS diff + prior calc + `run --force` → 有 `calc_dws` log（非 shortcut skip calc）  
- [ ] spec_stale 时 force 仍不 idempotent skip（已有 `test_idempotent_force_blocked_when_spec_stale` 保持绿）  
- [ ] 无 diff 无 force → 仍 pipeline_shortcut（smoke #1 行为不变）

**双架构签字：** 系统架构师 ⬜ | 数据架构师 ⬜ | 用户 ⬜

---

#### Task P0-2：接入 `calc --refresh-spec`（数据 P0-C）

**根因：** `backend/etl/calc_spec_refresh.py` 存在但未进 CLI；零测试。

**方案：**

- `cmd_calc` 增 `--refresh-spec ma[,volume]`  
- 有 `--refresh-spec` 时：调用 `cmd_refresh_spec` / `run_refresh_spec`，**不**走完整 `run_calc` 全市场路由  
- 语义：仅 `find_spec_stale_codes` 子集 + 对应 `(indicator,freq)` batch FULL；**不 rebuild DWD**

**文件：**

- `backend/cli.py`（argparse + cmd_calc 分支）
- `backend/etl/calc_spec_refresh.py`（若未在 main，须 merge 进分支）
- `tests/test_etl/test_calc_spec_refresh.py` — **新建**
- `tests/test_cli.py` — mock 冒烟

**验收：**

- [ ] `python -m backend.cli calc --date D --refresh-spec ma --help` 可见  
- [ ] stale 子集非空 → `calc_refresh_spec` etl log + `dws_ma_*` 新行  
- [ ] stale 为空 → 秒退 + log「no stale rows」  
- [ ] 与 `refresh --indicator ma` 文档分工写清（refresh=全链路 R1；refresh-spec=轻量 spec 刷新）

**双架构签字：** 系统架构师 ⬜ | 数据架构师 ⬜ | 用户 ⬜

---

#### Task P0-3：Wave 5 narrow vs full 等价性 golden（数据 P0-B）

**根因：** 性能优化未证明「范围更小且结果等价」→ 违反 engineering-protocol。

**方案：**

- 新建 `tests/test_etl/test_column_narrow_equivalence.py`  
- Fixture：内存 DuckDB，1–3 股，注入 `circ_mv` / `net_amount_dc` / `vol`-only 变更  
- 每条 case：  
  1. `CALC_COLUMN_NARROW=0` 或 `indicator_filter=None` → full 路由 calc  
  2. `indicator_filter=["dde"]`（或对应窄集）→ narrow calc  
  3. 比对 `v_dws_*_latest` 相关列，`atol=1e-9`（与 `test_append_calc.py` 一致）

**禁止：** 为通过测试改 Calculator 算法；失败则 narrow 逻辑或 G 门禁 bug。

**文件：**

- `tests/test_etl/test_column_narrow_equivalence.py`  
- 可选：小 fixture CSV 在 `tests/fixtures/`

**验收：**

- [ ] ≥2 case 绿（建议：`circ_mv` UPDATE + `vol`-only）  
- [ ] CI 必跑此文件  
- [ ] 绿前 runbook 标注：`CALC_COLUMN_NARROW=1` 为默认；若失败可临时 `=0`

**双架构签字：** 系统架构师 ⬜ | 数据架构师 ⬜ | 用户 ⬜

---

### Phase P1 — 契约、观测、实库（P0 全绿后）

#### Task P1-1：`FetchResult.to_completeness` 扩展（P1-D）

```python
"changed_field_events_count": len(self.changed_field_events),
"affected_ods_columns": sorted({ev[3] for ev in self.changed_field_events}),  # 可选
```

**文件：** `backend/fetch/fetch_result.py`、`tests/test_fetch/test_ods_diff.py`

**验收：** `run_fetch` log JSON 含新字段

---

#### Task P1-2：ODS float 容差 spec 分层（P1-F）

**定稿（写入 data-model spec § 或本 plan 附录）：**

| 层 | 容差 | 用途 |
|----|------|------|
| ODS diff | `FLOAT_ABS_TOL=1e-4`, large `1e-0`/rtol | DuckDB float32 vs API roundtrip |
| DWS 等价性 | `atol=1e-9` | append/FULL/narrow oracle |

**文件：** `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`（小版本 bump）、`tests/test_fetch/test_ods_diff.py` 注释

**验收：** 文档与 `backend/fetch/ods_diff.py` 常量一致

---

#### Task P1-3：实库 smoke 签字（P1-E）

```bash
export ANALYSIS_DATE=YYYYMMDD  # 已有 calc 快照日
./scripts/smoke_change_driven_refresh.sh              # 只读
./scripts/smoke_change_driven_refresh.sh --run-all    # §9 项 1–6
./scripts/smoke_change_driven_refresh.sh --run-wave5
```

**手工项 smoke #3（spec bump）：**

1. bump `MACalculator.SPEC_VERSION`（或测试用 mock stale 行）  
2. `run --date D` → 非 pipeline_shortcut；仅 ma 路由实质 FULL  
3. `v_dq_spec_freshness` ma stale=0（若有）

**归档：** log 存 `docs/superpowers/plans/evidence/2026-06-16-smoke/`（或 PR 描述）

**双架构签字：** 系统架构师 ⬜ | 数据架构师 ⬜ | 用户 ⬜

---

#### Task P1-4：Context 单源（P1-G，可选本 PR 或 follow-up）

- `indicator_filter` 移入 `PipelineContext` 构建；`RunCalcHandoff` 改为 Context 视图或废弃  
- **不阻塞 G1–G8** 若 handoff 行为不变且 P0-3 绿

---

### Phase P2 — 文档收尾（与 P1 并行，G11）

| 文件 | 动作 |
|------|------|
| `CLAUDE.md` | calc `--refresh-spec`；run `--force` 与 shortcut 关系；Wave5 等价性签字说明 |
| `docs/superpowers/plans/2026-06-15-change-driven-refresh-cli.md` | §1.1 改链接本 plan；§12 关闭 |
| `docs/superpowers/plans/2026-06-09-daily-runbook.md` | spec 发布 SOP 与 refresh-spec 对齐 |
| `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` | ODS 容差分层；refresh-spec CLI |

---

## 4. 八条要求 — 验收表（唯一签字依据）

| # | 要求 | 验收证据（全部必须 ⬜→✅） | 负责 Task |
|---|------|---------------------------|-----------|
| **1** | 源端变更 → 仅变更 ODS 重入库 | `test_ods_diff` + float golden；fetch log 含 `changed_field_events_count` | P1-1, P1-2 |
| **2** | ODS 变更 → 下游按范围重算 | smoke #2；P0-3 等价性；DWD rebuild 子集 log | P0-3, P1-3 |
| **3** | 指标 logic 变更 → 该指标重算 | P0-2 refresh-spec；smoke #3；`test_calc_spec_gate` | P0-2, P1-3 |
| **4** | 默认只刷指定日；全历史显式 | `test_cli/test_date_range.py`；refresh 范围 log | 已有 + P1-3 #5-6 |
| **5** | 历史数据自动补齐 | orchestrator auto-fetch 回归 pytest | G9 全量 |
| **6** | 分层只写相关表 | DWD 子集 + P0-3 narrow 等价 | P0-3 |
| **7** | 质量优先、不重复算 | smoke #1；P0-1 force；P0-3；三层短路文档 | P0-1, P0-3, P2 |
| **8** | CLI 易用 | `--help` 可执行：run/export/refresh/calc/refresh-spec/ops | P0-1, P0-2, P2 |

**用户总签字：** ⬜（8/8 ✅ 后勾选）

---

## 5. 三层短路 — 系统定稿表（P2 写入 CLAUDE）

| 层 | 机制 | force | spec_stale | narrow |
|----|------|-------|------------|--------|
| L0 | pipeline_shortcut | **不短路**（P0-1） | 不短路 | N/A |
| L1 | calc idempotent | 智能短路 | 不短路 | N/A |
| L2 | batch force shortcut | 可跳过 tail | fallthrough | N/A |
| L5 | column narrow | 不 batch shortcut | merge 进 filter | 等价性 P0-3 |

---

## 6. 开发流程与跟踪（双架构把关）

### 6.1 分支与 PR

- 分支：`fix/post-merge-acceptance-gates`  
- **一个 PR 完成 P0；P1 可同 PR 或 follow-up，但 G0–G8 关闭前必须 merge 全部**

### 6.2 每 Task 完成后的强制流程

1. 开发提交：代码 + pytest + 本 plan Task checkbox  
2. **数据架构师 review**（契约/等价性/spec）→ 通过 / 不通过  
3. **系统架构师 review**（编排/测试/CLI）→ 通过 / 不通过  
4. **用户** → 「好」进入下一 Task  
5. 任一「不通过」→ 不得勾选验收表

### 6.3 Review 输出模板（复制到 PR comment）

```markdown
## Task P0-X Review

### 数据架构师
- 契约/等价性：
- 验收表条目：#_
- 结论：通过 / 不通过

### 系统架构师
- 编排/模块/测试：
- 结论：通过 / 不通过
```

### 6.4 CI 要求

- PR：`pytest tests/ -v`  
- 必含：`tests/test_cli.py`（force）、`tests/test_etl/test_calc_spec_refresh.py`、`tests/test_etl/test_column_narrow_equivalence.py`

---

## 7. 决策树（实施时强制）

```
改 DWD/calc？
├─ P0-A force → 仅 skip 门禁，DWD 仍 rebuild_dwd_for_stale(changed∪stale)
├─ P0-2 refresh-spec → find_spec_stale_codes + batch FULL，无 DWD
├─ P0-3 narrow → 等价性绿后才宣传性能收益
└─ 禁止：habit rebuild_all_dwd / 无等价性开 narrow / 跳过 G1-G3
```

---

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 等价性测试 flaky | 固定 fixture 股；禁用多线程 calc 在 test con |
| circ_mv INSERT 不 narrow | 接受 G5；runbook 说明 backfill 走全路由 |
| force 导致同 day 墙钟变长 | 预期行为；文档写「force 非日常」 |
| calc_spec_refresh 未在 main | Task P0-2 首步确认文件存在，否则从 WIP  cherry-pick |

---

## 9. 实施 Checklist（开发 Agent）

- [x] G0 用户批准本 plan  
- [x] P0-1 force 穿透 + test  
- [x] P0-2 refresh-spec CLI + test  
- [x] P0-3 narrow 等价性 ≥2 case  
- [x] P1-1 completeness 字段  
- [x] P1-2 float spec 分层  
- [x] P1-3 实库 smoke 归档（见 `evidence/2026-06-16-smoke/`）  
- [x] P2 文档三处一致  
- [ ] G9 pytest 全绿  
- [ ] G10 实库 smoke  
- [ ] G11 文档与实库一致  
- [ ] G9 pytest 全绿  
- [ ] §4 八条验收表 8/8 ✅  
- [ ] G11 文档核对  
- [ ] 用户总签字 → 关闭本 plan + 更新 parent plan §1.1  

---

## 10. 审批

- [ ] 用户批准本 plan（回复「好 / 同意实施」）  
- [ ] 数据架构师批准  
- [ ] 系统架构师批准  

**批准后：** 从 P0-1 开始；**未批准禁止改码。**

---

## 附录 A — 受影响文件清单

| 文件 | Phase |
|------|-------|
| `backend/cli.py` | P0-1, P0-2 |
| `backend/etl/pipeline_context.py` | P0-1 |
| `backend/etl/calc_spec_refresh.py` | P0-2 |
| `backend/fetch/fetch_result.py` | P1-1 |
| `backend/fetch/ods_diff.py` | P1-2 文档引用 only |
| `tests/test_cli.py` | P0-1, P0-2 |
| `tests/test_etl/test_calc_spec_refresh.py` | P0-2 新建 |
| `tests/test_etl/test_column_narrow_equivalence.py` | P0-3 新建 |
| `CLAUDE.md`, runbook, data-model spec | P2 |
| `docs/superpowers/plans/2026-06-15-change-driven-refresh-cli.md` | P2 |

**不改：** Calculator 算法、schema DDL、export 列（除非 spec 文档 bump only）
