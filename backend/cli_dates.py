"""CLI date / date-range resolution for run, export, refresh."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)


def resolve_trade_date(date: str = None) -> str:
    """Return YYYYMMDD; default today when date is None."""
    if date:
        return date
    return datetime.now().strftime("%Y%m%d")


def ensure_trade_date(con, date: str) -> str:
    """Rollback to nearest trading day on or before date."""
    row = con.execute(
        "SELECT MAX(trade_date) FROM dim_date "
        "WHERE trade_date <= ? AND is_trade_day = 1",
        (date,),
    ).fetchone()
    if not row or not row[0]:
        return date
    trade_date = row[0]
    if trade_date != date:
        logger.warning("%s is not a trading day, using %s instead", date, trade_date)
    return trade_date


def add_date_range_arguments(parser) -> None:
    """Add --date | (--from + --to) to a subparser."""
    parser.add_argument("--date", help="Analysis date YYYYMMDD (default: today)")
    parser.add_argument(
        "--from", dest="date_from", metavar="FROM",
        help="Range start YYYYMMDD (requires --to; mutually exclusive with --date)",
    )
    parser.add_argument(
        "--to", dest="date_to", metavar="TO",
        help="Range end YYYYMMDD (requires --from)",
    )


def expand_trade_dates(con, start: str, end: str) -> List[str]:
    """Trading days in [start, end] from dim_date (inclusive, sorted)."""
    start_td = ensure_trade_date(con, start)
    end_td = ensure_trade_date(con, end)
    if start_td > end_td:
        raise ValueError(f"--from {start_td} is after --to {end_td}")
    rows = con.execute(
        """
        SELECT trade_date FROM dim_date
        WHERE is_trade_day = 1
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        [start_td, end_td],
    ).fetchall()
    if not rows:
        raise ValueError(f"No trading days in dim_date for {start_td}~{end_td}")
    return [r[0] for r in rows]


def resolve_cli_dates(con, args, default_today: bool = True) -> List[str]:
    """Resolve args to an ordered list of trading days."""
    has_date = getattr(args, "date", None) is not None
    date_from = getattr(args, "date_from", None)
    date_to = getattr(args, "date_to", None)
    has_from = date_from is not None
    has_to = date_to is not None

    if has_date and (has_from or has_to):
        raise ValueError("--date is mutually exclusive with --from/--to")
    if has_from ^ has_to:
        raise ValueError("--from and --to must be used together")

    if has_from:
        return expand_trade_dates(con, date_from, date_to)

    if has_date or default_today:
        single = ensure_trade_date(
            con, resolve_trade_date(getattr(args, "date", None)),
        )
        return [single]

    raise ValueError("Specify --date or --from/--to")


def run_date_range_loop(
    dates: List[str],
    fn: Callable[[str], None],
    continue_on_error: bool = False,
    label: str = "cli",
) -> Dict[str, list]:
    """Run fn(date) sequentially; fail-fast unless continue_on_error."""
    progress: Dict[str, list] = {"ok": [], "failed": []}
    total = len(dates)
    for i, trade_date in enumerate(dates, start=1):
        logger.info("progress %s.date_range: %d/%d | %s", label, i, total, trade_date)
        try:
            fn(trade_date)
            progress["ok"].append(trade_date)
        except Exception as exc:
            logger.exception("%s failed on %s", label, trade_date)
            progress["failed"].append({"date": trade_date, "error": str(exc)})
            if not continue_on_error:
                progress["aborted_at"] = trade_date
                raise
    return progress
