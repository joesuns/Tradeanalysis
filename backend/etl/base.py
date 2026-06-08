import duckdb
import numpy as np
import pandas as pd

from collections import deque
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


def ema(series: np.ndarray, period: int, seed: float = None) -> np.ndarray:
    """Exponential Moving Average. Seed = SMA of first 'period' valid values.

    When ``seed`` is provided, the series is treated as continuing from a prior
    bar whose EMA equals ``seed`` (post-warmup递推). NaN values carry forward.
    """
    result = np.full(len(series), np.nan)
    total_valid = np.sum(~np.isnan(series))
    if total_valid < min(period, 5) and seed is None:
        return result

    alpha = 2.0 / (period + 1)

    if seed is not None:
        prev = seed
        for i in range(len(series)):
            if np.isnan(series[i]):
                result[i] = prev
            else:
                result[i] = alpha * series[i] + (1 - alpha) * prev
                prev = result[i]
        return result

    valid_sofar = []
    valid_count = 0
    for i in range(len(series)):
        if np.isnan(series[i]):
            if i > 0 and not np.isnan(result[i - 1]):
                result[i] = result[i - 1]
        else:
            valid_sofar.append(series[i])
            valid_count += 1
            if valid_count < period:
                result[i] = np.mean(valid_sofar)
            elif valid_count == period:
                result[i] = np.mean(valid_sofar)
            else:
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


