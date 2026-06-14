"""Smoke tests for the logging system — format, file output, rotation."""

import logging
import os
import tempfile
from unittest.mock import patch


def test_log_format_includes_module():
    """Log messages include [module_name] in the output."""
    import io

    # Save and clear existing handlers/filters to avoid interference
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_filters = list(root.filters)
    root.handlers.clear()
    root.filters.clear()
    old_level = root.level
    root.setLevel(logging.DEBUG)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)

    try:
        # Import the format string from log_config (now includes %(run_id)s)
        from backend.log_config import _FORMAT, _RunIdFilter
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(_RunIdFilter())
        root.addHandler(handler)

        logger = logging.getLogger("my_custom_module")
        logger.info("test message")

        output = stream.getvalue()
        assert "[my_custom_module]" in output, (
            f"Module name missing from: {output}"
        )
    finally:
        root.handlers.clear()
        root.filters.clear()
        for h in old_handlers:
            root.addHandler(h)
        for f in old_filters:
            root.addFilter(f)
        root.setLevel(old_level)


def test_log_file_created():
    """setup_logging creates file handler writing to LOG_FILE."""
    from backend.log_config import setup_logging

    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "test.log")

        # Save root state
        root = logging.getLogger()
        old_handlers = list(root.handlers)
        old_filters = list(root.filters)
        root.handlers.clear()
        root.filters.clear()

        with patch("backend.log_config.LOG_FILE", log_path):
            logger = setup_logging("test_module")
            logger.info("hello world")

            # Force flush
            for h in root.handlers:
                h.flush()
                h.close()
            root.handlers.clear()
            root.filters.clear()

            # Restore
            for h in old_handlers:
                root.addHandler(h)
            for f in old_filters:
                root.addFilter(f)

        assert os.path.exists(log_path), f"Log file not created: {log_path}"
        content = open(log_path).read()
        assert "hello world" in content
        assert "[test_module]" in content


def test_etl_log_duration_tracking(db_with_schema):
    """log_etl_start/end captures real wall-clock time with different timestamps."""
    from backend.etl.error_handler import log_etl_start, log_etl_end
    import time

    con = db_with_schema
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

    con.execute("DELETE FROM ods_etl_log WHERE id = ?", (lid,))


def test_etl_log_error_stores_full_traceback(db_with_schema):
    """log_etl_error stores the complete traceback in DB, not truncated.

    Verifies the fix: tb[-500:] → tb (full traceback).
    Uses a deeply nested call chain to generate >1000 chars of traceback.
    """
    from backend.etl.error_handler import log_etl_start, log_etl_error

    def nested_error(depth):
        if depth <= 0:
            raise ValueError("deeply nested test error")
        return nested_error(depth - 1)

    con = db_with_schema
    lid, t0 = log_etl_start(con, "test_error_step")

    try:
        nested_error(15)  # ~60 lines of traceback
    except ValueError as e:
        log_etl_error(con, lid, "test_error_step", t0, 0, e)

    row = con.execute(
        "SELECT status, error_msg FROM ods_etl_log WHERE id = ?", (lid,)
    ).fetchone()

    assert row is not None, "Log row not found"
    assert row[0] == "failed"
    error_msg = row[1]

    assert "ValueError: deeply nested test error" in error_msg
    assert "nested_error" in error_msg, (
        f"Expected traceback in error_msg, got: {error_msg[:200]!r}..."
    )

    frame_count = error_msg.count("nested_error")
    assert frame_count > 5, (
        f"Expected >5 traceback frames, got {frame_count}. "
        f"error_msg may be truncated: {error_msg[:300]!r}..."
    )

    con.execute("DELETE FROM ods_etl_log WHERE id = ?", (lid,))


def test_default_run_id_is_dash():
    """Without set_run_id, log records show '-' as the run_id."""
    import io

    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_filters = list(root.filters)
    old_level = root.level
    root.handlers.clear()
    root.filters.clear()
    root.setLevel(logging.DEBUG)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    # Use the expected new format with [run_id]
    handler.setFormatter(logging.Formatter(
        "%(levelname)s [%(run_id)s][%(name)s] %(message)s"
    ))

    try:
        from backend.log_config import _RunIdFilter
        handler.addFilter(_RunIdFilter())
        root.addHandler(handler)

        logger = logging.getLogger("test.default")
        logger.info("no run id set")

        output = stream.getvalue()
        assert "[-][test.default]" in output, (
            f"Expected default '-' run_id, got: {output!r}"
        )
    finally:
        root.handlers.clear()
        root.filters.clear()
        for h in old_handlers:
            root.addHandler(h)
        for f in old_filters:
            root.addFilter(f)
        root.setLevel(old_level)


def test_set_run_id_injected():
    """When set_run_id is called, log records include the run_id."""
    import io

    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_filters = list(root.filters)
    old_level = root.level
    root.handlers.clear()
    root.filters.clear()
    root.setLevel(logging.DEBUG)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(levelname)s [%(run_id)s][%(name)s] %(message)s"
    ))

    try:
        from backend.log_config import _RunIdFilter, set_run_id
        handler.addFilter(_RunIdFilter())
        root.addHandler(handler)

        set_run_id("test123")
        logger = logging.getLogger("test.module")
        logger.info("hello world")

        output = stream.getvalue()
        assert "[test123][test.module]" in output, (
            f"Expected run_id 'test123' in: {output!r}"
        )
        assert "hello world" in output
    finally:
        root.handlers.clear()
        root.filters.clear()
        for h in old_handlers:
            root.addHandler(h)
        for f in old_filters:
            root.addFilter(f)
        root.setLevel(old_level)
