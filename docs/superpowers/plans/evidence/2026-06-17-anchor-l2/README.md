# Anchor L2 remediation @ 20260616

## Timeline

| Step | Result |
|------|--------|
| L1 partial (4 DDE + MA refresh) | sample500 0 mismatch; health_check PASS |
| Full oracle pre-bulk | 5053 matched / **59 mismatched** |
| Bulk `repair-dde-trend` 59 stocks | ~5min CALC_FORCE_HARD |
| MA v2 fix 59 stocks (repair side-effect) | ma @60616 spec_stale=0 |
| Full oracle post-bulk | **5110 matched / 2 mismatched** |
| Remaining 2 (`603089.SH`, `300275.SZ`) | **oracle false positive** — stored=full compute=up, tail255=down (EMA60 warm-up) |

## Commands

```bash
# Bulk DDE repair (oracle-defined scope)
python3 -m backend.cli ops repair-dde-trend --date 20260616 --freq daily \
  --ts-code $(cat dde-mismatch-codes-pre-repair.txt | tr '\n' ' ')

# MA v2 side-effect fix
python3 -m backend.cli refresh --date 20260616 --indicator ma \
  --ts-code $(cat dde-mismatch-codes-pre-repair.txt | tr '\n' ' ')
```

## Acceptance @ 20260616

- `ops spec-status`: ma/dde/macd/volume @ anchor **0 stale**
- `health_check`: **PASS** (Section J+K)
- DDE sample500 oracle: **0 mismatch**
- DDE full oracle: 5110/5112; 2 edge cases = **audit script tail255 bug**, not DWS stale

## Follow-ups

1. Fix `audit_dde_trend_oracle.py` — **done (2026-06-17):** default full history, not tail255
2. P0: dde content invalidation after `net_amount_dc`/`circ_mv` patch + refresh_state — **done (2026-06-17):** commit `588d072`, plan M1–M3 closed
