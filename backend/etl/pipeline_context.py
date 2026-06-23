"""Shared pipeline context for run / refresh (Wave 1 contract)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from backend.fetch.fetch_result import FetchResult

logger = logging.getLogger(__name__)


@dataclass
class PipelineContext:
    analysis_date: str
    ts_codes: List[str]
    mode: str = "run"
    fetch_result: FetchResult = field(default_factory=FetchResult.empty)
    skip_dwd_calc: bool = False
    force_scope: bool = False
    indicator_filter: Optional[List[str]] = None
    pipeline_shortcut: bool = False

    @classmethod
    def from_fetch(
        cls,
        con,
        analysis_date: str,
        ts_codes: List[str],
        fetch_result,
        mode: str = "run",
        force_scope: bool = False,
        force_recalc: bool = False,
        indicator_filter: Optional[List[str]] = None,
    ) -> "PipelineContext":
        from backend.etl.pipeline_context import coerce_fetch_result

        fr = coerce_fetch_result(fetch_result)
        skip = False
        if mode == "run" and not force_scope:
            skip = compute_skip_dwd_calc(
                con, analysis_date, ts_codes, fr, force_recalc=force_recalc,
            )
        ctx = cls(
            analysis_date=analysis_date,
            ts_codes=ts_codes,
            mode=mode,
            fetch_result=fr,
            skip_dwd_calc=skip,
            force_scope=force_scope,
            indicator_filter=indicator_filter,
            pipeline_shortcut=skip,
        )
        return ctx

    @property
    def changed_codes(self) -> List[str]:
        return self.fetch_result.changed_codes

    def changed_codes_for_date(self, trade_date: Optional[str] = None) -> List[str]:
        td = trade_date or self.analysis_date
        return self.fetch_result.changed_codes_for_date(td)

    def to_completeness(self) -> dict:
        out = self.fetch_result.to_completeness()
        out.update({
            "analysis_date": self.analysis_date,
            "pipeline_shortcut": self.pipeline_shortcut,
            "skip_dwd_calc": self.skip_dwd_calc,
            "mode": self.mode,
        })
        return out


def compute_skip_dwd_calc(
    con,
    analysis_date: str,
    ts_codes: List[str],
    fetch_result,
    force_recalc: bool = False,
) -> bool:
    """True when run may skip DWD+calc (fetch still ran). P0-1A."""
    if force_recalc:
        logger.info(
            "pipeline L0 gate: skip_dwd_calc=false reason=force_recalc date=%s",
            analysis_date,
        )
        return False
    fr = coerce_fetch_result(fetch_result)
    from backend.etl.column_indicator_deps import fetch_blocks_dwd_calc

    if fetch_blocks_dwd_calc(fr):
        logger.info(
            "pipeline L0 gate: skip_dwd_calc=false reason=fetch_rows_written "
            "date=%s rows=%d",
            analysis_date, fr.rows_written,
        )
        return False
    if fr.rows_written > 0:
        affected = sorted({ev[3] for ev in fr.changed_field_events})
        logger.info(
            "pipeline L0 gate: cosmetic ODS drift ignored date=%s rows=%d "
            "affected=%s",
            analysis_date, fr.rows_written, affected,
        )
    from backend.etl.calc_gate import has_prior_calc_snapshot
    from backend.etl.calc_spec_gate import has_spec_stale_indicators
    from backend.etl.orchestrator import find_stale_dwd_codes

    if has_spec_stale_indicators(con, analysis_date):
        logger.info(
            "pipeline L0 gate: skip_dwd_calc=false reason=spec_stale date=%s",
            analysis_date,
        )
        return False
    # rows_written==0: fetch already compared API vs ODS; persistent stale ODS
    # (halt/suspend) is structural — calc skip_log handles them, not a rerun blocker.
    stale_dwd = find_stale_dwd_codes(con, ts_codes, analysis_date)
    if stale_dwd:
        logger.info(
            "pipeline L0 gate: skip_dwd_calc=false reason=stale_dwd date=%s count=%d",
            analysis_date, len(stale_dwd),
        )
        return False
    if not has_prior_calc_snapshot(con, analysis_date):
        logger.info(
            "pipeline L0 gate: skip_dwd_calc=false reason=no_prior_calc date=%s",
            analysis_date,
        )
        return False
    logger.info(
        "pipeline L0 gate: skip_dwd_calc=true reason=eligible date=%s stocks=%d",
        analysis_date, len(ts_codes),
    )
    return True


def coerce_fetch_result(value) -> FetchResult:
    if isinstance(value, FetchResult):
        return value
    if value is None:
        return FetchResult.empty()
    return FetchResult(rows_written=int(value))
