"""CLI repair-dde-trend dry-run integration tests."""
from argparse import Namespace


def test_repair_dde_trend_dry_run(db_with_schema, capsys, monkeypatch):
    from backend.cli import cmd_repair_dde_trend

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_stock_basic (ts_code, list_date) VALUES ('R.SZ','20200101')"
    )
    monkeypatch.setattr("backend.db.connection.get_connection", lambda: con)
    monkeypatch.setattr("backend.db.connection.run_checkpoint", lambda _c: None)

    args = Namespace(
        date="20260612",
        freq="daily",
        ts_code=["R.SZ"],
        dry_run=True,
    )
    cmd_repair_dde_trend(args)
    out = capsys.readouterr().out
    assert "dry_run" in out or "True" in out
