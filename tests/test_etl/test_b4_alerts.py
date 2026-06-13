"""B4 alert alignment with 123 reversal signals."""
import numpy as np
import pandas as pd

from backend.etl.b4_alerts import (
    compute_ddx2_slope_alerts,
    compute_macd_hist_turn_alerts,
)


def test_macd_hist_turn_up_matches_123():
    # 123 test_hist_turn_up: macd [1.0, -1.0, 0.5]
    alerts = compute_macd_hist_turn_alerts(np.array([1.0, -1.0, 0.5]))
    assert alerts[2] == "downturn_reverse"


def test_macd_hist_turn_down():
    alerts = compute_macd_hist_turn_alerts(np.array([1.0, 2.0, 1.0]))
    assert alerts[2] == "upturn_reverse"


def test_ddx2_slope_inflection_bull_matches_123():
    n = 12
    ddx2 = np.zeros(n, dtype=float)
    ddx2[-6:] = [4.0, 3.0, 2.0, 1.0, 0.0, 5.0]
    alerts = compute_ddx2_slope_alerts(ddx2, window=5, eps=0.0)
    assert alerts[-1] == "upturn_reverse"


def test_ddx2_slope_inflection_bear_matches_123():
    n = 12
    ddx2 = np.zeros(n, dtype=float)
    ddx2[-6:] = [-4.0, -3.0, -2.0, -1.0, 0.0, -5.0]
    alerts = compute_ddx2_slope_alerts(ddx2, window=5, eps=0.0)
    assert alerts[-1] == "downturn_reverse"
