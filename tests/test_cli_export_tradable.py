"""CLI run_export tradable enrich audit in ods_etl_log."""
import json


def test_build_export_data_completeness_shape():
    from backend.export_wide import build_export_data_completeness

    dc = build_export_data_completeness(
        "20260612",
        {
            "daily": {"freq": "daily", "l1_macd": 5, "elapsed_sec": 1.2},
            "weekly": {"freq": "weekly", "l1_macd": 2, "elapsed_sec": 0.8},
        },
    )
    parsed = json.loads(json.dumps(dc))
    assert parsed["tradable_enrich"]["daily"]["l1_macd"] == 5
