"""Golden regression — skipped until golden files exist."""
import os

import duckdb
import pytest
from pathlib import Path

GOLDEN_DIR = Path("tests/fixtures/b4_gate")


def _golden_dates():
    from backend.b4_gate.verify import load_dates
    return load_dates(GOLDEN_DIR / "dates.txt")


def _golden_files_ready():
    from backend.b4_gate.verify import golden_path
    dates = _golden_dates()
    return dates and all(golden_path(d).exists() for d in dates)


def _local_duckdb_ready() -> bool:
    """Golden diff reads v_*_latest + DWD; requires persisted project DB."""
    from backend.config import DUCKDB_PATH

    if not os.path.isfile(DUCKDB_PATH):
        return False
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            con.execute("SELECT 1 FROM dwd_daily_quote LIMIT 1")
            return True
        finally:
            con.close()
    except duckdb.Error:
        return False


@pytest.mark.skipif(not _golden_files_ready(), reason="golden not frozen")
@pytest.mark.skipif(not _local_duckdb_ready(), reason="requires local tradeanalysis.duckdb")
def test_b4_golden_matches_db():
    from backend.b4_gate.verify import verify_all_dates
    from backend.config import DUCKDB_PATH

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        failures = verify_all_dates(con)
    finally:
        con.close()
    assert failures == []
