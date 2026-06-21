import atexit
import duckdb
import glob
import logging
import os
import shutil
import signal
import sys
import threading
from backend.config import (
    DUCKDB_PATH,
    DUCKDB_TEMP_DIRECTORY,
    DUCKDB_MAX_MEMORY_MB,
    DWS_PRUNE_KEEP_RUNS,
    MIN_DISK_FREE_MB,
    PRUNE_KEEP_BACKUPS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Temp-directory lifecycle
# ---------------------------------------------------------------------------

_temp_dirs_registered: set = set()
_cleanup_lock = threading.Lock()
_orphan_cleanup_done = False
_exit_signaled: int = 0  # 0=no signal, >0=received signal number


def _cleanup_temp_dirs() -> None:
    """atexit handler: remove registered DuckDB temp directories.

    When a signal was received (SIGTERM/SIGINT), restores the correct
    exit code before exiting.
    """
    with _cleanup_lock:
        dirs = list(_temp_dirs_registered)
        _temp_dirs_registered.clear()
    for d in dirs:
        if os.path.isdir(d):
            try:
                shutil.rmtree(d)
                logger.info("Cleaned up temp directory: %s", d)
            except Exception:
                logger.debug("Failed to clean temp directory %s — may be in use", d, exc_info=True)
    if _exit_signaled:
        sys.exit(128 + _exit_signaled)


def _signal_handler(signum, frame):
    """Minimal signal handler: record the signal — no heavy work in signal context.

    Cleanup and exit are delegated to ``_cleanup_temp_dirs`` (atexit), which
    is safe to run outside signal context and restores the correct exit code.
    """
    global _exit_signaled
    _exit_signaled = signum


def register_temp_cleanup(temp_dir: str) -> None:
    """Register *temp_dir* for automatic cleanup on process exit or signal.

    Idempotent — calling with the same directory multiple times is safe.
    Only the first call installs signal handlers (main thread only).
    """
    abs_dir = os.path.abspath(temp_dir)
    with _cleanup_lock:
        if abs_dir in _temp_dirs_registered:
            return
        _temp_dirs_registered.add(abs_dir)
        if len(_temp_dirs_registered) == 1:
            atexit.register(_cleanup_temp_dirs)
            # Signal handlers can only be installed in the main thread.
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGTERM, signal.SIGINT):
                    try:
                        signal.signal(sig, _signal_handler)
                    except ValueError:
                        pass  # not in main thread (defensive)


def _validate_temp_dir_path(temp_dir: str) -> None:
    """Reject paths containing characters unsafe for a non-parameterised SQL SET.

    DuckDB does not support parameterised ``SET temp_directory``, so we build
    the statement via string formatting.  Limiting allowed characters is a
    belt-and-braces defence in addition to the single-quote escaping in the
    caller.
    """
    import re
    if not re.match(r'^[a-zA-Z0-9_\-./:\\ ]+$', temp_dir):
        raise ValueError(
            f"DUCKDB_TEMP_DIRECTORY contains unsafe characters: {temp_dir!r}"
        )


def _resolve_temp_dir(data_dir: str) -> str:
    """Resolve DUCKDB_TEMP_DIRECTORY to an absolute path.

    Relative paths are resolved against *data_dir* (not CWD), so spill files
    stay on the same volume regardless of invocation directory.
    """
    tp = DUCKDB_TEMP_DIRECTORY
    if not os.path.isabs(tp):
        tp = os.path.join(data_dir, tp)
    return os.path.abspath(tp)


