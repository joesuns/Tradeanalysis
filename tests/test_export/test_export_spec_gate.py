"""EXPORT_SPEC_GATE warning on stale section spec."""
import logging

import duckdb

from backend.db.schema import create_all_tables


def test_export_spec_gate_warns_on_section_stale(tmp_path, monkeypatch, caplog):
    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    create_all_tables(con)
    con.execute(
        """
        INSERT INTO dws_dde_daily (
            ts_code, trade_date, ddx, ddx2, divergence, trend,
            trend_strength, alert, calc_date, input_fingerprint, spec_version
        ) VALUES ('000001.SZ', '20260616', 0.01, 0.02, NULL, 'flat', 0.0, NULL,
                  '20260616', 'fp', 'v1')
        """
    )
    con.close()

    monkeypatch.setattr("backend.config.EXPORT_SPEC_GATE", True)
    caplog.set_level(logging.WARNING)

    from backend.export_wide import export_wide_to_excel

    try:
        export_wide_to_excel(str(db), "20260616", str(tmp_path / "out.xlsx"))
    except Exception:
        pass
    assert any("export spec gate" in r.getMessage() for r in caplog.records)
