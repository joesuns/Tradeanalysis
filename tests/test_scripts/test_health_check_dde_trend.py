"""Section K: DDE trend content oracle gate thresholds."""
import duckdb

from scripts.health_check import Checker, dde_trend_oracle_gate


def test_dde_trend_oracle_gate_pass():
    con = duckdb.connect(":memory:")
    c = Checker(con)
    dde_trend_oracle_gate(c, matched=200, mismatched=0)
    assert c.failures == 0
    con.close()


def test_dde_trend_oracle_gate_warn_above_point_one_percent():
    con = duckdb.connect(":memory:")
    c = Checker(con)
    dde_trend_oracle_gate(c, matched=999, mismatched=1)
    assert c.failures == 0
    con.close()


def test_dde_trend_oracle_gate_fail_above_one_percent():
    con = duckdb.connect(":memory:")
    c = Checker(con)
    dde_trend_oracle_gate(c, matched=98, mismatched=2)
    assert c.failures == 1
    con.close()


def test_checker_warn_and_fail():
    con = duckdb.connect(":memory:")
    c = Checker(con)
    c.warn("test warning")
    c.fail("test failure")
    assert c.failures == 1
    con.close()
