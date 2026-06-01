import numpy as np
from backend.etl.base import ema, sma, linear_regression_slope


def test_ema_seed_is_sma_of_first_n():
    """EMA seed = SMA of first N values."""
    prices = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 15.0, 20.0], dtype=float)
    result = ema(prices, 5)
    assert abs(result[4] - 10.0) < 0.01  # seed at index 4 = mean of first 5
    # index 5: alpha=2/(5+1)=0.333, EMA = 0.333*15 + 0.667*10 = 11.667
    assert abs(result[5] - 11.667) < 0.1


def test_ema_skips_nan_suspension_days():
    """NaN values (suspension days) carry forward previous value."""
    prices = np.array([10.0, np.nan, np.nan, 12.0, 13.0], dtype=float)
    result = ema(prices, 3)
    assert result[1] == result[0]  # NaN → carry forward
    assert result[2] == result[0]  # NaN → carry forward


def test_ema_insufficient_data_returns_all_nan():
    prices = np.array([1.0, 2.0], dtype=float)
    result = ema(prices, 5)
    assert all(np.isnan(r) for r in result)


def test_sma_basic():
    prices = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = sma(prices, 3)
    assert np.isnan(result[0]) and np.isnan(result[1])
    assert abs(result[2] - 2.0) < 0.01  # (1+2+3)/3


def test_linear_regression_slope_positive():
    y = np.array([1.0, 2.0, 4.0, 8.0, 16.0])
    slope = linear_regression_slope(y)
    assert slope > 0  # log-space upward trend


def test_linear_regression_slope_insufficient_data():
    assert linear_regression_slope(np.array([np.nan, np.nan])) == 0.0
