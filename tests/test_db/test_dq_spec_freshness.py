"""v_dq_spec_freshness view exists after create_all_tables."""
import duckdb

from backend.db.schema import create_all_tables


def test_v_dq_spec_freshness_created():
    con = duckdb.connect(":memory:")
    create_all_tables(con)
    row = con.execute(
        """
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_name = 'v_dq_spec_freshness'
        """
    ).fetchone()
    assert int(row[0]) == 1
    con.close()
