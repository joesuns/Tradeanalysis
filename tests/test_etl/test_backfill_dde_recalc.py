import pytest


def test_invalidate_dde_weekly_snapshots_calc_date_only(db_with_schema):
    from backend.etl.backfill_dde_recalc import invalidate_dde_weekly_snapshots

    con = db_with_schema
    con.execute(
        """
        INSERT INTO dws_dde_weekly
        (ts_code, trade_date, calc_date, net_mf_amount, ddx, ddx2, trend,
         trend_strength, alert, divergence, input_fingerprint, spec_version)
        VALUES
        ('A.SZ','20240105','20260612',1.0,0.1,0.1,'up',0.1,NULL,NULL,'fp1','v2'),
        ('A.SZ','20240105','20260611',1.0,0.1,0.1,'flat',0.1,NULL,NULL,'fp0','v2'),
        ('B.SZ','20240105','20260612',1.0,0.1,0.1,'down',0.1,NULL,NULL,'fp2','v2')
        """
    )
    n = invalidate_dde_weekly_snapshots(con, "20260612")
    assert n == 2
    remain = con.execute("SELECT COUNT(*) FROM dws_dde_weekly").fetchone()[0]
    assert remain == 1
    assert con.execute(
        "SELECT calc_date FROM dws_dde_weekly WHERE ts_code='A.SZ'"
    ).fetchone()[0] == "20260611"


def test_invalidate_dde_weekly_snapshots_ts_subset(db_with_schema):
    from backend.etl.backfill_dde_recalc import invalidate_dde_weekly_snapshots

    con = db_with_schema
    con.execute(
        """
        INSERT INTO dws_dde_weekly
        (ts_code, trade_date, calc_date, net_mf_amount, ddx, ddx2, trend,
         trend_strength, alert, divergence, input_fingerprint, spec_version)
        VALUES
        ('A.SZ','20240105','20260612',1.0,0.1,0.1,'up',0.1,NULL,NULL,'fp1','v2'),
        ('B.SZ','20240105','20260612',1.0,0.1,0.1,'down',0.1,NULL,NULL,'fp2','v2')
        """
    )
    n = invalidate_dde_weekly_snapshots(con, "20260612", ts_codes=["A.SZ"])
    assert n == 1
    assert con.execute(
        "SELECT COUNT(*) FROM dws_dde_weekly WHERE ts_code='B.SZ'"
    ).fetchone()[0] == 1


def test_prepare_dde_weekly_recalc_dry_run(db_with_schema, monkeypatch):
    from backend.etl.backfill_dde_recalc import prepare_dde_weekly_recalc

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_stock_basic (ts_code, list_date) VALUES ('R.SZ','20200101')"
    )

    def fake_refresh(con, ts_codes, calc_date, dry_run=False):
        assert dry_run is True
        return {"updated": 0, "records_written": 0}

    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.refresh_calc_state_fingerprints",
        fake_refresh,
    )
    stats = prepare_dde_weekly_recalc(
        con, "20260612", ts_codes=["R.SZ"], dry_run=True,
    )
    assert stats["dry_run"] is True
    assert stats["dde_weekly_rows_deleted"] == 0
    assert stats["calc_date"] == "20260612"


def test_invalidate_dde_daily_snapshots_calc_date_only(db_with_schema):
    from backend.etl.backfill_dde_recalc import invalidate_dde_daily_snapshots

    con = db_with_schema
    con.execute(
        """
        INSERT INTO dws_dde_daily
        (ts_code, trade_date, calc_date, net_mf_amount, ddx, ddx2, trend,
         trend_strength, alert, divergence, input_fingerprint, spec_version)
        VALUES
        ('A.SZ','20260612','20260612',1.0,0.1,0.1,'up',0.1,NULL,NULL,'fp1','v2'),
        ('A.SZ','20260612','20260611',1.0,0.1,0.1,'flat',0.1,NULL,NULL,'fp0','v2'),
        ('B.SZ','20260612','20260612',1.0,0.1,0.1,'down',0.1,NULL,NULL,'fp2','v2')
        """
    )
    n = invalidate_dde_daily_snapshots(con, "20260612")
    assert n == 2
    remain = con.execute("SELECT COUNT(*) FROM dws_dde_daily").fetchone()[0]
    assert remain == 1
    assert con.execute(
        "SELECT calc_date FROM dws_dde_daily WHERE ts_code='A.SZ'"
    ).fetchone()[0] == "20260611"


