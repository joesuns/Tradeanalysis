"""Read-only spec freshness report for ops."""
import logging
from typing import List, Optional, Sequence, Tuple

from backend.etl.calc_spec_gate import resolve_weekly_anchor_trade_date

logger = logging.getLogger(__name__)

Row = Tuple[str, str, str, int, int, int, str]


def fetch_spec_freshness_rows(con, trade_date: str) -> List[Row]:
    """Return v_dq_spec_freshness rows for daily anchor and weekly week-end anchor."""
    weekly_anchor = resolve_weekly_anchor_trade_date(con, trade_date) or ""
    anchors = [trade_date]
    if weekly_anchor and weekly_anchor not in anchors:
        anchors.append(weekly_anchor)
    placeholders = ",".join("?" * len(anchors))
    rows = con.execute(
        f"""
        SELECT indicator, freq, anchor_trade_date, total, spec_ok, spec_stale, expected_spec
        FROM v_dq_spec_freshness
        WHERE anchor_trade_date IN ({placeholders})
        ORDER BY indicator, freq
        """,
        anchors,
    ).fetchall()
    return [tuple(r) for r in rows]


def format_spec_status_table(rows: Sequence[Row]) -> str:
    """Human-readable table for CLI output."""
    if not rows:
        return "No v_dq_spec_freshness rows for anchor date(s)."
    header = (
        f"{'indicator':<14} {'freq':<7} {'anchor':<10} "
        f"{'total':>7} {'ok':>7} {'stale':>7} {'expected':>8}"
    )
    lines = [header, "-" * len(header)]
    for indicator, freq, anchor, total, ok, stale, expected in rows:
        lines.append(
            f"{indicator:<14} {freq:<7} {anchor:<10} "
            f"{int(total):>7} {int(ok):>7} {int(stale):>7} {expected:>8}"
        )
    return "\n".join(lines)


def suggest_refresh_spec(rows: Sequence[Row], trade_date: str) -> str:
    """Suggest calc --refresh-spec when any row has spec_stale > 0."""
    stale_inds = sorted({r[0] for r in rows if int(r[5]) > 0})
    if not stale_inds:
        return ""
    return (
        "Suggested: python3 -m backend.cli calc --refresh-spec "
        + ",".join(stale_inds)
        + f" --date {trade_date}"
    )


def cmd_spec_status(con, trade_date: str) -> None:
    """CLI entry: print spec freshness for analysis anchor date."""
    rows = fetch_spec_freshness_rows(con, trade_date)
    print(format_spec_status_table(rows))
    hint = suggest_refresh_spec(rows, trade_date)
    if hint:
        print()
        print(hint)
    weekly = resolve_weekly_anchor_trade_date(con, trade_date)
    logger.info(
        "spec-status: trade_date=%s weekly_anchor=%s rows=%d",
        trade_date, weekly, len(rows),
    )
