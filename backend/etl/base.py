import numpy as np
import pandas as pd

from dataclasses import dataclass, field
from enum import Enum


class SkipReason(str, Enum):
    """Root-cause classification for why a stock was skipped during calculation."""
    NO_DWD_DATA = "no_dwd_data"              # DWD has 0 rows for this stock
    INSUFFICIENT_ROWS = "insufficient_rows"   # DWD has rows but < functional minimum
    SOURCE_UNAVAILABLE = "source_unavailable" # tushare doesn't support (e.g. BSE moneyflow)
    FETCH_FAILED = "fetch_failed"            # Auto-fetch exhausted retries
    DELISTED = "delisted"                     # Stock delisted before calc_date
    FINGERPRINT_MATCH = "fingerprint_match"   # DWD input unchanged since last calc


@dataclass
class CalcResult:
    """Return value of Calculator.calculate().

    Usage:
        result = CalcResult()
        result.calculated += 1
        result.add_skip(SkipReason.INSUFFICIENT_ROWS, "688001.SH", "DWD rows=15, min=27")
    """
    calculated: int = 0
    skipped: dict = field(default_factory=dict)  # {SkipReason: [(ts_code, detail), ...]}

    def add_skip(self, reason: SkipReason, ts_code: str, detail: str = ""):
        if reason not in self.skipped:
            self.skipped[reason] = []
        self.skipped[reason].append((ts_code, detail))

    @property
    def total_skipped(self) -> int:
        return sum(len(v) for v in self.skipped.values())

    @property
    def total_input(self) -> int:
        return self.calculated + self.total_skipped


def ema(series: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average. Seed = SMA of first 'period' valid values.
    NaN values are skipped (carry forward) for suspension day handling."""
    result = np.full(len(series), np.nan)
    total_valid = np.sum(~np.isnan(series))
    if total_valid < min(period, 5):
        return result

    alpha = 2.0 / (period + 1)
    valid_sofar = []
    valid_count = 0

    for i in range(len(series)):
        if np.isnan(series[i]):
            # Carry forward previous value for suspension days
            if i > 0 and not np.isnan(result[i - 1]):
                result[i] = result[i - 1]
        else:
            valid_sofar.append(series[i])
            valid_count += 1
            if valid_count < period:
                # Before seed: use SMA of all valid values seen so far
                result[i] = np.mean(valid_sofar)
            elif valid_count == period:
                # Seed: SMA of first 'period' valid values
                result[i] = np.mean(valid_sofar)
            else:
                # Normal EMA formula
                result[i] = alpha * series[i] + (1 - alpha) * result[i - 1]

    return result


def sma(series: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average. Returns NaN where window < period."""
    result = np.full(len(series), np.nan)
    for i in range(period - 1, len(series)):
        window = series[i - period + 1:i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) > 0:
            result[i] = np.mean(valid)
    return result


def linear_regression_slope(y: np.ndarray, use_log: bool = True) -> float:
    """Linear regression slope.
    - use_log=True: ln(y) regression (for MACD bar / volume — large value ranges)
    - use_log=False: raw regression (for DDX2 — small value range, may be negative)
    Returns slope in original (or log) units per bar.
    """
    y = np.array(y, dtype=float)
    if use_log:
        mask = ~np.isnan(y) & (y > 0)
    else:
        mask = ~np.isnan(y)
    if mask.sum() < 2:
        return 0.0
    x = np.arange(len(y), dtype=float)[mask]
    y_vals = np.log(y[mask]) if use_log else y[mask]
    slope = np.polyfit(x, y_vals, 1)[0]
    return float(slope)


def to_float_safe(val):
    """Convert numpy float/NaN to Python float or None for DuckDB compatibility.

    Used by all DWS calculators in their _insert() methods to avoid
    DuckDB CHECK constraint failures on NaN values.
    """
    if val is None:
        return None
    try:
        f = float(val)
        if pd.isna(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


import hashlib


def compute_fingerprint(df: "pd.DataFrame", float_cols: list[str] = None) -> str:
    """Compute content fingerprint (SHA256 truncated) for a DataFrame.

    When float_cols is None, auto-detects all numeric columns (excluding
    ts_code and trade_date). Takes min/max/mean/count as a deterministic
    summary. Returns 16-char hex string.
    """
    import pandas as pd
    if float_cols is None:
        float_cols = [c for c in df.columns
                      if c not in ("ts_code", "trade_date")
                      and pd.api.types.is_numeric_dtype(df[c])]
    parts = []
    for col in sorted(float_cols):
        if col not in df.columns:
            continue
        series = df[col].dropna()
        if len(series) == 0:
            parts.append(f"{col}:empty")
        else:
            parts.append(
                f"{col}:{series.min():.6f}:{series.max():.6f}:"
                f"{series.mean():.6f}:{len(series)}"
            )
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def check_dwd_unchanged(con, dws_table: str, ts_code: str,
                        df: "pd.DataFrame") -> bool:
    """Check if DWD input data is unchanged since last calculation.

    Computes fingerprint of df, queries the DWS table for the most
    recent input_fingerprint for this stock, and compares them.

    Returns True if unchanged (calculation can be skipped).
    """
    new_fp = compute_fingerprint(df)
    row = con.execute(f"""
        SELECT input_fingerprint FROM {dws_table}
        WHERE ts_code = ? AND input_fingerprint IS NOT NULL
        ORDER BY calc_date DESC LIMIT 1
    """, (ts_code,)).fetchone()
    return row is not None and row[0] == new_fp


def insert_dws_batch(con, table: str, df: "pd.DataFrame", ts_code: str,
                     calc_date: str, dws_cols: list[str],
                     float_cols: list[str],
                     spec_version: str = "v1",
                     input_fingerprint: str = None):
    """Shared DWS INSERT -- replaces individual Calculator _insert methods.

    Handles: calc_date, spec_version, input_fingerprint, to_float_safe.
    When input_fingerprint is provided, uses it directly instead of computing.
    """
    import pandas as pd

    data_cols = [c for c in dws_cols if c != "ts_code"]
    for c in data_cols:
        if c not in df.columns:
            df[c] = None

    batch = df[data_cols].copy()
    batch["ts_code"] = ts_code

    for c in float_cols:
        if c in batch.columns:
            batch[c] = batch[c].apply(to_float_safe)

    batch["calc_date"] = calc_date
    batch["spec_version"] = spec_version
    batch["input_fingerprint"] = input_fingerprint or compute_fingerprint(df, float_cols)

    con.register("_batch", batch)
    cols_sql = ", ".join(dws_cols)
    con.execute(
        f"INSERT OR REPLACE INTO {table} ({cols_sql}) "
        f"SELECT {cols_sql} FROM _batch"
    )
    con.unregister("_batch")
