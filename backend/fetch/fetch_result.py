"""Fetch layer result contract for change-driven pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple


@dataclass
class FetchResult:
    """Outcome of an ODS fetch pass (API compare + selective write)."""

    api_rows: int = 0
    rows_written: int = 0
    rows_unchanged: int = 0
    changed_pairs: List[Tuple[str, str]] = field(default_factory=list)
    # (ts_code, trade_date, ods_table, column, is_insert)
    changed_field_events: List[Tuple[str, str, str, str, bool]] = field(
        default_factory=list,
    )

    def __int__(self) -> int:
        """Backward compat: int(fetch_result) == rows_written."""
        return self.rows_written

    @classmethod
    def empty(cls) -> "FetchResult":
        return cls()

    def merge(self, other: "FetchResult") -> "FetchResult":
        seen: Set[Tuple[str, str]] = set(self.changed_pairs)
        merged_pairs = list(self.changed_pairs)
        for pair in other.changed_pairs:
            if pair not in seen:
                seen.add(pair)
                merged_pairs.append(pair)
        seen_events: Set[Tuple[str, str, str, str, bool]] = set(
            self.changed_field_events,
        )
        merged_events = list(self.changed_field_events)
        for ev in other.changed_field_events:
            if ev not in seen_events:
                seen_events.add(ev)
                merged_events.append(ev)
        return FetchResult(
            api_rows=self.api_rows + other.api_rows,
            rows_written=self.rows_written + other.rows_written,
            rows_unchanged=self.rows_unchanged + other.rows_unchanged,
            changed_pairs=merged_pairs,
            changed_field_events=merged_events,
        )

    @property
    def changed_codes(self) -> List[str]:
        return sorted({code for code, _ in self.changed_pairs})

    def changed_codes_by_date(self) -> Dict[str, List[str]]:
        by_date: Dict[str, Set[str]] = {}
        for code, td in self.changed_pairs:
            by_date.setdefault(td, set()).add(code)
        return {d: sorted(codes) for d, codes in sorted(by_date.items())}

    def changed_codes_for_date(self, trade_date: str) -> List[str]:
        return self.changed_codes_by_date().get(trade_date, [])

    @property
    def changed_codes_count(self) -> int:
        return len(self.changed_codes)

    def to_completeness(self) -> dict:
        affected_cols = sorted({ev[3] for ev in self.changed_field_events})
        return {
            "ods_api_rows": self.api_rows,
            "ods_rows_written": self.rows_written,
            "ods_rows_unchanged": self.rows_unchanged,
            "changed_codes_count": self.changed_codes_count,
            "changed_field_events_count": len(self.changed_field_events),
            "affected_ods_columns": affected_cols,
        }
