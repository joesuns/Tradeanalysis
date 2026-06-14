"""Golden regression — skipped until golden files exist."""
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


@pytest.mark.skipif(not _golden_files_ready(), reason="golden not frozen")
def test_b4_golden_matches_db(temp_db):
    from backend.b4_gate.verify import verify_all_dates

    failures = verify_all_dates(temp_db)
    assert failures == []
