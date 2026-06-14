"""Registry of calc routing specs for append-only and fast-skip preflight."""
from typing import Dict, Tuple

from backend.etl.calc_macd import MACDCalculator
from backend.etl.calc_ma import MACalculator
from backend.etl.calc_kpattern import KPatternCalculator
from backend.etl.calc_volume import VolumeCalculator
from backend.etl.calc_price_position import PricePositionCalculator
from backend.etl.calc_dde import DDECalculator

DDE_SIG_COLS = [
    "buy_lg_vol", "sell_lg_vol", "buy_elg_vol", "sell_elg_vol",
    "total_vol", "net_mf_amount", "close_qfq",
]

# (indicator_name, freq, CalcCls, SIGNATURE_COLS, source)
# source: "quote" | "dde"
CALC_ROUTE_SPECS = [
    ("macd", "daily", MACDCalculator, ["close_qfq"], "quote"),
    ("macd", "weekly", MACDCalculator, ["close_qfq"], "quote"),
    ("ma", "daily", MACalculator, ["close_qfq"], "quote"),
    ("ma", "weekly", MACalculator, ["close_qfq"], "quote"),
    ("kpattern", "daily", KPatternCalculator,
     ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol", "pct_chg"], "quote"),
    ("kpattern", "weekly", KPatternCalculator,
     ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol", "pct_chg"], "quote"),
    ("volume", "daily", VolumeCalculator, ["close_qfq", "vol"], "quote"),
    ("volume", "weekly", VolumeCalculator, ["close_qfq", "vol", "active_days"], "quote"),
    ("priceposition", "daily", PricePositionCalculator, ["close_qfq"], "quote"),
    ("priceposition", "weekly", PricePositionCalculator, ["close_qfq"], "quote"),
    ("dde", "daily", DDECalculator, DDE_SIG_COLS, "dde"),
    ("dde", "weekly", DDECalculator, DDE_SIG_COLS, "dde"),
]

# Canonical expected spec_version per (indicator, freq) routing key.
INDICATOR_SPEC_VERSIONS: Dict[Tuple[str, str], str] = {
    (indicator_name, freq): getattr(CalcCls, "SPEC_VERSION", "v1")
    for indicator_name, freq, CalcCls, _, _ in CALC_ROUTE_SPECS
}

# Route indicator name → DWS table/view prefix (priceposition → price_position).
INDICATOR_DWS_PREFIX = {
    "priceposition": "price_position",
}


def expected_spec_version(indicator: str, freq: str) -> str:
    return INDICATOR_SPEC_VERSIONS.get((indicator, freq), "v1")


def dws_latest_view(indicator: str, freq: str) -> str:
    prefix = INDICATOR_DWS_PREFIX.get(indicator, indicator)
    return f"v_dws_{prefix}_{freq}_latest"


def quote_sig_col_union() -> list:
    """Union of SIGNATURE_COLS for quote-sourced indicators (diagnostics only).

    Batch tail loading uses quote_pipeline_columns(), not this union.
    """
    cols = set()
    for _, _, _, sig_cols, source in CALC_ROUTE_SPECS:
        if source == "quote":
            cols.update(sig_cols)
    # active_days exists only on dwd_weekly_quote — never SELECT on daily loads
    cols.discard("active_days")
    return sorted(cols)


def quote_pipeline_columns(freq: str) -> list:
    """Canonical quote compute-input columns (per-stock pipeline + batch tails)."""
    cols = [
        "trade_date", "open_qfq", "high_qfq", "low_qfq",
        "close_qfq", "vol", "pct_chg",
    ]
    if freq == "weekly":
        cols.append("active_days")
    return cols


def quote_tail_columns(freq: str = "daily") -> list:
    """Columns for batch_load_quote_tails — same as per-stock pipeline quote load."""
    return quote_pipeline_columns(freq)
