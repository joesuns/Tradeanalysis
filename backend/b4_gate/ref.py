"""Resolve 123 reference SQLite path for B4 gate diff."""
import os
from typing import Optional

from backend.config import REF_123_SQLITE_PATH


def resolve_ref_123_sqlite_path(path: Optional[str] = None) -> str:
    """Return existing 123 SQLite file path.

    Priority: explicit path → REF_123_SQLITE_PATH env → legacy REF_123_DUCKDB_PATH
    (treated as sqlite file path during transition).
    """
    candidates = [
        path,
        os.environ.get("REF_123_SQLITE_PATH"),
        REF_123_SQLITE_PATH,
        os.environ.get("REF_123_DUCKDB_PATH"),
    ]
    for c in candidates:
        if not c:
            continue
        resolved = os.path.normpath(os.path.expanduser(c))
        if os.path.isfile(resolved):
            return resolved
    tried = [c for c in candidates if c]
    raise FileNotFoundError(
        "123 reference SQLite not found. Set REF_123_SQLITE_PATH or pass --ref-db. "
        f"Tried: {tried}"
    )
