"""Tests for calc_state_refresh (fingerprint realign without DWS recalc)."""
import pandas as pd

from backend.db.schema import ensure_calc_state_table
from backend.etl.calc_router import state_signature
from backend.etl.calc_state_refresh import refresh_calc_state_fingerprints


def _make_tail(last_td: str, close: float, n: int = 80) -> pd.DataFrame:
    base = int(last_td) - n
    dates = [str(base + i) for i in range(n)] + [last_td]
    return pd.DataFrame({"trade_date": dates, "close_qfq": [close] * (n + 1)})


def test_refresh_updates_stale_fingerprint(monkeypatch):
    """When DWD tail changes, history_fp is rewritten; DWS tables untouched."""
    from backend.etl import calc_state_refresh as mod

    codes = ["SA.SZ"]
    calc_date = "20260609"
    last_td = "20260608"
    old_tail = _make_tail(last_td, 10.0)
    new_tail = _make_tail(last_td, 11.0)
    old_fp = state_signature(old_tail, last_td, ["close_qfq"])
    new_fp = state_signature(new_tail, last_td, ["close_qfq"])
    assert old_fp != new_fp

    import duckdb
    con = duckdb.connect(":memory:")
    ensure_calc_state_table(con)
    con.execute("""
        INSERT INTO dws_calc_state
            (ts_code, freq, indicator, last_trade_date, history_fp,
             quote_latest_adj, spec_version, updated_calc_date)
        VALUES ('SA.SZ', 'daily', 'macd', ?, ?, NULL, 'v3', '20260608')
    """, [last_td, old_fp])

    def fake_quote_tails(_con, ts_codes, freq, columns, window=245):
        return {"SA.SZ": new_tail} if freq == "daily" else {}

    def fake_dde_tails(_con, ts_codes, freq, window=245):
        return {}

    monkeypatch.setattr(mod, "batch_load_quote_tails", fake_quote_tails)
    monkeypatch.setattr(mod, "batch_load_dde_tails", fake_dde_tails)
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.CALC_ROUTE_SPECS",
        [("macd", "daily", type("C", (), {"SPEC_VERSION": "v3"}), ["close_qfq"], "quote")],
    )

    summary = refresh_calc_state_fingerprints(con, codes, calc_date, dry_run=False)
    assert summary["keys_updated"] == 1
    row = con.execute("""
        SELECT history_fp, updated_calc_date FROM dws_calc_state
        WHERE ts_code = 'SA.SZ' AND indicator = 'macd'
    """).fetchone()
    assert row[0] == new_fp
    assert row[1] == calc_date
    con.close()


def test_refresh_updates_spec_version_when_fp_unchanged(monkeypatch):
    """Stale spec_version alone must be rewritten (avoids spurious FULL routing)."""
    from backend.etl import calc_state_refresh as mod

    last_td = "20260608"
    tail = _make_tail(last_td, 10.0)
    fp = state_signature(tail, last_td, ["close_qfq"])

    import duckdb
    con = duckdb.connect(":memory:")
    ensure_calc_state_table(con)
    con.execute("""
        INSERT INTO dws_calc_state
            (ts_code, freq, indicator, last_trade_date, history_fp,
             quote_latest_adj, spec_version, updated_calc_date)
        VALUES ('SA.SZ', 'daily', 'macd', ?, ?, NULL, 'v1', '20260608')
    """, [last_td, fp])

    monkeypatch.setattr(
        mod, "batch_load_quote_tails",
        lambda *_a, **_k: {"SA.SZ": tail},
    )
    monkeypatch.setattr(mod, "batch_load_dde_tails", lambda *_a, **_k: {})
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.CALC_ROUTE_SPECS",
        [("macd", "daily", type("C", (), {"SPEC_VERSION": "v3"}), ["close_qfq"], "quote")],
    )

    summary = refresh_calc_state_fingerprints(con, ["SA.SZ"], "20260609", dry_run=False)
    assert summary["keys_updated"] == 1
    row = con.execute(
        "SELECT history_fp, spec_version FROM dws_calc_state WHERE ts_code = 'SA.SZ'"
    ).fetchone()
    assert row[0] == fp
    assert row[1] == "v3"
    con.close()


