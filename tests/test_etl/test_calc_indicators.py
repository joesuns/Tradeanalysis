"""Contract tests: batch tail columns must cover all quote calculator compute inputs."""
import pytest

from backend.etl.calc_indicators import (
    CALC_ROUTE_SPECS,
    quote_pipeline_columns,
    quote_tail_columns,
    quote_sig_col_union,
)
from backend.etl.calc_kpattern import KPatternCalculator

# Columns each quote calculator reads from its input DataFrame at compute time.
# Keep in sync with calculator implementations — this is the regression gate.
QUOTE_COMPUTE_INPUT_COLS = {
    "macd": ["close_qfq"],
    "ma": ["close_qfq"],
    "kpattern": ["open_qfq", "high_qfq", "low_qfq", "close_qfq", "vol", "pct_chg"],
    "volume": ["close_qfq", "vol"],  # weekly also uses active_days via SIGNATURE_COLS
    "priceposition": ["close_qfq"],
}


@pytest.mark.parametrize("freq", ["daily", "weekly"])
def test_quote_tail_columns_equals_pipeline_columns(freq):
    assert quote_tail_columns(freq) == quote_pipeline_columns(freq)


@pytest.mark.parametrize("freq", ["daily", "weekly"])
def test_pipeline_columns_cover_all_quote_compute_inputs(freq):
    pipeline = set(quote_pipeline_columns(freq))
    for indicator, cols in QUOTE_COMPUTE_INPUT_COLS.items():
        if freq == "weekly" and indicator == "volume":
            cols = cols + ["active_days"]
        missing = [c for c in cols if c not in pipeline]
        assert not missing, f"{indicator}/{freq} compute needs {missing} not in pipeline"


@pytest.mark.parametrize("indicator,freq,CalcCls,sig_cols,source", CALC_ROUTE_SPECS)
def test_signature_cols_subset_of_pipeline(indicator, freq, CalcCls, sig_cols, source):
    if source != "quote":
        pytest.skip("dde uses moneyflow columns")
    pipeline = set(quote_pipeline_columns(freq))
    missing = [c for c in sig_cols if c not in pipeline]
    assert not missing, f"{indicator}/{freq} SIGNATURE_COLS {missing} not in pipeline"


def test_kpattern_signature_includes_pct_chg():
    assert "pct_chg" in KPatternCalculator.SIGNATURE_COLS


def test_quote_sig_col_union_includes_pct_chg_after_fix():
    assert "pct_chg" in quote_sig_col_union()
