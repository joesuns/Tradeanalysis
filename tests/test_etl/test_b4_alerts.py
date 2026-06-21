"""B4 alert helpers (MACD 123-aligned; DDE alert window configurable)."""
import numpy as np

from backend.etl.b4_alerts import (
    compute_ddx2_slope_alerts,
    compute_macd_hist_turn_alerts,
)
from backend.etl.calc_dde import DDE_ALERT_WINDOW


def test_macd_hist_turn_up_matches_123():
    # 123 test_hist_turn_up: macd [1.0, -1.0, 0.5]
    alerts = compute_macd_hist_turn_alerts(np.array([1.0, -1.0, 0.5]))
    assert alerts[2] == "downturn_reverse"


def test_macd_hist_turn_down():
    alerts = compute_macd_hist_turn_alerts(np.array([1.0, 2.0, 1.0]))
    assert alerts[2] == "upturn_reverse"


def test_ddx2_slope_inflection_bull_2bar():
    ddx2 = np.array([0.0, 0.0, 1.0, 0.0, 5.0], dtype=float)
    alerts = compute_ddx2_slope_alerts(ddx2, window=2, eps=0.0)
    assert alerts[-1] == "downturn_reverse"


def test_ddx2_slope_inflection_bear_2bar():
    ddx2 = np.array([0.0, 0.0, -1.0, 0.0, -5.0], dtype=float)
    alerts = compute_ddx2_slope_alerts(ddx2, window=2, eps=0.0)
    assert alerts[-1] == "upturn_reverse"


def test_dde_alert_production_window():
    assert DDE_ALERT_WINDOW == 2


def test_bullish_alert_enum_equivalence():
    """MACD V-shape and DDE slope-up both produce downturn_reverse."""
    # MACD: V shape → bullish → downturn_reverse
    macd_alerts = compute_macd_hist_turn_alerts(np.array([1.0, -1.0, 0.5]))
    assert macd_alerts[2] == "downturn_reverse"
    # DDE: slope turns positive → bullish → downturn_reverse
    dde_alerts = compute_ddx2_slope_alerts(
        np.array([0.0, 0.0, 1.0, 0.0, 5.0]), window=2, eps=0.0
    )
    assert dde_alerts[-1] == "downturn_reverse"


def test_bearish_alert_enum_equivalence():
    """MACD Λ-shape and DDE slope-down both produce upturn_reverse."""
    # MACD: Λ shape → bearish → upturn_reverse
    macd_alerts = compute_macd_hist_turn_alerts(np.array([1.0, 2.0, 1.0]))
    assert macd_alerts[2] == "upturn_reverse"
    # DDE: slope turns negative → bearish → upturn_reverse
    dde_alerts = compute_ddx2_slope_alerts(
        np.array([0.0, 0.0, -1.0, 0.0, -5.0]), window=2, eps=0.0
    )
    assert dde_alerts[-1] == "upturn_reverse"
