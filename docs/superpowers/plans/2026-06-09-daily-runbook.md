# Daily Pipeline Runbook

日常 A 股分析管道运维手册（Phase 3 护栏 + SLA 验收）。

## 日常运行

| 场景 | 命令 |
|------|------|
| 标准日更 | `python -m backend.cli run --date YYYYMMDD` |
| 指定最近交易日（默认 today） | `python -m backend.cli run` |
| 同 day 仅导 Excel（不重算） | `python -m backend.cli export --date YYYYMMDD` |
| 强制重算（不信检测 / spec 发版 / 修史） | `python -m backend.cli refresh --date YYYYMMDD [--indicator ma]` |
| 历史范围 repair | `python -m backend.cli refresh --from A --to B --confirm` |

`run` = fetch → rebuild DWD → calc → export（可用 `--skip-export` 跳过 Excel）。**同 day 无 ODS 变更时** `run` 仍 fetch 比对，但可 skip DWD+calc（`pipeline_shortcut`），此时用 `export` 最快。

**Wave 5 列→指标收窄（run 路径）：** 小范围 ODS 列变更（如 `circ_mv`）时 calc 可仅跑 `dde` 等子集；`CALC_COLUMN_NARROW=0` 关闭。验收：`scripts/smoke_change_driven_refresh.sh --run-wave5`。

**运维命令** 推荐 `python -m backend.cli ops <subcmd>`（顶层 `prune`/`refresh-state` 等仍可用，会 DeprecationWarning）。

## 禁止事项

| 禁止 | 原因 |
|------|------|
| `CALC_FORCE_HARD=1` | 绕过幂等/批处理短路，人为全量重算（**spec 发布日除外**，见下节） |
| `DWD_INCREMENTAL=0` | 回退全量 DWD rebuild，破坏增量路径 |
| 日常无 `ts_codes` 全库 `rebuild_all_dwd` | 毒化 `history_fp`，导致 `chunk≈全市场` |
| SLA 验收前全市场 DWD rebuild | 同上；验收用读库 `benchmark_run` 或真新日 `--run` |
| 日常 `--force` calc/run | 同日复跑应走幂等快路径，非 FORCE 全量 |

运维例外：除权/adj 回填后若未触发 fetch，需显式 `fetch` + `calc --force`（非日常）。

## 算法 SPEC_VERSION 发布（运维例外）

当 `Calculator.SPEC_VERSION` bump（算法/口径升级，DWD 输入未变）时，日常 `run/calc` **不会**自动刷新存量 DWS——须显式运维刷新。

| 步骤 | 命令 | 验收 |
|------|------|------|
| 1 窄指标刷新（推荐） | `python3 -m backend.cli calc --date YYYYMMDD --refresh-spec ma` | `v_dq_spec_freshness` ma spec_stale=0 |
| 2 或全 calc HARD | `CALC_FORCE_HARD=1 python3 -m backend.cli calc --date YYYYMMDD --force` | 同上 |
| 3 语义审计（MA） | `python3 -m scripts.audit_ma_alignment_fallback` | 前两项 = 0 |
| 4 重导 Excel | `python3 -m backend.cli export --date YYYYMMDD` | export 在 calc **之后** |

**允许 `CALC_FORCE_HARD=1` 的场景：** spec 发布日、Gate 3 验收；**日常禁止**。

**语义澄清：** `calc --date` = DWS 快照批次 `calc_date`；`export --date` = Excel 锚定 **trade_date** 截面（读 `v_*_latest`）。

**PR checklist：** bump spec → pytest → refresh-spec/HARD calc → audit → export → health_check Section J。

## 同日复跑

| 目标 | 命令 |
|------|------|
| 重跑管道但不导 Excel | `python -m backend.cli run --date YYYYMMDD --skip-export` |
| 仅重新导出 Excel | `python -m backend.cli export --date YYYYMMDD` |

同日复跑预期：fetch 0 行 → 跳过 rebuild；calc 幂等秒退；export 最快。

## 验收 grep（日志 / progress）

在 `data/tradeanalysis.log` 或终端 progress 中确认增量路径生效：

| 模式 | 含义 |
|------|------|
| `dwd.rebuild_incremental` | stale 子集增量 rebuild（非全库） |
| `mode=week=` | 周线周分区增量（非全历史 weekly DELETE） |
| `batch_only=` | calc 批处理 APPEND 路径（calc_dws data_completeness） |

不应出现：`dwd.rebuild stocks=all`（全库 rebuild）、大批量 `rebuild_all_dwd` WARNING。

辅助命令：

```bash
grep -E 'dwd.rebuild_incremental|mode=week=|dwd.qfq_update|batch_only=' data/tradeanalysis.log | tail
```

## 一次性运维

| 场景 | 命令 |
|------|------|
| calc state **缺失**导致永久 FULL | `python -m backend.cli backfill-state` |
| calc state **指纹过期**（全库 DWD rebuild 后 chunk 爆炸） | `python -m backend.cli refresh-state --date YYYYMMDD` |
| 周线 date_trunc 修复后 | `python -m backend.cli repair-weekly --execute` |
| DWS 快照清理 | `python -m backend.cli prune --keep 5` |
| B4 周线 DDE trend 元数据补洞（dc/circ） | 见下节 **backfill-dde-meta** |
| 验收前库备份 | `cp data/tradeanalysis.duckdb data/tradeanalysis.pre-YYYYMMDD.duckdb` |

`repair-weekly --execute` 后须再跑 `calc` 刷新周线指标。

### backfill-dde-meta（B4 周线 DDE trend）

**根因：** 周线 DDE trend 强制 `moneyflow_dc` + `circ_mv`；ODS 历史缺口会导致 Excel 周线 DDE 趋势大面积 N/A。

