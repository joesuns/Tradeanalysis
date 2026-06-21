"""B4 alert helpers (MACD 123-aligned; DDE alert window configurable)."""
import numpy as np
import pytest

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


def test_ddx2_slope_inflection_bull_3bar():
    ddx2 = np.array([0.0, 0.0, -2.0, -1.0, 3.0], dtype=float)
    alerts = compute_ddx2_slope_alerts(ddx2, window=3, eps=0.0)
    assert alerts[-1] == "downturn_reverse"


def test_ddx2_slope_inflection_bear_3bar():
    ddx2 = np.array([0.0, 0.0, 2.0, 1.0, -3.0], dtype=float)
    alerts = compute_ddx2_slope_alerts(ddx2, window=3, eps=0.0)
    assert alerts[-1] == "upturn_reverse"


def test_dde_alert_production_window():
    assert DDE_ALERT_WINDOW == 3


@pytest.mark.parametrize("macd_input,dde_input,expected", [
    # V-shape / slope turns positive → bullish → downturn_reverse
    (np.array([1.0, -1.0, 0.5]), np.array([0.0, 0.0, -2.0, -1.0, 3.0]), "downturn_reverse"),
    # Λ-shape / slope turns negative → bearish → upturn_reverse
    (np.array([1.0, 2.0, 1.0]), np.array([0.0, 0.0, 2.0, 1.0, -3.0]), "upturn_reverse"),
])
def test_cross_indicator_alert_enum_equivalence(macd_input, dde_input, expected):
    """MACD and DDE alerts produce identical enum values for the same directional scenario."""
    macd_alerts = compute_macd_hist_turn_alerts(macd_input)
    dde_alerts = compute_ddx2_slope_alerts(dde_input, window=DDE_ALERT_WINDOW, eps=0.0)
    assert macd_alerts[-1] == dde_alerts[-1] == expected
