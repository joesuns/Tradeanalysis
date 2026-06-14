"""ETL error grading and audit logging.

Provides:
    log_etl_start()  — INSERT a "running" row, return (log_id, start_time)
    log_etl_end()    — UPDATE the row with duration, status, row_count, error_msg
    log_etl_error()  — Convenience: log a step as "failed" with full traceback
    check_data_completeness() — compare ODS table freshness
"""

import json
import logging
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def log_etl_start(con, step_name: str,
                  min_trade_date: Optional[str] = None,
                  max_trade_date: Optional[str] = None) -> tuple:
    """Insert a 'running' row into ods_etl_log. Returns (log_id, start_time_monotonic).

    Use with log_etl_end() to capture real wall-clock duration.
    """
    log_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count, error_msg, data_completeness)
           VALUES (?, ?, ?, '', 'running', 0, '', '')""",
        (log_id, step_name, now_iso),
    )
    logger.info(f"ETL {step_name} — started")
    return log_id, time.monotonic()


def log_etl_end(con, log_id: str, step_name: str, start_time: float,
                status: str, row_count: int = 0, error_msg: str = "",
                data_completeness: Optional[dict] = None,
                min_trade_date: Optional[str] = None,
                max_trade_date: Optional[str] = None):
    """Finalize an ETL step with duration, status, and optional error/audit data.

    If status is 'failed' or 'degraded', also emits a warning log.
    """
    duration_ms = round((time.monotonic() - start_time) * 1000)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    comp = json.dumps(data_completeness) if data_completeness else ""

    con.execute(
        """UPDATE ods_etl_log
           SET finished_at = ?, status = ?, row_count = ?, error_msg = ?,
               data_completeness = ?
           WHERE id = ?""",
        (now_iso, status, row_count, error_msg or "", comp, log_id),
    )

    if status == "failed":
        err_summary = error_msg.split("\n")[0] if error_msg else ""
        logger.error("ETL %s: failed (%dms) — %s",
                     step_name, duration_ms, err_summary)
    elif status == "degraded":
        logger.warning("ETL %s: degraded (%dms) — %s",
                       step_name, duration_ms,
                       error_msg[:200] if error_msg else "")
    else:
        logger.info("ETL %s: %s (%dms, %d rows)",
                    step_name, status, duration_ms, row_count)


def log_etl_error(con, log_id: str, step_name: str, start_time: float,
                  row_count: int, exception: Exception,
                  min_trade_date: Optional[str] = None,
                  max_trade_date: Optional[str] = None):
    """Log a step as 'failed' — one-line ERROR to logger, full traceback to DB."""
    tb = traceback.format_exc()
    duration_ms = round((time.monotonic() - start_time) * 1000)
    logger.error("ETL %s — FAILED: %s (%dms)",
                 step_name, exception, duration_ms)
    log_etl_end(
        con, log_id, step_name, start_time, "failed",
        row_count=row_count,
        error_msg=f"{type(exception).__name__}: {exception}\n{tb}",
    )


def check_data_completeness(con) -> dict:
    """Check that ODS tables are all at the same latest trade_date.

    Returns a dict mapping table name -> max trade_date (or None if empty).
    """
    tables = ["ods_daily", "ods_daily_basic", "ods_moneyflow"]
    result = {}
    for t in tables:
        row = con.execute(f"SELECT MAX(trade_date) FROM {t}").fetchone()
        result[t] = row[0] if row and row[0] else None
    return result
