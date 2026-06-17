# Pipeline 30min 终验证据（2026-06-17）

**Plan：** `docs/superpowers/plans/2026-06-09-pipeline-30min-optimization.md` 附录 F  
**分支：** `perf/b2-polyfit-vectorization` @ `e556538`  
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

## S5 — 真新日 @ 20260617

```bash
python3 -m backend.cli run --date 20260617
```

- **Blocked:** fetch 0 rows; `calc_date 20260617 > ods_max 20260616`
- Steady-state new-day reference: **20260615 first run ~491s** (附录 F.1)

## health_check

```bash
python3 scripts/health_check.py
```

- ✅ PASS (2026-06-17), Section J spec_stale=0, K oracle 200/200
