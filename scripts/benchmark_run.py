"""Wall-clock summary from ods_etl_log + optional live run SLA validation.

Usage:
    python scripts/benchmark_run.py --date 20260605
    python scripts/benchmark_run.py --date 20260605 --run
    python scripts/benchmark_run.py --date 20260605 --run --skip-export
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import DUCKDB_PATH, LOG_FILE

SLA_SEC = 1800
RUN_STEPS = (
    "run_fetch",
    "run_rebuild_dwd",
    "run_refresh_state",
    "calc_dws",
    "run_export",
)
LOG_GREP_HINTS = (
    "dwd.rebuild_incremental",
    "mode=week=",
    "dwd.qfq_update",
)


def _parse_completeness(raw):
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def _duration_sec(started_at: Optional[str], finished_at: Optional[str]) -> float:
    if not started_at or not finished_at:
        return 0.0
    try:
        t0 = datetime.fromisoformat(started_at)
        t1 = datetime.fromisoformat(finished_at)
    except ValueError:
        return 0.0
    return max(0.0, (t1 - t0).total_seconds())


def _fetch_step_row(con, step_name: str, analysis_date: str):
    """Latest successful log row for step, matching analysis_date in completeness JSON."""
    rows = con.execute(
        """
        SELECT started_at, finished_at, data_completeness
        FROM ods_etl_log
        WHERE step_name = ? AND status = 'success'
        ORDER BY started_at DESC
        LIMIT 50
        """,
        [step_name],
    ).fetchall()
    for started_at, finished_at, comp_raw in rows:
        comp = _parse_completeness(comp_raw)
        ad = comp.get("analysis_date") or comp.get("calc_date")
        if ad == analysis_date:
            return (started_at, finished_at, comp_raw)
    return None


def _print_log_summary(con, analysis_date: str, sla_sec: int) -> Tuple[float, Optional[Dict]]:
    total = 0.0
    calc_comp = None
    for step in RUN_STEPS:
        row = _fetch_step_row(con, step, analysis_date)
        if not row:
            print(f"{step}: (no log for {analysis_date})")
            continue
        started_at, finished_at, comp_raw = row
        dur = _duration_sec(started_at, finished_at)
        total += dur
        comp = _parse_completeness(comp_raw)
        extra = ""
        if step == "run_rebuild_dwd" and comp.get("skipped"):
            extra = " skipped=true"
        if step == "calc_dws":
            calc_comp = comp
            batch_only = comp.get("batch_only")
            chunk_stocks = comp.get("chunk_stocks")
            if batch_only is not None:
                extra += f" batch_only={batch_only}"
            if chunk_stocks is not None:
                extra += f" chunk_stocks={chunk_stocks}"
        print(f"{step}: {dur:.1f}s{extra}")
    print(f"TOTAL wall (logged steps): {total:.1f}s  (SLA target <= {sla_sec}s)")
    return total, calc_comp


def _grep_log_hints(analysis_date: str) -> None:
    print("\nLog grep hints (check tradeanalysis.log):")
    if not os.path.isfile(LOG_FILE):
        print(f"  log file not found: {LOG_FILE}")
        return
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"  could not read log: {exc}")
        return
    for pattern in LOG_GREP_HINTS:
        found = pattern in text
        marker = "FOUND" if found else "MISSING"
        print(f"  [{marker}] {pattern}")
    if analysis_date in text:
        print(f"  date {analysis_date} appears in log")
    else:
        print(f"  date {analysis_date} not found in log (may be stale log file)")


def _run_pipeline(analysis_date: str, skip_export: bool) -> Tuple[int, float]:
    cmd = [sys.executable, "-m", "backend.cli", "run", "--date", analysis_date]
    if skip_export:
        cmd.append("--skip-export")
    t0 = time.monotonic()
    rc = subprocess.call(cmd)
    elapsed = time.monotonic() - t0
    print(f"benchmark_run: elapsed={elapsed:.1f}s exit={rc}")
    return rc, elapsed


def main():
    parser = argparse.ArgumentParser(description="Daily pipeline benchmark + SLA gate")
    parser.add_argument("--date", required=True, help="Analysis date YYYYMMDD")
    parser.add_argument("--db", default=DUCKDB_PATH)
    parser.add_argument("--sla-sec", type=int, default=SLA_SEC)
    parser.add_argument("--run", action="store_true", help="Execute cli run and measure wall clock")
    parser.add_argument("--skip-export", action="store_true", help="Pass --skip-export to cli run")
    args = parser.parse_args()

    run_elapsed = None
    if args.run:
        rc, run_elapsed = _run_pipeline(args.date, args.skip_export)
        if rc != 0:
            sys.exit(rc)

    con = duckdb.connect(args.db, read_only=True)
    try:
        print(f"\nods_etl_log summary for {args.date}:")
        total, _calc_comp = _print_log_summary(con, args.date, args.sla_sec)
    finally:
        con.close()

    _grep_log_hints(args.date)

    if args.run:
        if run_elapsed is not None and run_elapsed > args.sla_sec:
            print(
                f"SLA FAIL: run elapsed {run_elapsed:.1f}s > {args.sla_sec}s",
                file=sys.stderr,
            )
            sys.exit(2)
        print("SLA PASS (run wall clock)")
        sys.exit(0)

    if total > args.sla_sec:
        print(
            f"SLA FAIL: logged total {total:.1f}s > {args.sla_sec}s",
            file=sys.stderr,
        )
        sys.exit(1)
    print("SLA PASS (logged steps)")
    sys.exit(0)


if __name__ == "__main__":
    main()