def cleanup_orphan_temp_dirs(data_dir: str, db_path: str = None) -> int:
    """Remove DuckDB ``.tmp`` directories that no longer have an active process.

    DuckDB creates ``<db_file>.tmp`` in the database directory by default (or
    ``.tmp`` when the path contains no extension). This function cleans orphaned
    directories left behind by crashes.

    Returns the number of directories removed.
    """
    if db_path is None:
        db_path = DUCKDB_PATH
    db_abs = os.path.abspath(db_path)
    data_dir_abs = os.path.abspath(data_dir) if data_dir else os.path.dirname(db_abs)

    # Never touch directories that belong to this process.
    with _cleanup_lock:
        active = set(_temp_dirs_registered)

    removed = 0
    # Pattern 1: <db_name>.duckdb.tmp (DuckDB 1.x default next to database)
    default_tmp = os.path.join(data_dir_abs, os.path.basename(db_abs) + ".tmp")
    if os.path.isdir(default_tmp) and default_tmp not in active:
        try:
            shutil.rmtree(default_tmp)
            logger.info("Cleaned orphan temp dir: %s", default_tmp)
            removed += 1
        except Exception:
            logger.debug("Failed to clean %s", default_tmp, exc_info=True)

    # Pattern 2: our configured temp directory (e.g. ./data/tmp/)
    if DUCKDB_TEMP_DIRECTORY:
        cfg_tmp = _resolve_temp_dir(data_dir_abs)
        # Only clean if it's under data_dir (defence: don't rm random paths)
        if (cfg_tmp.startswith(data_dir_abs) and cfg_tmp != data_dir_abs
                and cfg_tmp not in active):
            if os.path.isdir(cfg_tmp):
                try:
                    shutil.rmtree(cfg_tmp)
                    logger.info("Cleaned configured temp dir: %s", cfg_tmp)
                    removed += 1
                except Exception:
                    logger.debug("Failed to clean %s", cfg_tmp, exc_info=True)

    return removed


def _safe_getsize(path: str) -> int:
    """Return file size in bytes, or 0 if the file is inaccessible (TOCTOU-safe)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def cleanup_backup_files(data_dir: str, keep: int = None, dry_run: bool = False) -> dict:
    """Remove old ``pre-*`` DuckDB backup files, retaining the *keep* most recent.

    Args:
        data_dir: path to the data directory.
        keep: number of recent backups to retain (default ``PRUNE_KEEP_BACKUPS``).
        dry_run: if True, only report what would be deleted.

    Returns:
        dict with ``deleted`` (list of paths) and ``freed_mb`` (int).
    """
    if keep is None:
        keep = PRUNE_KEEP_BACKUPS
    if keep < 0:
        keep = 0

    pattern = os.path.join(data_dir, "tradeanalysis.pre-*.duckdb")
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    to_delete = candidates[keep:] if len(candidates) > keep else []
    freed_bytes = sum(_safe_getsize(p) for p in to_delete)

    if not dry_run:
        for p in to_delete:
            try:
                os.unlink(p)
                logger.info("Removed old backup: %s", os.path.basename(p))
            except OSError:
                logger.warning("Failed to remove backup: %s", p, exc_info=True)

    return {
        "deleted": [os.path.basename(p) for p in to_delete],
        "freed_mb": freed_bytes // (1024 * 1024),
        "retained": [os.path.basename(p) for p in candidates[:keep]],
    }


def get_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Get a DuckDB connection, creating the data directory if needed.

    On read-write connections, configures a dedicated temp directory (so
    out-of-core spill files never compete with system /tmp) and a memory
    limit that triggers earlier, smaller spills.

    The **first** read-write connection in a process also cleans orphan temp
    directories left by previous crashed runs.
    """
    global _orphan_cleanup_done

    data_dir = os.path.dirname(os.path.abspath(DUCKDB_PATH)) or "."
    os.makedirs(data_dir, exist_ok=True)
    con = duckdb.connect(DUCKDB_PATH, read_only=read_only)
    if not read_only:
        con.execute("PRAGMA enable_checkpoint_on_shutdown;")

        # --- orphan cleanup (once per process) --------------------------------
        with _cleanup_lock:
            if not _orphan_cleanup_done:
                _orphan_cleanup_done = True
                should_clean = True
            else:
                should_clean = False
        if should_clean:
            cleanup_orphan_temp_dirs(data_dir)

        # --- temp_directory -------------------------------------------------
        # Resolve relative to data_dir (not CWD) so spill files stay on the
        # same volume as the database regardless of invocation directory.
        if DUCKDB_TEMP_DIRECTORY:
            temp_dir = _resolve_temp_dir(data_dir)
        else:
            temp_dir = os.path.join(data_dir, os.path.basename(DUCKDB_PATH) + ".tmp")

        os.makedirs(temp_dir, exist_ok=True)
        _validate_temp_dir_path(temp_dir)
        # Escape single quotes — DuckDB SET does not support parameterised values.
        con.execute("SET temp_directory = '{}';".format(temp_dir.replace("'", "''")))
        register_temp_cleanup(temp_dir)

        # --- memory_limit ---------------------------------------------------
        if DUCKDB_MAX_MEMORY_MB > 0:
            con.execute("SET memory_limit = '{}MB';".format(DUCKDB_MAX_MEMORY_MB))

    return con


