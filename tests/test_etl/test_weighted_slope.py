"""Golden-master tests for weighted_window_slopes (B2 polyfit vectorization).

The oracle is the exact legacy per-bar loop (np.polyfit with weights). The
vectorized closed-form must match it to 1e-9 across random data and edges.
"""
import numpy as np
from backend.etl.base import weighted_window_slopes


def _oracle_slopes(y, window, decay):
    """Frozen copy of the legacy per-bar weighted-regression loop.

    Mirrors MACD/DDE _compute_trend: full non-NaN window of exactly `window`
    points → np.polyfit slope; otherwise NaN.
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    out = np.full(n, np.nan)
    for i in range(n):
        if i < window - 1:
            continue
        segment = y[i - window + 1:i + 1]
        valid = segment[~np.isnan(segment)]
        if len(valid) < window:
            continue
        x = np.arange(window, dtype=float)
        weights = np.exp(x * decay)
        try:
            slope = float(np.polyfit(x, valid, 1, w=weights)[0])
        except (np.linalg.LinAlgError, ValueError, TypeError):
            continue
        if not np.isfinite(slope):
            continue
        out[i] = slope
    return out


def _assert_match(y, window, decay):
    got = weighted_window_slopes(y, window, decay)
    exp = _oracle_slopes(y, window, decay)
    # Both NaN in same places
    np.testing.assert_array_equal(np.isnan(got), np.isnan(exp))
    m = ~np.isnan(exp)
    np.testing.assert_allclose(got[m], exp[m], rtol=0, atol=1e-9)


def test_matches_oracle_macd_params_random():
    rng = np.random.default_rng(42)
    for _ in range(50):
        y = rng.normal(0, 1.0, size=rng.integers(5, 120))
        _assert_match(y, window=5, decay=0.15)


def test_matches_oracle_dde_params_random():
    rng = np.random.default_rng(7)
    for _ in range(50):
        y = rng.normal(0, 0.05, size=rng.integers(8, 100))
        _assert_match(y, window=8, decay=0.20)


def test_matches_oracle_unweighted():
    rng = np.random.default_rng(3)
    for _ in range(30):
        y = rng.normal(10, 2.0, size=rng.integers(5, 60))
        _assert_match(y, window=5, decay=0.0)


def test_matches_oracle_with_nans():
    rng = np.random.default_rng(99)
    for _ in range(50):
        y = rng.normal(0, 1.0, size=rng.integers(6, 80))
        # sprinkle NaNs
        idx = rng.integers(0, len(y), size=max(1, len(y) // 5))
        y[idx] = np.nan
        _assert_match(y, window=5, decay=0.15)


def test_shorter_than_window_all_nan():
    y = np.array([1.0, 2.0, 3.0])
    out = weighted_window_slopes(y, window=5, decay=0.15)
    assert np.all(np.isnan(out))


def test_leading_indices_nan():
    y = np.arange(10, dtype=float)
    out = weighted_window_slopes(y, window=5, decay=0.15)
    assert np.all(np.isnan(out[:4]))
    assert np.all(~np.isnan(out[4:]))


def test_perfect_linear_trend_slope():
    # y = 2x → slope should be 2 regardless of weights
    y = 2.0 * np.arange(20, dtype=float)
    out = weighted_window_slopes(y, window=5, decay=0.15)
    np.testing.assert_allclose(out[4:], 2.0, atol=1e-9)