def weighted_window_slopes(y: np.ndarray, window: int,
                           decay: float) -> np.ndarray:
    """Vectorized fixed-window exponentially-weighted LS regression slope.

    For each index ``i >= window-1`` returns the order-1 weighted least-squares
    slope over ``y[i-window+1 : i+1]`` with ``x = 0..window-1`` and weights
    ``exp(x*decay)``. This is numerically equivalent to the legacy per-bar
    ``np.polyfit(x, segment, 1, w=exp(x*decay))[0]``.

    IMPORTANT: ``np.polyfit(..., w=w)`` minimizes ``Σ (w·(y-ŷ))²``, so the
    effective WLS weight is ``w²`` — applied below as ``W = w*w``. Using ``w``
    directly would NOT match polyfit.

    Windows containing any NaN produce NaN (matching the legacy
    ``len(valid) < window -> skip`` guard, since segments are exactly ``window``
    long). Output length equals ``len(y)``; indices ``< window-1`` are NaN.
    Pass ``decay=0`` for an unweighted (OLS) fit.
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    out = np.full(n, np.nan)
    if window < 2 or n < window:
        return out
    x = np.arange(window, dtype=float)
    w = np.exp(x * decay)
    W = w * w
    SW = W.sum()
    xbar = (W * x).sum() / SW
    xc = x - xbar
    denom = (W * xc * xc).sum()
    c = W * xc  # slope = dot(c, window) / denom
    # np.convolve(y, c[::-1], 'valid')[k] == Σ_j c[j]·y[k+j] for window k..k+window-1
    conv = np.convolve(y, c[::-1], mode="valid")
    out[window - 1:] = conv / denom
    return out


def rolling_window_minmax_deque(values, window: int,
                              min_periods: int = 2):
    """O(n) rolling min/max via monotonic deques.

    Matches ``pd.Series(values).rolling(window, min_periods=min_periods).min/max``.
    """
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    roll_min = np.full(n, np.nan)
    roll_max = np.full(n, np.nan)
    if window < 1 or n == 0:
        return roll_min, roll_max

    min_dq = deque()
    max_dq = deque()
    for i in range(n):
        val = arr[i]
        left = i - window
        while min_dq and min_dq[0][0] <= left:
            min_dq.popleft()
        while max_dq and max_dq[0][0] <= left:
            max_dq.popleft()
        while min_dq and min_dq[-1][1] >= val:
            min_dq.pop()
        while max_dq and max_dq[-1][1] <= val:
            max_dq.pop()
        min_dq.append((i, val))
        max_dq.append((i, val))
        count = i - max(0, i - window + 1) + 1
        if count >= min_periods:
            roll_min[i] = min_dq[0][1]
            roll_max[i] = max_dq[0][1]
    return roll_min, roll_max


def compute_price_signal_divergence(
        close, signal, window: int = 60, dedup: int = 5,
        require_finite_signal_window: bool = False,
        spike_filter_top: bool = False) -> list:
    """Price-signal top/bottom divergence with rolling window + dedup.

    Used by MACD (DIF), DDE (DDX), and Volume (vol). Vectorized rolling
    extrema; only the dedup pass is sequential (5-bar lookback).
    """
    from numpy.lib.stride_tricks import sliding_window_view

    close = np.asarray(close, dtype=float)
    signal = np.asarray(signal, dtype=float)
    n = len(close)
    result = [None] * n
    if n < window:
        return result

    w = window - 1
    close_w = sliding_window_view(close, window)
    signal_w = sliding_window_view(signal, window)

    c_hi = np.nanmax(close_w, axis=1)
    c_lo = np.nanmin(close_w, axis=1)
    d_hi = np.nanmax(signal_w, axis=1)
    d_lo = np.nanmin(signal_w, axis=1)
    sig_peak_iloc = np.argmax(signal_w, axis=1)
    sig_valley_iloc = np.argmin(signal_w, axis=1)
    c_lo_iloc = np.argmin(close_w, axis=1)

    cur_c = close[w:]
    cur_d = signal[w:]
    valid = ~np.isnan(cur_c) & ~np.isnan(cur_d)
    if require_finite_signal_window:
        valid &= ~np.isnan(signal_w).any(axis=1)

    top = (
        valid
        & (sig_peak_iloc < w)
        & (d_hi != 0)
        & (cur_d < d_hi)
        & (cur_c >= c_hi * 0.98)
    )
    if spike_filter_top:
        spike = np.zeros(len(signal_w), dtype=bool)
        for i in range(len(signal_w)):
            peak = sig_peak_iloc[i]
            lo = max(0, peak - 2)
            hi = min(window, peak + 3)
            neighbors = signal_w[i, lo:hi]
            spike[i] = (neighbors >= d_hi[i] * 0.8).sum() < 2
        top &= ~spike

    with np.errstate(divide="ignore", invalid="ignore"):
        d_recovery = np.where(d_lo != 0, (cur_d - d_lo) / np.abs(d_lo), 0.0)
    bottom = (
        valid
        & (sig_valley_iloc < w)
        & (d_lo != 0)
        & (cur_d > d_lo)
        & (d_recovery > 0.1)
        & ((w - c_lo_iloc) >= 3)
        & (cur_c <= c_lo * 1.02)
    )

    for idx, i in enumerate(range(w, n)):
        if top[idx]:
            recent = any(
                result[j] == "top_divergence"
                for j in range(max(0, i - dedup), i)
            )
            if not recent:
                result[i] = "top_divergence"
        if bottom[idx]:
            recent = any(
                result[j] == "bottom_divergence"
                for j in range(max(0, i - dedup), i)
            )
            if not recent:
                result[i] = "bottom_divergence"
    return result


def sliding_window_mean_abs(y: np.ndarray, window: int) -> np.ndarray:
    """Rolling mean of |y| over a fixed window (used for slope normalization).

    Output index i is ``mean(|y[i-window+1 : i+1]|)``; windows containing any
    NaN yield NaN (matching the legacy full-window ``mean(np.abs(valid))``).
    Indices ``< window-1`` are NaN.
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    out = np.full(n, np.nan)
    if window < 1 or n < window:
        return out
    conv = np.convolve(np.abs(y), np.ones(window), mode="valid")
    out[window - 1:] = conv / window
    return out


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


