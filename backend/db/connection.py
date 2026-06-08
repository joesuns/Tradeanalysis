import duckdb
import os
import shutil
import logging
from backend.config import DUCKDB_PATH

logger = logging.getLogger(__name__)


def get_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Get a DuckDB connection, creating the data directory if needed."""
    data_dir = os.path.dirname(DUCKDB_PATH) or "."
    os.makedirs(data_dir, exist_ok=True)
    con = duckdb.connect(DUCKDB_PATH, read_only=read_only)
    if not read_only:
        con.execute("PRAGMA enable_checkpoint_on_shutdown;")
    return con


def check_connectivity() -> dict:
    """Check DuckDB connectivity, disk space, and database size."""
    result = {"duckdb": "ok", "disk_free_mb": 0, "db_size_mb": 0, "version": ""}
    try:
        con = get_connection()
        result["version"] = con.execute("SELECT version()").fetchone()[0]
        con.execute("SELECT 1")
        con.close()
    except Exception as e:
        result["duckdb"] = f"error: {e}"
        return result
    data_dir = os.path.dirname(DUCKDB_PATH) or "."
    stat = shutil.disk_usage(data_dir)
    result["disk_free_mb"] = stat.free // (1024 * 1024)
    if os.path.exists(DUCKDB_PATH):
        result["db_size_mb"] = os.path.getsize(DUCKDB_PATH) // (1024 * 1024)
    if result["disk_free_mb"] < 100:
        result["duckdb"] = "fatal: low disk space"
    return result


def run_checkpoint(con: duckdb.DuckDBPyConnection):
    """Execute a WAL checkpoint.

    Uses CHECKPOINT first; falls back to FORCE CHECKPOINT when other
    connections still hold transactions (e.g. cmd_run briefly overlapping
    with cmd_calc). FORCE waits until all writers finish — safe after
    multi-threaded calc when worker connections are already closed.
    """
    try:
        con.execute("CHECKPOINT;")
    except duckdb.TransactionException:
        con.execute("FORCE CHECKPOINT;")
    logger.info("WAL checkpoint done")


DWS_TABLES = [
    f"dws_{indicator}_{freq}"
    for indicator in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]
    for freq in ["daily", "weekly"]
]


def prune_dws_snapshots(con: duckdb.DuckDBPyConnection, keep_runs: int = 5) -> dict:
    """Remove superseded DWS snapshot rows while preserving latest-per-key.

    DWS tables are INSERT-only: each calc run appends a new ``calc_date``
    snapshot, so storage grows unbounded. Consumers only read the
    ``v_*_latest`` views, which select MAX(calc_date) per (ts_code, trade_date).

    This deletes rows older than the ``keep_runs``-th most recent distinct
    ``calc_date`` *unless* the row is the MAX(calc_date) for its
    (ts_code, trade_date) pair. That invariant guarantees the latest views
    stay byte-for-byte identical, even when fingerprint-skip leaves a key's
    newest value on an older calc_date.

    Args:
        keep_runs: number of most recent run dates to fully retain.
            1 collapses to pure latest; larger keeps a rolling audit window.

    Returns:
        dict mapping table name -> number of rows deleted.
    """
    if keep_runs < 1:
        raise ValueError("keep_runs must be >= 1")

    result = {}
    for table in DWS_TABLES:
        cutoff_row = con.execute(
            f"SELECT MIN(calc_date) FROM ("
            f"  SELECT DISTINCT calc_date FROM {table} "
            f"  ORDER BY calc_date DESC LIMIT ?"
            f")",
            (keep_runs,),
        ).fetchone()
        cutoff = cutoff_row[0] if cutoff_row else None
        if cutoff is None:
            result[table] = 0
            continue

        before = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        con.execute(
            f"""
            DELETE FROM {table}
            WHERE calc_date < ?
              AND (ts_code, trade_date, calc_date) NOT IN (
                  SELECT ts_code, trade_date, MAX(calc_date)
                  FROM {table}
                  GROUP BY ts_code, trade_date
              )
            """,
            (cutoff,),
        )
        after = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        result[table] = before - after
    return result
