"""Shared logging configuration — file rotation + stderr, structured format.

Usage:
    from backend.log_config import setup_logging
    logger = setup_logging(__name__)

Features:
    - RotatingFileHandler: writes to LOG_FILE, rotates at LOG_MAX_BYTES, keeps
      LOG_BACKUP_COUNT backups.
    - StreamHandler: stderr at LOG_LEVEL for dev visibility.
    - Format includes ISO8601 timestamp, level, [run_id][module_name], and message.
    - Trace ID: set_run_id() injects a request-level identifier via contextvars.
    - Idempotent — safe to call multiple times (handlers added once).
"""

import contextvars
import logging
import os
from logging.handlers import RotatingFileHandler

from backend.config import LOG_BACKUP_COUNT, LOG_FILE, LOG_LEVEL, LOG_MAX_BYTES

_FORMAT = "%(asctime)s %(levelname)-8s [%(run_id)s][%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar('run_id', default='-')


class _RunIdFilter(logging.Filter):
    """Inject run_id from context variable into every log record."""

    def filter(self, record):
        record.run_id = _run_id.get()
        return True


def set_run_id(run_id: str):
    """Set the run ID for the current execution context.

    Call at CLI entry point before any work begins.  Thread-safe: each
    thread inherits the parent's run_id at creation time, but modifications
    in child threads do not propagate back.
    """
    _run_id.set(run_id)


def setup_logging(name: str = None) -> logging.Logger:
    """Configure root logger with file rotation + stderr.

    Idempotent — only adds handlers on first call. Subsequent calls
    are no-ops. Returns a logger for the given name (or root if None).

    Parameters
    ----------
    name : str, optional
        Logger name. Pass __name__ to get a module-scoped logger.

    Returns
    -------
    logging.Logger
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Prevent duplicate handlers on repeated calls
    if root.handlers:
        return logging.getLogger(name) if name else root

    fmt = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    # File handler with rotation
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)  # File always gets DEBUG for forensic analysis
    fh.setFormatter(fmt)
    fh.addFilter(_RunIdFilter())
    root.addHandler(fh)

    # Stderr handler (for dev visibility)
    sh = logging.StreamHandler()
    sh.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    sh.setFormatter(fmt)
    sh.addFilter(_RunIdFilter())
    root.addHandler(sh)

    return logging.getLogger(name) if name else root
