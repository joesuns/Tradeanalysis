"""Index indicator calc pipeline — thin adapters over existing calculators."""
import logging
import time

from backend.etl.calc_macd import MACDCalculator
from backend.etl.calc_ma import MACalculator
from backend.etl.calc_volume import VolumeCalculator

logger = logging.getLogger(__name__)


# ── Calculator adapters ──────────────────────────────────────

class IndexMACDCalculator(MACDCalculator):
    """MACD calculator adapted for indices. Uses dwd_index_* as source.

    Inherits all computation logic (calculate, _compute_indicators, _insert, etc.)
    from MACDCalculator. Only overrides __init__ to set index-specific table names.
    SIGNATURE_COLS (class-level ['close_qfq']) and SPEC_VERSION ('v3') are inherited.
    """
    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_index_daily" if freq == "daily" else "dwd_index_weekly"
        self.dws_table = f"dws_index_macd_{freq}"


class IndexMACalculator(MACalculator):
    """MA calculator adapted for indices. Uses dwd_index_* as source.

    Inherits all computation logic from MACalculator.
    SIGNATURE_COLS (class-level ['close_qfq']) and SPEC_VERSION ('v2') are inherited.
    """
    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_index_daily" if freq == "daily" else "dwd_index_weekly"
        self.dws_table = f"dws_index_ma_{freq}"


class IndexVolumeCalculator(VolumeCalculator):
    """Volume calculator adapted for indices. Uses dwd_index_* as source.

    Inherits all computation logic from VolumeCalculator.
    NOTE: SIGNATURE_COLS is instance-level in VolumeCalculator (set in __init__),
    so we must set it explicitly.
    """
    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        self.src_table = "dwd_index_daily" if freq == "daily" else "dwd_index_weekly"
        self.dws_table = f"dws_index_volume_{freq}"
        self.SIGNATURE_COLS = ["close_qfq", "vol"]
        if freq == "weekly":
            self.SIGNATURE_COLS = ["close_qfq", "vol", "active_days"]


# ── Calc pipeline ────────────────────────────────────────────

def _get_tracked_index_codes(con) -> list:
    """Get active index codes from dim_index."""
    rows = con.execute(
        "SELECT ts_code FROM dim_index WHERE is_active = 1 ORDER BY ts_code"
    ).fetchall()
    return [r[0] for r in rows]


def calc_index_pipeline(con, calc_date: str) -> dict:
    """Run index calc for all tracked indices. Full calc (DELETE + recalculate).

    Returns {step_label: {calculated, skipped, elapsed}}.

    Simpler than stock calc because:
    - No adj_factor/qfq changes → no fingerprint drift
    - No daily_basic coverage gate
    - No column→indicator narrowing
    - No DDE/price_position/kpattern
    - Index data volume is tiny → FULL calc is cheap
    """
    codes = _get_tracked_index_codes(con)
    if not codes:
        logger.warning("progress calc.index: no tracked indices in dim_index")
        return {}

    logger.info("progress calc.index: %d indices for date=%s", len(codes), calc_date)
    stats = {}

    calculators = [
        (IndexMACDCalculator, "macd"),
        (IndexMACalculator, "ma"),
        (IndexVolumeCalculator, "volume"),
    ]

    for CalcCls, name in calculators:
        for freq in ("daily", "weekly"):
            calc = CalcCls(con, freq)
            label = f"index_{name}_{freq}"
            t0 = time.monotonic()

            result = calc.calculate(codes, calc_date)

            elapsed = time.monotonic() - t0
            stats[label] = {
                "calculated": result.calculated,
                "skipped": result.skipped,
                "elapsed": elapsed,
            }
            logger.info(
                "progress calc.index: %s | calculated=%d skipped_reasons=%d | %.1fs",
                label, result.calculated, len(result.skipped), elapsed,
            )

    return stats


def calc_index_refresh(con, ts_code: str, calc_date: str,
                       indicators: list = None) -> dict:
    """Force FULL recalc for a single index (e.g., after config change).

    Args:
        indicators: list of indicator names, e.g. ['macd', 'ma']. None = all three.
    """
    if indicators is None:
        indicators = ["macd", "ma", "volume"]

    stats = {}
    calc_map = {
        "macd": IndexMACDCalculator,
        "ma": IndexMACalculator,
        "volume": IndexVolumeCalculator,
    }

    for name in indicators:
        CalcCls = calc_map[name]
        for freq in ("daily", "weekly"):
            calc = CalcCls(con, freq)
            con.execute(
                f"DELETE FROM {calc.dws_table} WHERE ts_code = ?",
                [ts_code],
            )
            result = calc.calculate([ts_code], calc_date)
            stats[f"{name}_{freq}"] = result.calculated

    return stats
