"""G2 stratified sample loader for B4 golden / diff."""
from dataclasses import dataclass
from pathlib import Path
from typing import List

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "b4_gate"
SAMPLE_CSV = FIXTURE_DIR / "sample_500.csv"


@dataclass
class SampleRow:
    ts_code: str
    bucket: str
    note: str = ""


def load_sample(path: Path = None) -> List[SampleRow]:
    import csv

    csv_path = path or SAMPLE_CSV
    rows: List[SampleRow] = []
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(
                SampleRow(
                    ts_code=row["ts_code"].strip(),
                    bucket=row["bucket"].strip(),
                    note=row.get("note", "").strip(),
                )
            )
    return rows


def is_bse(ts_code: str) -> bool:
    return ts_code.endswith(".BJ")


def skip_dde_compare(ts_code: str, bucket: str) -> bool:
    """BSE stocks: DDE columns are not compared against 123."""
    return is_bse(ts_code) or bucket == "bse"
