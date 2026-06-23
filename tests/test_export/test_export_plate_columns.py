"""Integration tests for plate/concept columns in export."""
import pytest
import pandas as pd
from unittest.mock import MagicMock


class TestPlateColumnsInExport:
    """Verify plate columns appear correctly in exported DataFrame."""

    def test_id_cols_include_plate_columns(self):
        """_ID_COLS includes tdx_industry_board, dc_concept_board, and dc_theme_board."""
        from backend.export_wide import _ID_COLS

        assert "tdx_industry_board" in _ID_COLS
        assert "dc_concept_board" in _ID_COLS
        assert "dc_theme_board" in _ID_COLS

        # Verify order: theme after concept
        concept_idx = _ID_COLS.index("dc_concept_board")
        theme_idx = _ID_COLS.index("dc_theme_board")
        assert theme_idx == concept_idx + 1, (
            f"dc_theme_board should be right after dc_concept_board, "
            f"got positions {concept_idx} and {theme_idx}"
        )

    def test_col_names_include_new_columns(self):
        """_COL_NAMES includes new plate columns."""
        from backend.export_wide import _COL_NAMES

        assert "tdx_industry_board" in _COL_NAMES
        assert "dc_concept_board" in _COL_NAMES
        assert "dc_theme_board" in _COL_NAMES
        assert _COL_NAMES["dc_theme_board"] == "所属题材"

    def test_plate_merge_with_existing_daily(self):
        """Plate enrichment merges correctly into daily DataFrame."""
        daily = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ", "830001.BJ"],
            "trade_date": ["20260620"] * 3,
            "close": [10.5, 20.3, 5.0],
        })

        enrichment = {
            "000001.SZ": {"tdx_industry_board": "银行", "dc_concept_board": "央企改革,沪深300"},
            "000002.SZ": {"tdx_industry_board": "房地产", "dc_concept_board": "物业管理"},
        }

        plate_data = []
        for ts_code, cols in enrichment.items():
            plate_data.append({
                "ts_code": ts_code,
                "tdx_industry_board": cols.get("tdx_industry_board"),
                "dc_concept_board": cols.get("dc_concept_board"),
            })
        plate_df = pd.DataFrame(plate_data)
        merged = daily.merge(plate_df, on="ts_code", how="left")

        for col in ["tdx_industry_board", "dc_concept_board"]:
            if col in merged.columns:
                merged[col] = merged[col].fillna("N/A")

        assert merged.loc[0, "tdx_industry_board"] == "银行"
        assert merged.loc[0, "dc_concept_board"] == "央企改革,沪深300"
        assert merged.loc[2, "dc_concept_board"] == "N/A"

    def test_plate_not_in_signal_cols(self):
        """Plate columns must NOT be in signal/event/state metric sets."""
        from backend.export_wide import (
            _EVENT_SIGNAL_COLS, _STATE_METRIC_COLS, _SIGNAL_COLS
        )

        for col in ["tdx_industry_board", "dc_concept_board"]:
            assert col not in _EVENT_SIGNAL_COLS, f"{col} must not be event signal"
            assert col not in _STATE_METRIC_COLS, f"{col} must not be state metric"
            assert col not in _SIGNAL_COLS, f"{col} must not be in signal set"


class TestPlateNullDisplay:
    """Verify plate columns display N/A (not '-') for missing data."""

    def test_plate_cols_not_in_event_signal(self):
        """Plate columns must NOT be classified as event signals."""
        from backend.export_wide import _EVENT_SIGNAL_COLS
        assert "tdx_industry_board" not in _EVENT_SIGNAL_COLS
        assert "dc_concept_board" not in _EVENT_SIGNAL_COLS

    def test_plate_cols_not_in_state_metric(self):
        """Plate columns must NOT be classified as state metrics (they're attributes)."""
        from backend.export_wide import _STATE_METRIC_COLS
        assert "tdx_industry_board" not in _STATE_METRIC_COLS
        assert "dc_concept_board" not in _STATE_METRIC_COLS

    def test_apply_display_nulls_leaves_na_untouched(self):
        """apply_display_nulls must not replace existing 'N/A' strings."""
        from backend.export_wide import apply_display_nulls

        df = pd.DataFrame({
            "tdx_industry_board": ["银行", "N/A", pd.NA],
            "dc_concept_board": ["新能源", pd.NA, "N/A"],
        })
        result = apply_display_nulls(df)
        # Existing "N/A" preserved; pd.NA remains as NaN (to be filled by explicit fillna)
        assert result.loc[1, "tdx_industry_board"] == "N/A"
        assert result.loc[2, "dc_concept_board"] == "N/A"
        # pd.NA is NOT filled by apply_display_nulls (not in any signal set)
        assert pd.isna(result.loc[2, "tdx_industry_board"])
        assert pd.isna(result.loc[1, "dc_concept_board"])

    def test_plate_na_filled_before_apply_display_nulls(self):
        """End-to-end: fillna before apply_display_nulls -> N/A preserved."""
        from backend.export_wide import apply_display_nulls

        df = pd.DataFrame({
            "close": [10.5, 20.3],
            "tdx_industry_board": [None, "银行"],
            "dc_concept_board": ["新能源", None],
        })
        for col in ["tdx_industry_board", "dc_concept_board"]:
            if col in df.columns:
                df[col] = df[col].fillna("N/A")

        result = apply_display_nulls(df)
        assert result.loc[0, "tdx_industry_board"] == "N/A"
        assert result.loc[1, "dc_concept_board"] == "N/A"

    def test_theme_col_not_in_signal_cols(self):
        """dc_theme_board is NOT a signal column."""
        from backend.export_wide import _SIGNAL_COLS

        assert "dc_theme_board" not in _SIGNAL_COLS

    def test_theme_col_in_classification_cols(self):
        """dc_theme_board IS a classification column (null -> N/A, not -)."""
        from backend.export_wide import _PLATE_CLASSIFICATION_COLS

        assert "dc_theme_board" in _PLATE_CLASSIFICATION_COLS
