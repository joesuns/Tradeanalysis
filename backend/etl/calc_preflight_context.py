"""CalcPreflightContext — run→calc handoff for refresh tails and preflight modes."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

Key = Tuple[str, str]  # (indicator_name, freq)
ModeEntry = Tuple[str, list]  # (mode, new_bars)

_RUN_CTX: Optional["CalcPreflightContext"] = None


@dataclass
class CalcPreflightContext:
    calc_date: str
    source: str  # "refresh_state" | "cold"
    stale_codes: List[str]
    state_map: Dict[Tuple[str, str, str], dict]
    daily_tails: dict
    weekly_tails: dict
    dde_daily: dict
    dde_weekly: dict
    stock_modes: Dict[str, Dict[Key, ModeEntry]]
    fp_cache_by_stock: Dict[str, Dict[Key, str]]
    refresh_summary: Dict[str, Any] = field(default_factory=dict)


def set_run_preflight_context(ctx: CalcPreflightContext) -> None:
    global _RUN_CTX
    _RUN_CTX = ctx


def pop_run_preflight_context() -> Optional[CalcPreflightContext]:
    global _RUN_CTX
    ctx = _RUN_CTX
    _RUN_CTX = None
    return ctx


def slice_context_for_codes(
    ctx: CalcPreflightContext,
    calc_codes: List[str],
) -> CalcPreflightContext:
    code_set = set(calc_codes)
    return CalcPreflightContext(
        calc_date=ctx.calc_date,
        source=ctx.source,
        stale_codes=[c for c in ctx.stale_codes if c in code_set],
        state_map={
            k: v for k, v in ctx.state_map.items() if k[0] in code_set
        },
        daily_tails={c: ctx.daily_tails[c] for c in calc_codes if c in ctx.daily_tails},
        weekly_tails={c: ctx.weekly_tails[c] for c in calc_codes if c in ctx.weekly_tails},
        dde_daily={c: ctx.dde_daily[c] for c in calc_codes if c in ctx.dde_daily},
        dde_weekly={c: ctx.dde_weekly[c] for c in calc_codes if c in ctx.dde_weekly},
        stock_modes={c: ctx.stock_modes[c] for c in calc_codes if c in ctx.stock_modes},
        fp_cache_by_stock={
            c: ctx.fp_cache_by_stock[c] for c in calc_codes if c in ctx.fp_cache_by_stock
        },
        refresh_summary=dict(ctx.refresh_summary),
    )


def merge_context_patch(
    ctx: Optional[CalcPreflightContext],
    patch_codes: List[str],
    patch_bundle: Dict[str, Any],
    calc_date: str,
) -> CalcPreflightContext:
    """Merge refresh artifacts for patch_codes into existing ctx (copy-on-write)."""
    if ctx is None:
        return build_context_from_refresh(
            calc_date=calc_date,
            stale_codes=list(patch_codes),
            summary=patch_bundle.get("summary", {}),
            state_map=patch_bundle.get("state_map", {}),
            tails_bundle=patch_bundle,
        )

    patch_set = set(patch_codes)
    daily_tails = dict(ctx.daily_tails)
    weekly_tails = dict(ctx.weekly_tails)
    dde_daily = dict(ctx.dde_daily)
    dde_weekly = dict(ctx.dde_weekly)
    stock_modes = dict(ctx.stock_modes)
    fp_cache = dict(ctx.fp_cache_by_stock)
    state_map = dict(ctx.state_map)

    for c in patch_codes:
        if c in patch_bundle.get("daily_tails", {}):
            daily_tails[c] = patch_bundle["daily_tails"][c]
        if c in patch_bundle.get("weekly_tails", {}):
            weekly_tails[c] = patch_bundle["weekly_tails"][c]
        if c in patch_bundle.get("dde_daily", {}):
            dde_daily[c] = patch_bundle["dde_daily"][c]
        if c in patch_bundle.get("dde_weekly", {}):
            dde_weekly[c] = patch_bundle["dde_weekly"][c]
        if c in patch_bundle.get("stock_modes", {}):
            stock_modes[c] = patch_bundle["stock_modes"][c]
        if c in patch_bundle.get("fp_cache_by_stock", {}):
            fp_cache[c] = patch_bundle["fp_cache_by_stock"][c]

    for k, v in patch_bundle.get("state_map", {}).items():
        if k[0] in patch_set:
            state_map[k] = v

    stale = list(dict.fromkeys(list(ctx.stale_codes) + list(patch_codes)))
    summary = dict(ctx.refresh_summary)
    summary["merged_patch"] = list(patch_codes)

    return CalcPreflightContext(
        calc_date=ctx.calc_date,
        source="refresh_state",
        stale_codes=stale,
        state_map=state_map,
        daily_tails=daily_tails,
        weekly_tails=weekly_tails,
        dde_daily=dde_daily,
        dde_weekly=dde_weekly,
        stock_modes=stock_modes,
        fp_cache_by_stock=fp_cache,
        refresh_summary=summary,
    )


def build_context_from_refresh(
    calc_date: str,
    stale_codes: List[str],
    summary: Dict[str, Any],
    state_map: Dict[Tuple[str, str, str], dict],
    tails_bundle: Dict[str, Any],
) -> CalcPreflightContext:
    """Build CalcPreflightContext from refresh_calc_state_fingerprints artifacts."""
    return CalcPreflightContext(
        calc_date=calc_date,
        source="refresh_state",
        stale_codes=list(stale_codes),
        state_map=dict(state_map),
        daily_tails=tails_bundle["daily_tails"],
        weekly_tails=tails_bundle["weekly_tails"],
        dde_daily=tails_bundle["dde_daily"],
        dde_weekly=tails_bundle["dde_weekly"],
        stock_modes=tails_bundle["stock_modes"],
        fp_cache_by_stock=tails_bundle["fp_cache_by_stock"],
        refresh_summary=dict(summary),
    )
