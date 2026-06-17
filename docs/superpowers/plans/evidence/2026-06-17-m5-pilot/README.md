# M5 Pilot Evidence — 20260616

## M5.1 备份

- `data/tradeanalysis.pre-m5-20260617.duckdb`（28GB）

## M5.3 真跑（50 股 MACD refresh-spec）

- 股单：`2026-06-17-m5-pilot-codes.txt`（000001.SZ … 000151.SZ）
- Force 方式：state + DWS `@calc_date=20260616` spec→v1 后 `calc --refresh-spec macd`
- 结果：MACD 日 50 股 / ~5s / 12,250 行；MACD 周 50 股 / ~4s / 12,250 行
- 日志：`/tmp/m5-pilot-refresh-macd-force.log`

## M5.4 E1 Oracle（stored vs expanding）

```bash
python3 scripts/audit_macd_b4_oracle.py --date 20260616 --freq both \
  --ts-code-file docs/superpowers/plans/evidence/2026-06-17-m5-pilot-codes.txt
```

| 路由 | 对比 bar 数 | mismatch |
|------|-------------|----------|
| daily `trend`/`turning_point` | 12,250 | **0** |
| weekly `trend`/`turning_point` | 12,250 | **0** |
| **合计** | **24,500** | **0** |

**E1：Pass**（50 股 write window 100% match）

输出：`2026-06-17-m5-e1-oracle.txt`

## M5.5 spec-status + health_check Section J

- `ops spec-status --date 20260616`：`macd/volume @ 20260616 anchor stale=0`；weekly anchor 全 fresh
- `health_check` Section J（export anchor）：
  - macd/volume daily+weekly spec_stale：**0**
  - ma alignment 软层：`bull_strong+s5flat+s10up=20`（已知 WIP，非 P2+ 回归）
- 证据：`spec-status-20260616.txt`、`health-check.txt`

## M5.6 E3 同日复跑

见 `e3-rerun.txt`：pilot ratio=0.94；全市场 ratio=0.89 → **Pass**

## E2 pilot spec

50 股 `@20260616`：macd stale=0，volume stale=0

## T2 CALC_B4_WEEKLY_FAST=0 spot-check

3 股末周 bar expanding vs stored：**0 mismatch**
