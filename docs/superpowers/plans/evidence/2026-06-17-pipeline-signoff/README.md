# Pipeline 30min 终验证据（2026-06-17）

**Plan：** `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md` 附录 F  
**分支：** `perf/b2-polyfit-vectorization`（含 PR #14 + 附录 F/S5 签字 commit）  
**DB 备份：** `data/tradeanalysis.pre-pipeline-signoff-20260617.duckdb`

## S1 — PR #14

- Merged: https://github.com/joesuns/Tradeanalysis/pull/14
- Target: `perf/b2-polyfit-vectorization`（repo default branch）

## S3 — benchmark_run @ 20260616

```bash
python3 scripts/benchmark_run.py --date 20260616 --run
```

- Live wall: **145.3s** (pipeline_shortcut; export 136.4s)
- SLA: PASS

## S4 — 附录 E5 @ 20260612

```bash
python3 -m backend.cli run --date 20260612 --skip-export
```

- chunk_stocks=**0**, batch_only=**5391**, batch_full=**118**
- Wall ~256s (run_id `ded70a43`)

## S5 — 真新日 @ 20260617 ✅

```bash
python3 -m backend.cli fetch --start 20260617 --end 20260617   # 5504 rows
python3 -m backend.cli run --date 20260617
```

- **Wall clock:** 1587s (~26.4min) — SLA PASS
- chunk=0, batch_only=5391, batch_append=925s
- export 5271 rows
- health_check PASS
- Log: `/tmp/run-20260617-newday.log`

## health_check

```bash
python3 scripts/health_check.py
```

- ✅ PASS (2026-06-17), Section J spec_stale=0, K oracle 200/200

## F.4 四条硬约束

| # | 判定 | 证据 |
|---|------|------|
| 1 ≤30min | ✅ | F.1 ~491s；F.2 145s；**F.3 1587s** |
| 2 质量 | ✅ | health_check（含 S5 跑后） |
| 3 最小计算 | ✅ | E5 chunk=0；`mode=week=` |
| 4 无全库 rebuild | ✅ | stale 子集 only |
