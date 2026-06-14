"""Verify Tradeanalysis DB against frozen B4 golden CSVs."""
from pathlib import Path
from typing import List, Optional

import pandas as pd

from backend.b4_gate.columns import B4_ALL_FIELDS, B4_HARD_ALL_FIELDS
from backend.b4_gate.extract import extract_ta_b4
from backend.b4_gate.sample import load_sample

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "b4_gate"
DATES_FILE = FIXTURE_DIR / "dates.txt"


def load_dates(path: Path = None) -> List[str]:
    p = path or DATES_FILE
    if not p.exists():
        return []
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def golden_path(date: str) -> Path:
    return FIXTURE_DIR / f"golden_{date}.csv"


def verify_date(con, date: str, sample_path: Path = None) -> List[dict]:
    golden = golden_path(date)
    if not golden.exists():
        return [{"date": date, "error": f"missing golden {golden}"}]
    gdf = pd.read_csv(golden, dtype=str)
    codes = gdf["ts_code"].tolist()
    ta = extract_ta_b4(con, date, codes)
    failures: List[dict] = []
    for _, grow in gdf.iterrows():
        ts = grow["ts_code"]
        trow = ta[ta["ts_code"] == ts]
        if trow.empty:
            failures.append({"date": date, "ts_code": ts, "error": "missing in TA"})
            continue
        trow = trow.iloc[0]
        for col in B4_HARD_ALL_FIELDS + ["trade_date", "week_end"]:
            if col not in grow.index:
                continue
            exp = grow[col]
            got = trow.get(col)
            exp_s = None if pd.isna(exp) or str(exp).strip() == "" else str(exp).strip()
            got_s = None if pd.isna(got) or str(got).strip() == "" else str(got).strip()
            if exp_s != got_s:
                failures.append({
                    "date": date,
                    "ts_code": ts,
                    "field": col,
                    "expected": exp_s,
                    "got": got_s,
                })
    return failures


def verify_all_dates(con, dates: Optional[List[str]] = None) -> List[dict]:
    dates = dates or load_dates()
    all_fail: List[dict] = []
    for d in dates:
        all_fail.extend(verify_date(con, d))
    return all_fail


def export_golden(con, date: str, out_path: Path, sample_path: Path = None) -> int:
    rows = load_sample(sample_path)
    codes = [r.ts_code for r in rows]
    df = extract_ta_b4(con, date, codes)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["ts_code", "trade_date", "week_end"] + [
        c for c in B4_HARD_ALL_FIELDS if c in df.columns
    ]
    df[cols].to_csv(out_path, index=False)
    return len(df)
