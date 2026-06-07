"""RecalcSpec registry — single source of truth for recalc window widths."""
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class RecalcSpec:
    lookback: int
    seed: int = 0
    event_tail: int = 0
    min_rows: int = 0

    @property
    def total(self) -> int:
        return self.lookback + self.seed + self.event_tail


def resolve_recalc_bars(specs: List[RecalcSpec], safety: int = 5) -> int:
    """Aggregate max(total) across specs plus safety margin."""
    if not specs:
        return safety
    return max(s.total for s in specs) + safety


def collect_specs(freq: str) -> List[RecalcSpec]:
    """Collect RecalcSpec from all registered calculators for given freq."""
    from backend.etl.orchestrator import CALCULATORS

    attr = "RECALC_SPEC_DAILY" if freq == "daily" else "RECALC_SPEC_WEEKLY"
    specs = []
    for cls in CALCULATORS:
        spec = getattr(cls, attr, None)
        if spec is not None:
            specs.append(spec)
    return specs


def resolve_warmup_tdays() -> int:
    """Derive daily warmup from registry: max(min_rows, lookback)."""
    specs = collect_specs("daily")
    return max(max(s.min_rows, s.lookback) for s in specs)


def resolve_weekly_warmup_weeks() -> int:
    """Derive weekly fetch warmup: volume pct_rank (120w), not PP recalc width (250w)."""
    specs = collect_specs("weekly")
    gate_specs = [s for s in specs if s.lookback <= 120]
    return max(s.lookback for s in gate_specs)


def resolve_load_start(con, recalc_start: str, freq: str) -> str:
    """Load from (max lookback - 1) bars before recalc_start for seed correctness."""
    specs = collect_specs(freq)
    extra = max(s.lookback for s in specs) - 1
    if extra <= 0:
        return recalc_start
    if freq == "weekly":
        row = con.execute("""
            SELECT trade_date FROM (
                SELECT trade_date,
                       ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
                FROM dim_date
                WHERE is_trade_day = 1 AND is_week_end = 1 AND trade_date < ?
            ) WHERE rn = ?
        """, [recalc_start, extra]).fetchone()
    else:
        row = con.execute("""
            SELECT trade_date FROM (
                SELECT trade_date,
                       ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
                FROM dim_date
                WHERE is_trade_day = 1 AND trade_date < ?
            ) WHERE rn = ?
        """, [recalc_start, extra]).fetchone()
    return row[0] if row else recalc_start