def check_connectivity() -> dict:
    """Check DuckDB connectivity, disk space (data + temp), and database size.

    Returns a dict suitable for health-check logging.  The ``duckdb`` key is
    ``"ok"`` when everything passes; ``"fatal: ..."`` when the pipeline should
    refuse to start; ``"warn: ..."`` when the pipeline may proceed at risk.
    """
    result = {
        "duckdb": "ok",
        "disk_free_mb": 0,
        "temp_disk_free_mb": 0,
        "db_size_mb": 0,
        "version": "",
        "backup_count": 0,
        "backup_size_mb": 0,
    }
    try:
        con = get_connection(read_only=True)
        result["version"] = con.execute("SELECT version()").fetchone()[0]
        con.execute("SELECT 1")
        con.close()
    except Exception as e:
        result["duckdb"] = f"error: {e}"
        return result

    data_dir = os.path.dirname(os.path.abspath(DUCKDB_PATH)) or "."

    # ---- data volume free space ----
    stat = shutil.disk_usage(data_dir)
    result["disk_free_mb"] = stat.free // (1024 * 1024)
    if os.path.exists(DUCKDB_PATH):
        result["db_size_mb"] = _safe_getsize(DUCKDB_PATH) // (1024 * 1024)

    # ---- temp directory free space (same volume if under data/) ----
    temp_dir = _resolve_temp_dir(data_dir) if DUCKDB_TEMP_DIRECTORY else ""
    if temp_dir and os.path.isdir(temp_dir):
        try:
            tstat = shutil.disk_usage(temp_dir)
            result["temp_disk_free_mb"] = tstat.free // (1024 * 1024)
        except OSError:
            result["temp_disk_free_mb"] = result["disk_free_mb"]
    else:
        result["temp_disk_free_mb"] = result["disk_free_mb"]

    # ---- backup files ----
    pattern = os.path.join(data_dir, "tradeanalysis.pre-*.duckdb")
    backups = sorted(glob.glob(pattern))
    result["backup_count"] = len(backups)
    if backups:
        result["backup_size_mb"] = sum(_safe_getsize(p) for p in backups) // (1024 * 1024)

    # ---- gate decisions ----
    threshold = max(MIN_DISK_FREE_MB, result["db_size_mb"] // 3)
    temp_threshold = max(threshold // 2, 1024)

    if result["disk_free_mb"] < threshold:
        result["duckdb"] = (
            f"fatal: low disk space ({result['disk_free_mb']} MB free < "
            f"{threshold} MB min)"
        )
    elif result["temp_disk_free_mb"] < temp_threshold:
        result["duckdb"] = (
            f"fatal: low temp disk space ({result['temp_disk_free_mb']} MB free < "
            f"{temp_threshold} MB min)"
        )
    elif result["disk_free_mb"] < threshold * 2:
        result["duckdb"] = (
            f"warn: disk space low ({result['disk_free_mb']} MB free)"
        )
    elif result["backup_count"] > max(PRUNE_KEEP_BACKUPS, 3):
        result["duckdb"] = (
            f"warn: {result['backup_count']} old backup files ({result['backup_size_mb']} MB) — "
            f"consider 'prune --cleanup-backups'"
        )

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


def prune_dws_snapshots(con: duckdb.DuckDBPyConnection, keep_runs: int = None) -> dict:
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
            Defaults to ``DWS_PRUNE_KEEP_RUNS`` (env-var override-able, 2).
            1 collapses to pure latest; larger keeps a rolling audit window.

    Returns:
        dict mapping table name -> number of rows deleted.
    """
    if keep_runs is None:
        keep_runs = DWS_PRUNE_KEEP_RUNS
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