def test_invalidate_dde_daily_snapshots_ts_subset(db_with_schema):
    from backend.etl.backfill_dde_recalc import invalidate_dde_daily_snapshots

    con = db_with_schema
    con.execute(
        """
        INSERT INTO dws_dde_daily
        (ts_code, trade_date, calc_date, net_mf_amount, ddx, ddx2, trend,
         trend_strength, alert, divergence, input_fingerprint, spec_version)
        VALUES
        ('A.SZ','20260612','20260612',1.0,0.1,0.1,'up',0.1,NULL,NULL,'fp1','v2'),
        ('B.SZ','20260612','20260612',1.0,0.1,0.1,'down',0.1,NULL,NULL,'fp2','v2')
        """
    )
    n = invalidate_dde_daily_snapshots(con, "20260612", ts_codes=["A.SZ"])
    assert n == 1
    assert con.execute(
        "SELECT COUNT(*) FROM dws_dde_daily WHERE ts_code='B.SZ'"
    ).fetchone()[0] == 1


def test_prepare_dde_daily_recalc_dry_run(db_with_schema):
    from backend.etl.backfill_dde_recalc import prepare_dde_daily_recalc

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_stock_basic (ts_code, list_date) VALUES ('R.SZ','20200101')"
    )
    stats = prepare_dde_daily_recalc(
        con, "20260612", ts_codes=["R.SZ"], dry_run=True,
    )
    assert stats["dry_run"] is True
    assert stats["dde_daily_rows_deleted"] == 0
    assert stats["dde_daily_state_deleted"] == 0
    assert stats["calc_date"] == "20260612"


def test_purge_dde_daily_history_ts_subset(db_with_schema):
    from backend.etl.backfill_dde_recalc import purge_dde_daily_history

    con = db_with_schema
    con.execute(
        """
        INSERT INTO dws_dde_daily
        (ts_code, trade_date, calc_date, net_mf_amount, ddx, ddx2, trend,
         trend_strength, alert, divergence, input_fingerprint, spec_version)
        VALUES
        ('A.SZ','20260610','20260611',1.0,0.1,0.1,'up',0.1,NULL,NULL,'fp0','v2'),
        ('A.SZ','20260612','20260612',1.0,0.1,0.1,'up',0.1,NULL,NULL,'fp1','v2'),
        ('B.SZ','20260612','20260612',1.0,0.1,0.1,'down',0.1,NULL,NULL,'fp2','v2')
        """
    )
    n = purge_dde_daily_history(con, ["A.SZ"])
    assert n == 2
    assert con.execute(
        "SELECT COUNT(*) FROM dws_dde_daily WHERE ts_code='A.SZ'"
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM dws_dde_daily WHERE ts_code='B.SZ'"
    ).fetchone()[0] == 1


def test_prepare_dde_daily_recalc_purge_history_requires_ts_codes(db_with_schema):
    from backend.etl.backfill_dde_recalc import prepare_dde_daily_recalc

    con = db_with_schema
    con.execute(
        "INSERT INTO ods_stock_basic (ts_code, list_date) VALUES ('R.SZ','20200101')"
    )
    with pytest.raises(ValueError, match="purge_history requires"):
        prepare_dde_daily_recalc(
            con, "20260612", ts_codes=None, dry_run=False, purge_history=True,
        )