def compute_history_signature(df: "pd.DataFrame", cols: list,
                              precision: int = 6) -> str:
    """Strong content signature over actual value sequences (not summary stats).

    Hashes the row-ordered, rounded values of ``cols`` (+ trade_date) so any
    real value change flips the signature, while sub-precision float noise does
    not. Replaces the lossy min/max/mean/count fingerprint for state gating.
    """
    if df is None or df.empty:
        return hashlib.sha256(b"empty").hexdigest()[:16]
    parts = []
    td = df["trade_date"].astype(str).tolist()
    parts.append("td:" + ",".join(td))
    for col in sorted(cols):
        if col not in df.columns:
            parts.append(f"{col}:absent")
            continue
        vals = df[col].to_numpy(dtype=float)
        rounded = np.where(np.isnan(vals), np.nan, np.round(vals, precision))
        parts.append(f"{col}:" + ",".join(
            "nan" if np.isnan(v) else format(v, f".{precision}f") for v in rounded
        ))
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def compute_input_fingerprint(df: "pd.DataFrame",
                              recalc_start: "Optional[str]" = None) -> str:
    """Strategy-A domain fingerprint: last_trade_date + window subset hash.

    When recalc_start is None, the window spans the full df (backward compat
    during P0.5 rollout before orchestrator passes explicit recalc_start).
    """
    if df.empty:
        return hashlib.sha256(b"empty").hexdigest()[:16]
    last_td = str(df["trade_date"].max())
    if recalc_start is not None:
        window_df = df[df["trade_date"] >= recalc_start]
    else:
        window_df = df
    window_fp = compute_fingerprint(window_df)
    raw = f"last_td:{last_td}|window:{window_fp}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_quote_groups(con, src_table: str, freq: str, columns: list[str],
                      ts_codes: list[str], chunk_size: int = 400,
                      start_date: str = None) -> dict:
    """Batch-load per-stock quote DataFrames with one query per chunk.

    Replaces the per-stock N+1 ``SELECT ... WHERE ts_code = ?`` loop in the
    calculators: a single ``WHERE ts_code IN (...)`` query per chunk, then an
    in-memory ``groupby`` splits it back into per-stock frames.

    The returned frames are byte-for-byte equivalent to the legacy per-stock
    query — same ``columns`` (without ts_code), same row order
    (``ORDER BY trade_date``), same filters:
      * daily  → ``WHERE is_suspended = 0``
      * weekly → ``JOIN dim_date ... WHERE is_week_end = 1``

    Stocks with no matching rows are absent from the result dict.
    """
    groups = {}
    cols_csv = ", ".join(columns)
    for i in range(0, len(ts_codes), chunk_size):
        chunk = ts_codes[i:i + chunk_size]
        ph = ",".join(["?"] * len(chunk))
        date_filter = ""
        params = list(chunk)
        if start_date is not None:
            date_filter = " AND d.trade_date >= ?" if freq == "weekly" else " AND trade_date >= ?"
            params.append(start_date)
        if freq == "weekly":
            d_cols = ", ".join("d." + c for c in columns)
            query = f"""
                SELECT d.ts_code, {d_cols}
                FROM {src_table} d
                JOIN dim_date dd ON d.trade_date = dd.trade_date
                WHERE d.ts_code IN ({ph}) AND dd.is_week_end = 1{date_filter}
                ORDER BY d.ts_code, d.trade_date
            """
        else:
            query = f"""
                SELECT ts_code, {cols_csv}
                FROM {src_table}
                WHERE ts_code IN ({ph}) AND is_suspended = 0{date_filter}
                ORDER BY ts_code, trade_date
            """
        big = con.execute(query, params).df()
        if big.empty:
            continue
        for ts_code, g in big.groupby("ts_code", sort=False):
            g = g.drop(columns=["ts_code"]).reset_index(drop=True)
            groups[ts_code] = g
    return groups


def resolve_ema_anchor_date(con, first_trade_date: str, freq: str):
    """Trade date immediately before the first bar in an incremental load window."""
    if freq == "weekly":
        row = con.execute("""
            SELECT trade_date FROM dim_date
            WHERE is_trade_day = 1 AND is_week_end = 1 AND trade_date < ?
            ORDER BY trade_date DESC LIMIT 1
        """, [first_trade_date]).fetchone()
    else:
        row = con.execute("""
            SELECT trade_date FROM dim_date
            WHERE is_trade_day = 1 AND trade_date < ?
            ORDER BY trade_date DESC LIMIT 1
        """, [first_trade_date]).fetchone()
    return row[0] if row else None


