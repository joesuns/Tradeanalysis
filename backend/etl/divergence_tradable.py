"""Tradable divergence consumer layer (L2) over L1 structure divergence."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Literal, Optional, Tuple

import duckdb
import pandas as pd

from backend.etl.divergence_structure import (
    StructureEvent,
    trace_dde_structure_events,
    trace_macd_structure_events,
)

TRADABLE_TG_LAG_MAX = 1
TRACE_LOOKBACK = 250

RejectReason = Literal["skip_peak", "tg_lag", "zone_mismatch"]

REJECT_REASON_ZH = {
    "skip_peak": "隔峰",
    "tg_lag": "滞后",
    "zone_mismatch": "区域",
}

DIVERGENCE_ZH = {
    "top_divergence": "顶背离",
    "bottom_divergence": "底背离",
}


@dataclass
class TradableEnrichStats:
    """Aggregated enrich counts for one freq (export observability)."""

    freq: str
    l1_macd: int = 0
    l1_dde: int = 0
    tradable: int = 0
    reject: int = 0
    reject_skip_peak: int = 0
    reject_tg_lag: int = 0
    reject_zone_mismatch: int = 0
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TradableVerdict:
    l1_label: Optional[str]
    tradable_label: Optional[str]
    reject_reason: Optional[RejectReason]
    path: str
    tg_lag_bars: int
    trade_date: Optional[str] = None


def classify_tradable(event: StructureEvent) -> TradableVerdict:
    """Apply three hard gates: direct path, TG lag, zone consistency."""
    label = event.l1_label
    if event.path == "skip_peak":
        return TradableVerdict(
            label, None, "skip_peak", event.path, event.tg_lag_bars, event.trade_date,
        )
    if event.tg_lag_bars > TRADABLE_TG_LAG_MAX:
        return TradableVerdict(
            label, None, "tg_lag", event.path, event.tg_lag_bars, event.trade_date,
        )
    if not event.zone_ok:
        return TradableVerdict(
            label, None, "zone_mismatch", event.path, event.tg_lag_bars, event.trade_date,
        )
    return TradableVerdict(
        label, label, None, event.path, event.tg_lag_bars, event.trade_date,
    )


def _normalize_td(trade_date: str) -> str:
    return str(trade_date).replace("-", "")[:8]


def _load_trace_frame(
    con,
    ts_code: str,
    freq: str,
    indicator: str,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """Load tail history using DWS latest indicator values (not EMA recompute)."""
    if indicator == "macd":
        view = "v_dws_macd_daily_latest" if freq == "daily" else "v_dws_macd_weekly_latest"
        quote = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        week_filter = (
            " AND dd.is_week_end = 1" if freq == "weekly" else " AND q.is_suspended = 0"
        )
        join_dim = (
            " JOIN dim_date dd ON q.trade_date = dd.trade_date" if freq == "weekly" else ""
        )
        cols = "m.dif, m.dea, m.macd_bar"
    else:
        view = "v_dws_dde_daily_latest" if freq == "daily" else "v_dws_dde_weekly_latest"
        quote = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        week_filter = (
            " AND dd.is_week_end = 1" if freq == "weekly" else " AND q.is_suspended = 0"
        )
        join_dim = (
            " JOIN dim_date dd ON q.trade_date = dd.trade_date" if freq == "weekly" else ""
        )
        cols = "m.ddx, m.ddx2"

    end_clause = ""
    params: list = [ts_code]
    if end_date:
        end_clause = " AND q.trade_date <= ?"
        params.append(_normalize_td(end_date))

    sql = f"""
        SELECT q.trade_date, q.close_qfq, {cols}
        FROM {quote} q
        {join_dim}
        JOIN {view} m ON q.ts_code = m.ts_code AND q.trade_date = m.trade_date
        WHERE q.ts_code = ?{week_filter}{end_clause}
        ORDER BY q.trade_date DESC
        LIMIT {TRACE_LOOKBACK}
    """
    df = con.execute(sql, params).df()
    if df.empty:
        return df
    return df.sort_values("trade_date").reset_index(drop=True)


def _load_l1_from_view(
    con,
    ts_code: str,
    trade_date: str,
    freq: str,
    indicator: str,
) -> Optional[str]:
    view = {
        ("macd", "daily"): "v_dws_macd_daily_latest",
        ("macd", "weekly"): "v_dws_macd_weekly_latest",
        ("dde", "daily"): "v_dws_dde_daily_latest",
        ("dde", "weekly"): "v_dws_dde_weekly_latest",
    }[(indicator, freq)]
    row = con.execute(
        f"SELECT divergence FROM {view} WHERE ts_code = ? AND trade_date = ?",
        [ts_code, _normalize_td(trade_date)],
    ).fetchone()
    return row[0] if row and row[0] else None


def _zone_ok_for_l1(l1_label: str, row: pd.Series, indicator: str) -> bool:
    if indicator == "macd":
        bar = float(row["macd_bar"])
        if l1_label == "top_divergence":
            return bar > 0
        return bar < 0
    ddx = float(row["ddx"])
    if l1_label == "top_divergence":
        return ddx > 0
    return ddx < 0


def _fallback_verdict(
    l1_label: str,
    trade_date: str,
    row: pd.Series,
    indicator: str,
) -> TradableVerdict:
    """Conservative verdict when L1 exists in DWS but trace cannot reproduce TG metadata."""
    zone_ok = _zone_ok_for_l1(l1_label, row, indicator)
    if not zone_ok:
        return TradableVerdict(
            l1_label, None, "zone_mismatch", "unknown", TRACE_LOOKBACK, trade_date,
        )
    return TradableVerdict(
        l1_label, None, "skip_peak", "unknown", TRACE_LOOKBACK, trade_date,
    )


def _trace_events_for_indicator(
    df: pd.DataFrame,
    indicator: str,
) -> List[StructureEvent]:
    trade_dates = df["trade_date"].astype(str).tolist()
    close = df["close_qfq"].values
    if indicator == "macd":
        return trace_macd_structure_events(
            close,
            df["dif"].values,
            df["dea"].values,
            df["macd_bar"].values,
            trade_dates=trade_dates,
        )
    return trace_dde_structure_events(
        close,
        df["ddx"].values,
        df["ddx2"].values,
        trade_dates=trade_dates,
    )


def evaluate_tradable_for_case(
    db_path: str,
    ts_code: str,
    trade_date: str,
    freq: str,
    indicator: str,
    con=None,
) -> TradableVerdict:
    """Evaluate tradable verdict for one L1 event (integration / screening)."""
    td = _normalize_td(trade_date)
    own_con = con is None
    if own_con:
        con = duckdb.connect(db_path, read_only=True)
    try:
        l1 = _load_l1_from_view(con, ts_code, td, freq, indicator)
        if not l1:
            return TradableVerdict(None, None, None, "", 0, td)
        return _verdict_for_l1_row(con, ts_code, freq, indicator, td, l1)
    finally:
        if own_con:
            con.close()


def _verdict_for_l1_row(
    con,
    ts_code: str,
    freq: str,
    indicator: str,
    trade_date: str,
    l1_label: str,
) -> TradableVerdict:
    td = _normalize_td(trade_date)
    df = _load_trace_frame(con, ts_code, freq, indicator, end_date=td)
    if df.empty:
        return TradableVerdict(l1_label, None, "skip_peak", "unknown", TRACE_LOOKBACK, td)

    events = _trace_events_for_indicator(df, indicator)
    for ev in events:
        if _normalize_td(ev.trade_date or "") == td and ev.l1_label == l1_label:
            return classify_tradable(ev)

    row = df[df["trade_date"].astype(str) == td]
    if row.empty:
        return TradableVerdict(l1_label, None, "skip_peak", "unknown", TRACE_LOOKBACK, td)
    return _fallback_verdict(l1_label, td, row.iloc[0], indicator)


def enrich_tradable_columns(
    df: pd.DataFrame,
    con,
    freq: str = "daily",
) -> Tuple[pd.DataFrame, TradableEnrichStats]:
    """Add tradable/reject columns for rows with L1 macd/dde divergence."""
    stats = TradableEnrichStats(freq=freq)
    if df.empty:
        return df, stats

    t0 = time.monotonic()
    out = df.copy()
    pairs = [
        ("macd", "macd_divergence", "macd_divergence_tradable", "macd_divergence_reject"),
        ("dde", "dde_divergence", "dde_divergence_tradable", "dde_divergence_reject"),
    ]
    verdict_cache = {}
    for indicator, l1_col, trad_col, reject_col in pairs:
        if l1_col not in out.columns:
            continue
        out[trad_col] = None
        out[reject_col] = None
        mask = out[l1_col].notna()
        if not mask.any():
            continue
        if indicator == "macd":
            stats.l1_macd = int(mask.sum())
        else:
            stats.l1_dde = int(mask.sum())
        for idx in out.index[mask]:
            ts_code = out.at[idx, "ts_code"]
            l1 = out.at[idx, l1_col]
            td = str(out.at[idx, "trade_date"])
            cache_key = (ts_code, freq, indicator, _normalize_td(td), l1)
            if cache_key not in verdict_cache:
                verdict_cache[cache_key] = _verdict_for_l1_row(
                    con, ts_code, freq, indicator, td, l1,
                )
            verdict = verdict_cache[cache_key]
            if verdict.tradable_label:
                out.at[idx, trad_col] = verdict.tradable_label
                stats.tradable += 1
            elif verdict.reject_reason:
                out.at[idx, reject_col] = verdict.reject_reason
                stats.reject += 1
                if verdict.reject_reason == "skip_peak":
                    stats.reject_skip_peak += 1
                elif verdict.reject_reason == "tg_lag":
                    stats.reject_tg_lag += 1
                elif verdict.reject_reason == "zone_mismatch":
                    stats.reject_zone_mismatch += 1
    stats.elapsed_sec = round(time.monotonic() - t0, 1)
    return out, stats


def tradable_label_zh(label: Optional[str]) -> str:
    if not label:
        return "-"
    return DIVERGENCE_ZH.get(label, label)


def reject_reason_zh(reason: Optional[str]) -> str:
    if not reason:
        return "-"
    return REJECT_REASON_ZH.get(reason, reason)