def test_refresh_dry_run_writes_nothing(monkeypatch):
    from backend.etl import calc_state_refresh as mod

    last_td = "20260608"
    old_fp = state_signature(_make_tail(last_td, 10.0), last_td, ["close_qfq"])

    import duckdb
    con = duckdb.connect(":memory:")
    ensure_calc_state_table(con)
    con.execute("""
        INSERT INTO dws_calc_state
            (ts_code, freq, indicator, last_trade_date, history_fp,
             quote_latest_adj, spec_version, updated_calc_date)
        VALUES ('SA.SZ', 'daily', 'macd', ?, ?, NULL, 'v3', '20260608')
    """, [last_td, old_fp])

    new_tail = _make_tail(last_td, 99.0)

    monkeypatch.setattr(
        mod, "batch_load_quote_tails",
        lambda *_a, **_k: {"SA.SZ": new_tail},
    )
    monkeypatch.setattr(mod, "batch_load_dde_tails", lambda *_a, **_k: {})
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.CALC_ROUTE_SPECS",
        [("macd", "daily", type("C", (), {"SPEC_VERSION": "v3"}), ["close_qfq"], "quote")],
    )

    summary = refresh_calc_state_fingerprints(con, ["SA.SZ"], "20260609", dry_run=True)
    assert summary["keys_updated"] == 1
    assert summary["records_written"] == 0
    row = con.execute("SELECT history_fp FROM dws_calc_state WHERE ts_code = 'SA.SZ'").fetchone()
    assert row[0] == old_fp
    con.close()


def test_maybe_refresh_skipped_when_flag_off(monkeypatch):
    from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

    calls = []

    def fake_refresh(con, codes, calc_date, dry_run=False, return_artifacts=False):
        calls.append((codes, calc_date))
        return {"records_written": 1}

    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.refresh_calc_state_fingerprints",
        fake_refresh,
    )
    monkeypatch.setattr("backend.config.DWD_REBUILD_REFRESH_STATE", False)

    import duckdb
    con = duckdb.connect(":memory:")
    out = maybe_refresh_state_after_dwd_rebuild(
        con, ["A.SZ"], "20260610", {"daily_quote": 1},
    )
    assert out is None
    assert calls == []
    con.close()


def test_maybe_refresh_runs_when_dwd_result_nonempty(monkeypatch):
    from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

    calls = []

    def fake_refresh(con, codes, calc_date, dry_run=False, return_artifacts=False, **kwargs):
        calls.append((list(codes), calc_date, dry_run, return_artifacts))
        return {"records_written": 3, "chunk_stocks": 0}

    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.refresh_calc_state_fingerprints",
        fake_refresh,
    )
    monkeypatch.setattr("backend.config.DWD_REBUILD_REFRESH_STATE", True)

    import duckdb
    con = duckdb.connect(":memory:")
    summary = maybe_refresh_state_after_dwd_rebuild(
        con, ["A.SZ", "B.SZ"], "20260610",
        {"daily_quote": 2, "weekly_quote": 0, "moneyflow": 1},
    )
    assert summary["records_written"] == 3
    assert calls == [(["A.SZ", "B.SZ"], "20260610", False, False)]
    con.close()


def test_maybe_refresh_skipped_when_dwd_result_empty(monkeypatch):
    from backend.etl.calc_state_refresh import maybe_refresh_state_after_dwd_rebuild

    calls = []
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.refresh_calc_state_fingerprints",
        lambda *a, **k: calls.append(1),
    )
    monkeypatch.setattr("backend.config.DWD_REBUILD_REFRESH_STATE", True)

    import duckdb
    con = duckdb.connect(":memory:")
    assert maybe_refresh_state_after_dwd_rebuild(con, ["A.SZ"], "20260610", {}) is None
    assert maybe_refresh_state_after_dwd_rebuild(
        con, ["A.SZ"], "20260610",
        {"daily_quote": 0, "weekly_quote": 0, "moneyflow": 0},
    ) is None
    assert calls == []
    con.close()


