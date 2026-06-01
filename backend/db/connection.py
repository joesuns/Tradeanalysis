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
    """Execute a WAL checkpoint."""
    con.execute("CHECKPOINT;")
    logger.info("WAL checkpoint done")
