"""Tests for WAL checkpoint after multi-connection calc."""
import concurrent.futures
import os
import tempfile

import duckdb

from backend.db.connection import run_checkpoint


def _worker_write(path):
    con = duckdb.connect(path)
    try:
        con.execute("INSERT INTO t VALUES (1)")
    finally:
        con.close()


def test_run_checkpoint_succeeds_with_idle_sibling_connection():
    """Other open RW connection must not break checkpoint (FORCE fallback)."""
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)

    sibling = duckdb.connect(path)
    sibling.execute("CREATE TABLE t (x INTEGER)")

    con = duckdb.connect(path)
    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(_worker_write, [path, path]))
        run_checkpoint(con)
    finally:
        con.close()
        sibling.close()
        os.unlink(path)
        wal = path + ".wal"
        if os.path.exists(wal):
            os.unlink(wal)
