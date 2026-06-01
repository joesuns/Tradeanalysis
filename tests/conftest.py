import pytest
import duckdb
import os
import tempfile


@pytest.fixture
def temp_db():
    """Create a temporary DuckDB database, auto-cleaned after test."""
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)  # Remove the empty file so DuckDB can create it fresh
    con = duckdb.connect(path)
    yield con
    con.close()
    os.unlink(path)
    # Clean up WAL file if it exists
    wal_path = path + ".wal"
    if os.path.exists(wal_path):
        os.unlink(wal_path)


@pytest.fixture
def db_with_schema(temp_db):
    """Temporary DuckDB database with full DDL schema applied."""
    from backend.db.schema import create_all_tables
    create_all_tables(temp_db)
    return temp_db
