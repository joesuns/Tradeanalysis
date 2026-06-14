"""Indicator-level calc work queue — decouple routing from whole-stock execution."""
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

Key = Tuple[str, str]  # (indicator_name, freq)
ModeMap = Dict[Key, Tuple[str, list]]  # mode, new_bars
StockModes = Dict[str, ModeMap]


@dataclass
class CalcWorkQueue:
    skip_items: List[Tuple[str, Key]] = field(default_factory=list)
    append_items: List[Tuple[str, Key, list]] = field(default_factory=list)
    full_items: List[Tuple[str, Key]] = field(default_factory=list)

    @property
    def full_stocks(self) -> Set[str]:
        return {ts for ts, _ in self.full_items}

    @property
    def append_stocks(self) -> Set[str]:
        return {ts for ts, _, _ in self.append_items}


def build_work_queue(
    stock_modes: StockModes,
    completed_keys: Set[Tuple[str, str, str]] = None,
) -> CalcWorkQueue:
    """Build indicator-level queues from preflight modes.

    completed_keys: {(ts_code, indicator_name, freq)} already handled by batch phase.
    """
    if completed_keys is None:
        completed_keys = set()

    q = CalcWorkQueue()
    for ts_code, modes in stock_modes.items():
        for (indicator_name, freq), (mode, new_bars) in modes.items():
            if (ts_code, indicator_name, freq) in completed_keys:
                continue
            key = (indicator_name, freq)
            if mode == "SKIP":
                q.skip_items.append((ts_code, key))
            elif mode == "APPEND":
                q.append_items.append((ts_code, key, new_bars))
            else:
                q.full_items.append((ts_code, key))
    return q


def group_by_indicator(items) -> Dict[Key, List[str]]:
    """Group queue items by (indicator, freq) -> [ts_codes]."""
    groups: Dict[Key, List[str]] = {}
    for row in items:
        ts_code = row[0]
        key = row[1]
        groups.setdefault(key, []).append(ts_code)
    return groups
