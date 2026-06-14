"""Helpers for MACD/DDE structure divergence golden collection (TDX alignment)."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from backend.etl.calc_dde import DDECalculator
from backend.etl.calc_macd import MACDCalculator

GOLDEN_COLUMNS = ["ts_code", "trade_date", "freq", "divergence", "note"]
WORKSHEET_COLUMNS = [
    "ts_code",
    "freq",
    "impl_trade_date",
    "impl_divergence",
    "tdx_trade_date",
    "tdx_divergence",
    "note",
]


def load_macd_daily_frame(con, ts_code: str, freq: str = "daily") -> pd.DataFrame:
    """Load quote history and compute MACD indicators (same as calculator pipeline)."""
    calc = MACDCalculator(con, freq)
    if freq == "daily":
        df = con.execute(
            """
            SELECT trade_date, close_qfq
            FROM dwd_daily_quote
            WHERE ts_code = ? AND is_suspended = 0
            ORDER BY trade_date
            """,
            [ts_code],
        ).df()
    else:
        df = con.execute(
            """
            SELECT wq.trade_date, wq.close_qfq
            FROM dwd_weekly_quote wq
            JOIN dim_date dd ON wq.trade_date = dd.trade_date AND dd.is_week_end = 1
            WHERE wq.ts_code = ?
            ORDER BY wq.trade_date
            """,
            [ts_code],
        ).df()
    if df is None or df.empty:
        return pd.DataFrame()
    return calc._compute_indicators(df.reset_index(drop=True))


def load_dde_daily_frame(con, ts_code: str, freq: str = "daily") -> pd.DataFrame:
    """Load moneyflow + quote and compute DDE indicators."""
    calc = DDECalculator(con, freq)
    df = calc._load_daily(ts_code) if freq == "daily" else calc._load_weekly(ts_code)
    if df is None or df.empty:
        return pd.DataFrame()
    return calc._compute_indicators(df.reset_index(drop=True))


def extract_divergence_events(
    df: pd.DataFrame,
    ts_code: str,
    freq: str = "daily",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[dict]:
    """Return rows where divergence is top/bottom (structure TG days)."""
    if df is None or df.empty or "divergence" not in df.columns:
        return []
    out = df.loc[df["divergence"].notna(), ["trade_date", "divergence"]].copy()
    if start_date:
        out = out[out["trade_date"] >= start_date]
    if end_date:
        out = out[out["trade_date"] <= end_date]
    rows = []
    for _, r in out.iterrows():
        rows.append({
            "ts_code": ts_code,
            "freq": freq,
            "impl_trade_date": str(r["trade_date"]),
            "impl_divergence": str(r["divergence"]),
            "tdx_trade_date": "",
            "tdx_divergence": "",
            "note": "impl_export",
        })
    return rows


def date_within_tolerance(hits: List[str], expect_date: str, tol: int = 1) -> bool:
    if not hits:
        return False
    expect_dt = datetime.strptime(str(expect_date).replace("-", "")[:8], "%Y%m%d")
    for h in hits:
        hit_str = str(h).replace("-", "")[:8]
        hit_dt = datetime.strptime(hit_str, "%Y%m%d")
        if abs((hit_dt - expect_dt).days) <= tol:
            return True
    return False


def read_ts_codes_file(path: Path) -> List[str]:
    codes = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            codes.append(line.split()[0])
    return codes


def write_csv(path: Path, rows: List[dict], columns: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def read_worksheet(path: Path) -> List[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def merge_worksheet_into_golden(worksheet_rows: List[dict], golden_path: Path) -> Tuple[int, int]:
    """Import rows with tdx_trade_date filled. Returns (added, skipped_dup)."""
    existing = []
    if golden_path.exists():
        with open(golden_path, newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))

    keys = {(r["ts_code"], r["trade_date"], r.get("freq", "daily"), r["divergence"])
            for r in existing}

    added = 0
    skipped = 0
    for row in worksheet_rows:
        tdx_date = (row.get("tdx_trade_date") or "").strip()
        tdx_div = (row.get("tdx_divergence") or "").strip()
        if not tdx_date or not tdx_div:
            continue
        tdx_date = tdx_date.replace("-", "")[:8]
        entry = {
            "ts_code": row["ts_code"].strip(),
            "trade_date": tdx_date,
            "freq": (row.get("freq") or "daily").strip(),
            "divergence": tdx_div,
            "note": (row.get("note") or "tdx_structure_TG").strip() or "tdx_structure_TG",
        }
        key = (entry["ts_code"], entry["trade_date"], entry["freq"], entry["divergence"])
        if key in keys:
            skipped += 1
            continue
        existing.append(entry)
        keys.add(key)
        added += 1

    write_csv(golden_path, existing, GOLDEN_COLUMNS)
    return added, skipped


def diff_golden_vs_impl(
    con,
    golden_path: Path,
    indicator: str,
    tolerance: int = 1,
    freq: str = "daily",
) -> Dict[str, object]:
    """Compare golden (TDX labels) against current implementation."""
    if not golden_path.exists():
        return {"total": 0, "matched": 0, "rate": 0.0, "details": []}

    with open(golden_path, newline="", encoding="utf-8") as f:
        golden_rows = list(csv.DictReader(f))

    details = []
    matched = 0
    for row in golden_rows:
        ts_code = row["ts_code"]
        expect_date = row["trade_date"].replace("-", "")[:8]
        expect_div = row["divergence"]
        row_freq = row.get("freq", freq) or freq

        if indicator == "macd":
            df = load_macd_daily_frame(con, ts_code, row_freq)
        else:
            df = load_dde_daily_frame(con, ts_code, row_freq)

        if df.empty:
            details.append({
                "ts_code": ts_code,
                "expect_date": expect_date,
                "expect": expect_div,
                "ok": False,
                "impl_hits": [],
                "reason": "no_dwd_data",
            })
            continue

        hits = df.loc[df["divergence"] == expect_div, "trade_date"].astype(str).tolist()
        ok = date_within_tolerance(hits, expect_date, tol=tolerance)
        if ok:
            matched += 1
        details.append({
            "ts_code": ts_code,
            "expect_date": expect_date,
            "expect": expect_div,
            "ok": ok,
            "impl_hits": hits,
            "reason": "" if ok else "no_match",
        })

    total = len(golden_rows)
    rate = (matched / total) if total else 0.0
    return {"total": total, "matched": matched, "rate": rate, "details": details}


def smoke_impl_on_codes(
    con,
    indicator: str,
    ts_codes: List[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    freq: str = "daily",
) -> List[dict]:
    """Run structure divergence on each code; return per-stock summary rows."""
    rows = []
    for ts_code in ts_codes:
        if indicator == "macd":
            df = load_macd_daily_frame(con, ts_code, freq)
        else:
            df = load_dde_daily_frame(con, ts_code, freq)
        if df.empty:
            rows.append({
                "ts_code": ts_code,
                "bars": 0,
                "top": 0,
                "bottom": 0,
                "status": "no_data",
            })
            continue
        events = extract_divergence_events(
            df, ts_code, freq=freq, start_date=start_date, end_date=end_date,
        )
        top = sum(1 for e in events if e["impl_divergence"] == "top_divergence")
        bottom = sum(1 for e in events if e["impl_divergence"] == "bottom_divergence")
        rows.append({
            "ts_code": ts_code,
            "bars": len(df),
            "top": top,
            "bottom": bottom,
            "status": "ok",
        })
    return rows


def sample_ts_codes(con, count: int = 25) -> List[str]:
    """Pick diverse active stocks for golden labeling."""
    rows = con.execute(
        """
        SELECT ts_code, sector, name
        FROM dim_stock
        WHERE is_active = 1
          AND ts_code NOT LIKE '%.BJ'
        ORDER BY sector, ts_code
        """
    ).fetchall()
    if not rows:
        return []

    by_sector: Dict[str, List[str]] = {}
    for ts_code, sector, _name in rows:
        sec = sector or "unknown"
        by_sector.setdefault(sec, []).append(ts_code)

    picked: List[str] = []
    sectors = sorted(by_sector.keys())
    idx = 0
    while len(picked) < count and sectors:
        sec = sectors[idx % len(sectors)]
        pool = by_sector[sec]
        if pool:
            picked.append(pool.pop(0))
            if not pool:
                sectors.remove(sec)
                if not sectors:
                    break
                idx = 0
                continue
        idx += 1

    return picked[:count]