def load_ema_seed(con, dws_table: str, ts_code: str, trade_date: str, col: str):
    """Read one EMA column from the latest DWS snapshot at trade_date."""
    try:
        row = con.execute(f"""
            SELECT {col} FROM {dws_table}
            WHERE ts_code = ? AND trade_date = ?
            ORDER BY calc_date DESC
            LIMIT 1
        """, [ts_code, trade_date]).fetchone()
    except duckdb.CatalogException:
        return None
    if row is None or row[0] is None:
        return None
    return float(row[0])


def resolve_ema_seeds(con, dws_table: str, ts_code: str, df: "pd.DataFrame",
                      freq: str, cols, recalc_start: str):
    """Load EMA seeds from DWS at the bar before df's first trade_date."""
    from backend.config import CALC_INCREMENTAL
    if not recalc_start or not CALC_INCREMENTAL:
        return None
    if df is None or df.empty:
        return None
    anchor = resolve_ema_anchor_date(con, df.iloc[0]["trade_date"], freq)
    if anchor is None:
        return None
    seeds = {}
    for col in cols:
        val = load_ema_seed(con, dws_table, ts_code, anchor, col)
        if val is None:
            return None
        seeds[col] = val
    return seeds


def load_latest_fingerprints(con, dws_table: str,
                             ts_codes: list[str]) -> dict:
    """Batch-load the latest (MAX calc_date) input_fingerprint per stock.

    Replaces the per-stock N+1 query in check_dwd_unchanged: one window-function
    query returns {ts_code: fingerprint} for the whole group. Stocks without any
    non-null fingerprint are absent from the dict.
    """
    if not ts_codes:
        return {}
    placeholders = ",".join(["?" for _ in ts_codes])
    try:
        rows = con.execute(f"""
            SELECT ts_code, input_fingerprint FROM (
                SELECT ts_code, input_fingerprint,
                       ROW_NUMBER() OVER (PARTITION BY ts_code
                                          ORDER BY calc_date DESC, trade_date DESC) AS rn
                FROM {dws_table}
                WHERE ts_code IN ({placeholders}) AND input_fingerprint IS NOT NULL
            ) WHERE rn = 1
        """, ts_codes).fetchall()
    except duckdb.CatalogException:
        # DWS table not created yet → treat as no prior fingerprints (first calc)
        return {}
    return {r[0]: r[1] for r in rows}


def check_dwd_unchanged(con, dws_table: str, ts_code: str,
                        df: "pd.DataFrame", latest_fps: dict = None,
                        recalc_start: "Optional[str]" = None) -> bool:
    """Check if DWD input data is unchanged since last calculation.

    Uses strategy-A ``compute_input_fingerprint`` (last_td + window subset).

    When ``latest_fps`` (a {ts_code: fingerprint} dict from
    load_latest_fingerprints) is provided, the comparison uses it instead of
    issuing a per-stock SQL query — this avoids the N+1 round-trip. When None,
    falls back to a single per-stock query (backward compatible).

    Returns True if unchanged (calculation can be skipped).
    """
    new_fp = compute_input_fingerprint(df, recalc_start=recalc_start)
    if latest_fps is not None:
        return latest_fps.get(ts_code) == new_fp
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
                     input_fingerprint: str = None,
                     write_start: str = None,
                     write_end: str = None) -> int:
    """Shared DWS INSERT -- replaces individual Calculator _insert methods.

    Handles: calc_date, spec_version, input_fingerprint, to_float_safe.
    When input_fingerprint is provided, uses it directly instead of computing.
    Returns the number of rows inserted (0 when write range filters to empty).
    """
    import pandas as pd

    data_cols = [c for c in dws_cols if c != "ts_code"]
    for c in data_cols:
        if c not in df.columns:
            df[c] = None

    batch = df[data_cols].copy()
    if write_start is not None:
        batch = batch[batch["trade_date"] >= write_start]
    if write_end is not None:
        batch = batch[batch["trade_date"] <= write_end]
    if batch.empty:
        return 0
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
    return len(batch)
