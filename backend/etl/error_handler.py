import logging
import json
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def log_etl(con, step_name: str, status: str, row_count: int = 0,
            error_msg: str = "", data_completeness: Optional[dict] = None):
    """Log an ETL step execution to ods_etl_log.

    Uses UUID for id to avoid race conditions with concurrent ETL processes.
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    comp = json.dumps(data_completeness) if data_completeness else None

    con.execute(
        """INSERT INTO ods_etl_log
           (id, step_name, started_at, finished_at, status, row_count,
            error_msg, data_completeness)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), step_name, now, now, status, row_count,
         error_msg or "", comp or ""),
    )

    if status in ("failed", "degraded"):
        logger.warning(f"ETL {step_name}: {status} — {error_msg}")


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
