import duckdb
from scripts.health_check import Checker


def test_checker_expect_min():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (v INTEGER)")
    con.execute("INSERT INTO t VALUES (5)")
    c = Checker(con)
    c.expect_min("five", "SELECT v FROM t", minimum=3)
    assert c.failures == 0
    c.expect_min("too low", "SELECT v FROM t", minimum=10)
    assert c.failures == 1
    con.close()