def test_refresh_return_artifacts_includes_modes(monkeypatch):
    """return_artifacts=True yields stock_modes for calc hot path."""
    from backend.etl import calc_state_refresh as mod
    from backend.etl.calc_preflight_context import build_context_from_refresh

    last_td = "20260608"
    new_td = "20260609"
    old_tail = _make_tail(last_td, 10.0)
    new_tail = _make_tail(last_td, 10.0)
    new_tail = pd.concat(
        [new_tail, pd.DataFrame({"trade_date": [new_td], "close_qfq": [10.5]})],
        ignore_index=True,
    )
    old_fp = state_signature(old_tail, last_td, ["close_qfq"])

    import duckdb
    con = duckdb.connect(":memory:")
    ensure_calc_state_table(con)
    con.execute("""
        INSERT INTO dws_calc_state
            (ts_code, freq, indicator, last_trade_date, history_fp,
             quote_latest_adj, spec_version, updated_calc_date)
        VALUES ('SA.SZ', 'daily', 'macd', ?, ?, NULL, 'v3', '20260608')
    """, [last_td, old_fp])

    def fake_quote_tails(_con, ts_codes, freq, columns, window=245):
        if freq == "daily":
            return {"SA.SZ": new_tail}
        return {}

    def fake_dde_tails(_con, ts_codes, freq, window=245):
        return {}

    monkeypatch.setattr(mod, "batch_load_quote_tails", fake_quote_tails)
    monkeypatch.setattr(mod, "batch_load_dde_tails", fake_dde_tails)
    _one_spec = [
        ("macd", "daily", type("C", (), {"SPEC_VERSION": "v3"}), ["close_qfq"], "quote"),
    ]
    monkeypatch.setattr("backend.etl.calc_state_refresh.CALC_ROUTE_SPECS", _one_spec)
    monkeypatch.setattr("backend.etl.calc_fast_skip.CALC_ROUTE_SPECS", _one_spec)

    summary, tails_bundle = refresh_calc_state_fingerprints(
        con, ["SA.SZ"], "20260609", dry_run=False, return_artifacts=True,
    )
    # fp at anchored last_td (20260608) unchanged when only a newer bar exists in tail
    assert summary["keys_updated"] == 0
    assert "SA.SZ" in tails_bundle["stock_modes"]
    assert ("macd", "daily") in tails_bundle["stock_modes"]["SA.SZ"]
    mode, new_bars = tails_bundle["stock_modes"]["SA.SZ"][("macd", "daily")]
    assert mode == "APPEND"
    assert new_bars == [new_td]

    ctx = build_context_from_refresh(
        calc_date="20260609",
        stale_codes=["SA.SZ"],
        summary=summary,
        state_map=tails_bundle["state_map"],
        tails_bundle=tails_bundle,
    )
    assert ctx.source == "refresh_state"
    assert ctx.stock_modes["SA.SZ"][("macd", "daily")][0] == "APPEND"
    con.close()


def test_refresh_without_artifacts_skips_preflight(monkeypatch):
    """CLI refresh-state path must not run post-preflight (return_artifacts=False)."""
    from backend.etl import calc_state_refresh as mod

    last_td = "20260608"
    tail = _make_tail(last_td, 10.0)
    fp = state_signature(tail, last_td, ["close_qfq"])

    import duckdb
    con = duckdb.connect(":memory:")
    ensure_calc_state_table(con)
    con.execute("""
        INSERT INTO dws_calc_state
            (ts_code, freq, indicator, last_trade_date, history_fp,
             quote_latest_adj, spec_version, updated_calc_date)
        VALUES ('SA.SZ', 'daily', 'macd', ?, ?, NULL, 'v3', '20260608')
    """, [last_td, fp])

    preflight_calls = []
    monkeypatch.setattr(
        mod, "batch_load_quote_tails",
        lambda *_a, **_k: {"SA.SZ": tail},
    )
    monkeypatch.setattr(mod, "batch_load_dde_tails", lambda *_a, **_k: {})

    def _track_preflight(*args, **kwargs):
        preflight_calls.append(1)
        return None, {}

    monkeypatch.setattr(mod, "preflight_stock_modes_with_fps", _track_preflight)
    monkeypatch.setattr(
        "backend.etl.calc_state_refresh.CALC_ROUTE_SPECS",
        [("macd", "daily", type("C", (), {"SPEC_VERSION": "v3"}), ["close_qfq"], "quote")],
    )

    summary = refresh_calc_state_fingerprints(
        con, ["SA.SZ"], "20260609", dry_run=False, return_artifacts=False,
    )
    assert summary["preflight_skip"] == 0
    assert summary["preflight_append"] == 0
    assert preflight_calls == []
    con.close()


def test_refresh_isolated_tail_load_uses_isolated_loader(monkeypatch):
    """isolated_tail_load=True must not use inline _load_refresh_tails_sequential_on_con."""
    from backend.etl import calc_state_refresh as mod

    isolated_calls = []
    inline_calls = []

    monkeypatch.setattr(
        mod, "_load_refresh_tails_isolated",
        lambda ts_codes, n: isolated_calls.append((ts_codes, n)) or (
            {}, {}, {}, {}, {},
        ),
    )
    monkeypatch.setattr(
        mod, "_load_refresh_tails_sequential_on_con",
        lambda con, ts_codes, n: inline_calls.append(1) or ({}, {}, {}, {}, {}),
    )

    import duckdb
    con = duckdb.connect(":memory:")
    summary = mod.refresh_calc_state_fingerprints(
        con, ["SA.SZ"], "20260609", dry_run=True, isolated_tail_load=True,
    )
    assert summary["tail_load_mode"] == "isolated"
    assert len(isolated_calls) == 1
    assert inline_calls == []
    con.close()
