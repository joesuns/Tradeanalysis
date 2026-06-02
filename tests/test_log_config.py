"""Smoke tests for the logging system — format, file output, rotation."""

import logging
import os
import tempfile
from unittest.mock import patch


def test_log_format_includes_module():
    """Log messages include [module_name] in the output."""
    import io

    # Save and clear existing handlers to avoid interference
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    root.handlers.clear()
    old_level = root.level
    root.setLevel(logging.DEBUG)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)

    try:
        # Import the format string from log_config
        from backend.log_config import _FORMAT
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)

        logger = logging.getLogger("my_custom_module")
        logger.info("test message")

        output = stream.getvalue()
        assert "[my_custom_module]" in output, (
            f"Module name missing from: {output}"
        )
    finally:
        root.handlers.clear()
        for h in old_handlers:
            root.addHandler(h)
        root.setLevel(old_level)


def test_log_file_created():
    """setup_logging creates file handler writing to LOG_FILE."""
    from backend.log_config import setup_logging

    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "test.log")

        # Save root state
        root = logging.getLogger()
        old_handlers = list(root.handlers)
        root.handlers.clear()

        with patch("backend.log_config.LOG_FILE", log_path):
            logger = setup_logging("test_module")
            logger.info("hello world")

            # Force flush
            for h in root.handlers:
                h.flush()
                h.close()
            root.handlers.clear()

            # Restore
            for h in old_handlers:
                root.addHandler(h)

        assert os.path.exists(log_path), f"Log file not created: {log_path}"
        content = open(log_path).read()
        assert "hello world" in content
        assert "[test_module]" in content


def test_etl_log_duration_tracking():
    """log_etl_start/end captures real wall-clock time with different timestamps."""
    from backend.db.connection import get_connection
    from backend.etl.error_handler import log_etl_start, log_etl_end
    import time

    con = get_connection()
    try:
        lid, t0 = log_etl_start(con, "test_step")
        time.sleep(0.1)
        log_etl_end(con, lid, "test_step", t0, "success", row_count=42)

        row = con.execute(
            "SELECT status, row_count, started_at, finished_at "
            "FROM ods_etl_log WHERE id = ?", (lid,)
        ).fetchone()

        assert row is not None, "Log row not found"
        assert row[0] == "success"
        assert row[1] == 42
        assert row[2] != "", "started_at should not be empty"
        assert row[3] != "", "finished_at should not be empty"
        assert row[2] != row[3], (
            f"started_at ({row[2]}) should differ from finished_at ({row[3]})"
        )

        # Clean up test row
        con.execute("DELETE FROM ods_etl_log WHERE id = ?", (lid,))
    finally:
        con.close()
