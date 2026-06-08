"""Registry of calc routing specs for append-only and fast-skip preflight."""
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
     ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol"], "quote"),
    ("kpattern", "weekly", KPatternCalculator,
     ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol"], "quote"),
    ("volume", "daily", VolumeCalculator, ["close_qfq", "vol"], "quote"),
    ("volume", "weekly", VolumeCalculator, ["close_qfq", "vol"], "quote"),
    ("priceposition", "daily", PricePositionCalculator, ["close_qfq"], "quote"),
    ("priceposition", "weekly", PricePositionCalculator, ["close_qfq"], "quote"),
    ("dde", "daily", DDECalculator, DDE_SIG_COLS, "dde"),
    ("dde", "weekly", DDECalculator, DDE_SIG_COLS, "dde"),
]


def quote_sig_col_union() -> list:
    """Union of SIGNATURE_COLS for quote-sourced indicators (excludes trade_date)."""
    cols = set()
    for _, _, _, sig_cols, source in CALC_ROUTE_SPECS:
        if source == "quote":
            cols.update(sig_cols)
    return sorted(cols)


def quote_tail_columns() -> list:
    """Columns for batch_load_quote_tails including trade_date."""
    return ["trade_date"] + quote_sig_col_union()
