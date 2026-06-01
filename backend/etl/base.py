import numpy as np


def ema(series: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average. Seed = SMA of first 'period' valid values.
    NaN values are skipped (carry forward) for suspension day handling."""
    result = np.full(len(series), np.nan)
    total_valid = np.sum(~np.isnan(series))
    if total_valid < period:
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


def linear_regression_slope(y: np.ndarray) -> float:
    """Slope of log(y) regression. Returns percent/day in log space (>0 = expanding, <0 = shrinking)."""
    y = np.array(y, dtype=float)
    mask = ~np.isnan(y) & (y > 0)
    if mask.sum() < 2:
        return 0.0
    x = np.arange(len(y), dtype=float)[mask]
    log_y = np.log(y[mask])
    slope = np.polyfit(x, log_y, 1)[0]
    return float(slope)
