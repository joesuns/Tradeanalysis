"""Post backfill-dde-meta closure: refresh-state → invalidate DDE weekly DWS → calc --force."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import List, Optional

logger = logging.getLogger(__name__)


def purge_dde_daily_history(
    con,
    ts_codes: List[str],
) -> int:
    """Remove all DDE daily DWS snapshots for ts_codes (targeted deep repair).

    Narrow scope: explicit ts_codes only — never call without a stock list.
    Used before CALC_FORCE_HARD to drop stale superseded trend rows that
    v_*_latest would otherwise keep serving.
    """
    if not ts_codes:
        return 0
    ph = ",".join(["?"] * len(ts_codes))
    before = con.execute(
        f"SELECT COUNT(*) FROM dws_dde_daily WHERE ts_code IN ({ph})",
        list(ts_codes),
    ).fetchone()[0]
    if before:
        con.execute(
            f"DELETE FROM dws_dde_daily WHERE ts_code IN ({ph})",
            list(ts_codes),
        )
    return int(before)


def invalidate_dde_daily_snapshots(
    con,
    calc_date: str,
    ts_codes: Optional[List[str]] = None,
) -> int:
    """Remove DDE daily DWS rows for calc_date so daily trend is recomputed.

    Narrow scope: one calc_date batch only (not full-table DELETE).
    """
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        before = con.execute(
            f"""
            SELECT COUNT(*) FROM dws_dde_daily
            WHERE calc_date = ? AND ts_code IN ({ph})
            """,
            [calc_date] + list(ts_codes),
        ).fetchone()[0]
        if before:
            con.execute(
                f"""
                DELETE FROM dws_dde_daily
                WHERE calc_date = ? AND ts_code IN ({ph})
                """,
                [calc_date] + list(ts_codes),
            )
        return int(before)

    before = con.execute(
        "SELECT COUNT(*) FROM dws_dde_daily WHERE calc_date = ?",
        [calc_date],
    ).fetchone()[0]
    if before:
        con.execute(
            "DELETE FROM dws_dde_daily WHERE calc_date = ?",
            [calc_date],
        )
    return int(before)


def invalidate_dde_daily_calc_state(
    con,
    ts_codes: Optional[List[str]] = None,
) -> int:
    """Drop dde/daily calc routing state so chunk path can FULL if needed."""
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        before = con.execute(
            f"""
            SELECT COUNT(*) FROM dws_calc_state
            WHERE indicator = 'dde' AND freq = 'daily'
              AND ts_code IN ({ph})
            """,
            list(ts_codes),
        ).fetchone()[0]
        if before:
            con.execute(
                f"""
                DELETE FROM dws_calc_state
                WHERE indicator = 'dde' AND freq = 'daily'
                  AND ts_code IN ({ph})
                """,
                list(ts_codes),
            )
        return int(before)

    before = con.execute(
        """
        SELECT COUNT(*) FROM dws_calc_state
        WHERE indicator = 'dde' AND freq = 'daily'
        """
    ).fetchone()[0]
    if before:
        con.execute(
            """
            DELETE FROM dws_calc_state
            WHERE indicator = 'dde' AND freq = 'daily'
            """
        )
    return int(before)


def invalidate_dde_weekly_snapshots(
    con,
    calc_date: str,
    ts_codes: Optional[List[str]] = None,
) -> int:
    """Remove DDE weekly DWS rows for calc_date so weekly trend is recomputed.

    Narrow scope: one calc_date batch only (not full-table DELETE).
    """
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        before = con.execute(
            f"""
            SELECT COUNT(*) FROM dws_dde_weekly
            WHERE calc_date = ? AND ts_code IN ({ph})
            """,
            [calc_date] + list(ts_codes),
        ).fetchone()[0]
        if before:
            con.execute(
                f"""
                DELETE FROM dws_dde_weekly
                WHERE calc_date = ? AND ts_code IN ({ph})
                """,
                [calc_date] + list(ts_codes),
            )
        return int(before)

    before = con.execute(
        "SELECT COUNT(*) FROM dws_dde_weekly WHERE calc_date = ?",
        [calc_date],
    ).fetchone()[0]
    if before:
        con.execute(
            "DELETE FROM dws_dde_weekly WHERE calc_date = ?",
            [calc_date],
        )
    return int(before)


def invalidate_dde_weekly_calc_state(
    con,
    ts_codes: Optional[List[str]] = None,
) -> int:
    """Drop dde/weekly calc routing state so chunk path can FULL if needed."""
    if ts_codes:
        ph = ",".join(["?"] * len(ts_codes))
        before = con.execute(
            f"""
            SELECT COUNT(*) FROM dws_calc_state
            WHERE indicator = 'dde' AND freq = 'weekly'
              AND ts_code IN ({ph})
            """,
            list(ts_codes),
        ).fetchone()[0]
        if before:
            con.execute(
                f"""
                DELETE FROM dws_calc_state
                WHERE indicator = 'dde' AND freq = 'weekly'
                  AND ts_code IN ({ph})
                """,
                list(ts_codes),
            )
        return int(before)

    before = con.execute(
        """
        SELECT COUNT(*) FROM dws_calc_state
        WHERE indicator = 'dde' AND freq = 'weekly'
        """
    ).fetchone()[0]
    if before:
        con.execute(
            """
            DELETE FROM dws_calc_state
            WHERE indicator = 'dde' AND freq = 'weekly'
            """
        )
    return int(before)


def prepare_dde_daily_recalc(
    con,
    calc_date: str,
    ts_codes: Optional[List[str]] = None,
    dry_run: bool = False,
    purge_history: bool = False,
) -> dict:
    """Invalidate dde daily DWS/state (in-process, before calc subprocess)."""
    from backend.etl.calc_gate import assert_calc_date_ready, resolve_effective_calc_date
    from backend.config import CALC_STRICT_DATE
    from backend.db.schema import ensure_calc_state_table
    from backend.fetch.ods_daily import get_all_active_codes

    ensure_calc_state_table(con)
    if CALC_STRICT_DATE:
        assert_calc_date_ready(con, calc_date, strict=True)
    else:
        calc_date = resolve_effective_calc_date(con, calc_date, cap_to_ods=True)

    universe = ts_codes if ts_codes else get_all_active_codes(con)
    logger.info(
        "repair_dde_trend: daily prepare stocks=%d calc_date=%s dry_run=%s",
        len(universe), calc_date, dry_run,
    )
    stats = {
        "calc_date": calc_date,
        "stocks": len(universe),
        "dry_run": dry_run,
        "purge_history": purge_history,
        "dde_daily_history_purged": 0,
        "dde_daily_rows_deleted": 0,
        "dde_daily_state_deleted": 0,
    }
    if not dry_run:
        if purge_history:
            if not ts_codes:
                raise ValueError("purge_history requires --ts-code (refusing full-market purge)")
            stats["dde_daily_history_purged"] = purge_dde_daily_history(
                con, list(ts_codes),
            )
        stats["dde_daily_rows_deleted"] = invalidate_dde_daily_snapshots(
            con, calc_date, ts_codes=ts_codes,
        )
        stats["dde_daily_state_deleted"] = invalidate_dde_daily_calc_state(
            con, ts_codes=ts_codes,
        )
    return stats


def prepare_dde_weekly_recalc(
    con,
    calc_date: str,
    ts_codes: Optional[List[str]] = None,
    dry_run: bool = False,
) -> dict:
    """refresh-state + invalidate dde weekly DWS/state (in-process, before calc subprocess)."""
    from backend.etl.calc_gate import assert_calc_date_ready, resolve_effective_calc_date
    from backend.etl.calc_state_refresh import refresh_calc_state_fingerprints
    from backend.config import CALC_STRICT_DATE
    from backend.db.schema import ensure_calc_state_table
    from backend.fetch.ods_daily import get_all_active_codes

    ensure_calc_state_table(con)
    if CALC_STRICT_DATE:
        assert_calc_date_ready(con, calc_date, strict=True)
    else:
        calc_date = resolve_effective_calc_date(con, calc_date, cap_to_ods=True)

    universe = ts_codes if ts_codes else get_all_active_codes(con)
    logger.info(
        "backfill_dde_recalc: refresh-state stocks=%d calc_date=%s dry_run=%s",
        len(universe), calc_date, dry_run,
    )
    refresh_summary = refresh_calc_state_fingerprints(
        con, universe, calc_date, dry_run=dry_run,
    )
    stats = {
        "calc_date": calc_date,
        "stocks": len(universe),
        "dry_run": dry_run,
        "refresh_state": refresh_summary,
        "dde_weekly_rows_deleted": 0,
        "dde_weekly_state_deleted": 0,
    }
    if not dry_run:
        stats["dde_weekly_rows_deleted"] = invalidate_dde_weekly_snapshots(
            con, calc_date, ts_codes=ts_codes,
        )
        stats["dde_weekly_state_deleted"] = invalidate_dde_weekly_calc_state(
            con, ts_codes=ts_codes,
        )
    return stats


def run_calc_force_hard_subprocess(
    calc_date: str,
    ts_codes: Optional[List[str]] = None,
) -> None:
    """Spawn calc --force in a fresh process (CALC_* env read at import)."""
    cmd = [
        sys.executable, "-m", "backend.cli", "calc",
        "--date", calc_date, "--force",
    ]
    if ts_codes:
        cmd.append("--ts-code")
        cmd.extend(ts_codes)
    env = os.environ.copy()
    env["CALC_FORCE_HARD"] = "1"
    env["CALC_FAST_SKIP"] = "0"
    env["CALC_FORCE_BATCH_REUSE"] = "0"
    logger.info(
        "backfill_dde_recalc: calc --force subprocess calc_date=%s stocks=%s",
        calc_date,
        len(ts_codes) if ts_codes else "all",
    )
    subprocess.run(cmd, check=True, env=env)


def run_backfill_dde_recalc_closure(
    con,
    calc_date: str,
    ts_codes: Optional[List[str]] = None,
    dry_run: bool = False,
) -> dict:
    """Full closure: prepare (in-process) then calc subprocess.

    Caller must close ``con`` before this if DuckDB single-writer conflicts;
    this function closes nothing — CLI closes then calls ``run_calc_force_hard_subprocess``.
    """
    stats = prepare_dde_weekly_recalc(
        con, calc_date, ts_codes=ts_codes, dry_run=dry_run,
    )
    if dry_run:
        stats["calc"] = {"skipped": True, "reason": "dry_run"}
        return stats

    from backend.db.connection import run_checkpoint
    run_checkpoint(con)
    # Caller closes con, then:
    stats["calc"] = {"pending_subprocess": True}
    return stats
