"""Shared ETL test fixtures."""
from backend.etl.calc_macd import MACDCalculator


def seed_dim_date_anchor(con, trade_date: str) -> None:
    """Minimal dim_date row for weekly anchor / spec gate queries."""
    con.execute(
        """
        INSERT INTO dim_date (trade_date, is_trade_day, is_week_end)
        VALUES (?, 1, 1)
        """,
        [trade_date],
    )


def insert_prior_calc_volume(con, calc_date: str, n: int = 4000) -> None:
    """Bulk MACD rows: has_prior_calc_snapshot + L0 gate without spec_stale."""
    con.execute(
        """
        INSERT INTO dws_macd_daily (
            ts_code, trade_date, calc_date, dif, dea, macd_bar, spec_version
        )
        SELECT code, ?, ?, 0, 0, 0, ?
        FROM (
            SELECT unnest(generate_series(1, ?)) AS i,
                   printf('C%04d.SZ', i) AS code
        )
        """,
        [calc_date, calc_date, MACDCalculator.SPEC_VERSION, n],
    )


def patch_run_calc_dim_deps(monkeypatch, recalc_start: str = "20250601") -> None:
    """run_calc tests that reach auto-spec/chunk without a full dim calendar."""
    monkeypatch.setattr(
        "backend.etl.orchestrator.resolve_recalc_start",
        lambda *a, **k: recalc_start,
    )
    monkeypatch.setattr(
        "backend.etl.calc_spec_refresh.run_auto_spec_refresh_if_needed",
        lambda *a, **k: {"skipped": True},
    )
    monkeypatch.setattr(
        "backend.etl.calc_spec_gate.count_dws_spec_stale_on_trade_date",
        lambda *a, **k: {},
    )
