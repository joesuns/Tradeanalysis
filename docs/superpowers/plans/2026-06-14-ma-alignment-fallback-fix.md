# MA Alignment v2 八格实现修复（Stay v2）

**Date:** 2026-06-14  
**Status:** 代码已实施 — **存量 DWS 待 `refresh --indicator ma` 运维**  
**Parent:** [`2026-06-08-ma-alignment-single-slope-fallback.md`](2026-06-08-ma-alignment-single-slope-fallback.md)  
**Related:** [`2026-06-14-calc-spec-version-governance.md`](2026-06-14-calc-spec-version-governance.md)

## 问题

`MACalculator.SPEC_VERSION=v2` 已发布，data-model §6.3 八格表已定稿，但 `calc_ma._compute_alignment` Layer 3 仍用旧 6 格 if/else，导致：

- `s5平+s10上`（多头位）→ 错标 `bull_strong`（应为 `bull_building`）
- `s5平+s10上`（空头位）→ 错标 `bear_weakening`（应为 `bear_building`）

## 定案（数据架构师 A）

- **Stay v2** — spec 八格表即 v2 契约；本次为 implementation bugfix，不 bump v3
- **存量刷新** — `refresh --indicator ma`（**非** `refresh-spec`，state 已 v2 时 refresh-spec 无效）

## 代码变更

| 文件 | 变更 |
|------|------|
| `backend/etl/calc_ma.py` | `_layer3_fallback_alignment` 八格查表 |
| `tests/test_etl/test_calc_ma.py` | 八格 matrix + 301205 回归 |
| `backend/db/schema.py` | `v_dq_spec_freshness` |
| `scripts/health_check.py` | Section J |
| `scripts/audit_ma_alignment_fallback.py` | 语义审计（前两项 = 0） |

## 运维（Gate 3）

```bash
python3 -m backend.cli refresh --date 20260612 --indicator ma
python3 -m scripts.audit_ma_alignment_fallback
python3 -m scripts.health_check   # Section J
python3 -m backend.cli export --date 20260612
```

**锚点：** `301205.SZ` @ `20260612` → `bear_building`, `spec_version=v2`

## 验收

- [x] pytest `test_calc_ma` + `test_append_calc` ma
- [ ] 实库 audit 前两项 = 0（refresh 后）
- [ ] health_check Section J PASS（refresh 后）
