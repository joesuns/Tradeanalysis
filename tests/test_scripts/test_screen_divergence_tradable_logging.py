"""TDD: screening script progress logging."""
import logging
from io import StringIO
from unittest.mock import patch

from scripts.screen_divergence_tradable import main


def test_screening_logs_progress(caplog):
    fake_rows = [
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260612",
            "l1": "top_divergence",
            "tradable": None,
            "reject_reason": "skip_peak",
        }
    ]
    with patch(
        "scripts.screen_divergence_tradable._list_l1_events",
        return_value=(fake_rows, 1, 0),
    ):
        with patch("scripts.screen_divergence_tradable.Path.exists", return_value=True):
            with caplog.at_level(logging.INFO):
                stdout = StringIO()
                with patch("sys.stdout", stdout):
                    rc = main(["--date", "20260612", "--db", "/tmp/x.duckdb"])
    assert rc == 0
    assert "000001.SZ" in stdout.getvalue()
    assert any("progress screening.tradable: started" in r.message for r in caplog.records)
    assert any("progress screening.tradable: done" in r.message for r in caplog.records)