**一条命令（推荐）：**

```bash
python -m backend.cli backfill-dde-meta --days 900 --since 20230911 --date YYYYMMDD \
  --sync-dwd --workers 3 --sync-dwd-batch 50 --recalc
```

`--recalc` 闭环（自动，无需手工 env）：

1. `refresh-state` — 对齐 `dws_calc_state` 与 DWD 尾窗  
2. 删除 `dws_dde_weekly` 当日快照（窄范围，非全表）  
3. 子进程 `calc --force`（`CALC_FORCE_HARD=1` + `CALC_FAST_SKIP=0`）— 主要重算 DDE weekly

| 场景 | 命令 |
|------|------|
| 预览缺口 | `backfill-dde-meta --days 900 --dry-run` |
| 中断恢复（ODS 已写、DWD 未 sync） | `backfill-dde-meta --sync-dwd-only` |
| ODS+DWD 已完成，仅 calc 闭环 | `backfill-dde-meta --recalc-only --date YYYYMMDD` |
| Pilot 子集 | 加 `--ts-code 000011.SZ`（stock 路径 + 子集 recalc） |

**禁止：** backfill / `--recalc` 期间并行 `run` 或 `calc`（DuckDB 单写进程）。

### DDE trend 内容修复（2026-06-14 事故）

**症状：** 日线 `dde_trend`（Excel「DDE趋势」）与 B4 moneyflow 算法不一致；`calc --refresh-spec dde` **无效**（state 已是 v2）。

**消费熔断（repair 前）：** combo/screening **勿用** `dde_trend` 硬过滤；见 plan `2026-06-14-dde-trend-repair.md` M0。

| 步骤 | 命令 | 验收 |
|------|------|------|
| 1 Oracle 基线 | `python3 scripts/audit_dde_trend_oracle.py --date YYYYMMDD --freq daily --sample 500` | 记录 mismatch（repair 前） |
| 2 六股 pilot | `python3 -m backend.cli repair-dde-trend --date YYYYMMDD --freq daily --ts-code 600831.SH ...` | oracle 6/6 |
| 3 全市场 daily | `python3 -m backend.cli repair-dde-trend --date YYYYMMDD --freq daily` | sample 500 mismatch < 0.1% |
| 4 验收 | `python3 scripts/audit_dde_trend_oracle.py --date YYYYMMDD --freq daily --sample 500` | exit 0 |

**禁止：** `rebuild_all_dwd`、12 指标无差别 `--force`、repair 期间并行 `run`。

**反面教材：** 仅 `calc --refresh-spec dde` 无法修复（`find_spec_stale_codes` 返回 0）。

**2026-06-14 实库 repair 已完成（calc_date=20260612）：** oracle sample 500 mismatch=0；health_check Section K PASS。

---

**验收：** 成熟股最新 week-end 截面 `v_dws_dde_weekly_latest.trend` NULL ≤20%；Excel 周线 DDE趋势 N/A 应显著低于补洞前（~90%）。

### refresh-state vs backfill-state

| | backfill-state | refresh-state |
|--|----------------|---------------|
| 触发 | `dws_calc_state` **无行** | 有行但 `history_fp` ≠ 当前 DWD 245 尾窗 |
| 行为 | FULL 算指标 + 写 DWS | **只**重算指纹 UPSERT state，**不写 DWS** |
| 耗时 | 与缺失键数量成正比（可能数小时） | 全市场约 **10–15 min** |
| 验收后 | 跑完应 `health_check` | 日志 `chunk_stocks` 应 ≪ 全市场；再 `run --skip-export` 验证 |

预览不写库：`python -m backend.cli refresh-state --date YYYYMMDD --dry-run`

**禁止**：用 `refresh-state` 替代「DWD 尾窗数值真变且须重算 DWS」的场景——那种情况须 FULL chunk 或按股重算。

## SLA 验收（M4 真新日）

目标：稳态真新日整条链路 ≤ **1800s**（30 min）。

| 模式 | 命令 | 说明 |
|------|------|------|
| 读库摘要（默认） | `python scripts/benchmark_run.py --date YYYYMMDD` | 汇总 `ods_etl_log` 分项；exit 1 若 logged total > SLA |
| 实跑门禁 | `python scripts/benchmark_run.py --date YYYYMMDD --run` | 执行 `cli run` 测墙钟；exit 2 若 elapsed > SLA |
| 实跑不导出 | `python scripts/benchmark_run.py --date YYYYMMDD --run --skip-export` | **M4 推荐**：真新日签字，跳过 Excel |

**M4 日选择：** `dim_date` 中 `trade_date > MAX(ods_daily)` 的首个交易日（非 ODS 已有日）。当前锚点：`ods_max=20260609` → `--date 20260610`。

**推荐执行链（2026-06-11）：**

1. 完成 `docs/superpowers/plans/2026-06-11-batch-preflight-silent-gap.md` Task 1–5（同日复跑 ≤300s 目标）
2. 备份库 → `benchmark_run --date 20260610 --run --skip-export`
3. `python scripts/health_check.py`
4. 更新 `pipeline-30min-optimization.md` 附录 B

**实跑监控：** `chunk_stocks≥400` 或 `calc.stocks` 持续爬升 → 停跑查 state；`dde_weekly` / `batch_preflight` 长静默见 silent-gap plan。

输出含：`run_fetch` / `run_rebuild_dwd` / `calc_dws` / `run_export` 分项耗时，`calc_dws` 的 `batch_only` / `chunk_stocks`，以及 log grep 提示。

## 相关文档

- 优化计划：`docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md`
- 数据模型 spec：`docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md`
