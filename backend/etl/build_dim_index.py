"""Build dim_index from ods_index_basic. Full refresh (DELETE + INSERT)."""
import logging

logger = logging.getLogger(__name__)


def build_dim_index(con) -> int:
    """Build dim_index from ods_index_basic. Returns row count.

    Extracts numeric index_code from ts_code (e.g. '000001.SH' → '000001').
    Marks is_active based on exp_date vs current date.
    Marks has_valuation=1 for the 6 core indices known to support index_dailybasic.
    """
    logger.info("progress build_dim_index: started")

    con.execute("DELETE FROM dim_index")
    con.execute("""
        INSERT INTO dim_index (
            ts_code, index_code, name, fullname, market, category,
            list_date, exp_date, is_active, has_valuation
        )
        SELECT
            ts_code,
            CASE
                WHEN ts_code LIKE '%.SH' THEN SUBSTR(ts_code, 1, INSTR(ts_code, '.') - 1)
                WHEN ts_code LIKE '%.SZ' THEN SUBSTR(ts_code, 1, INSTR(ts_code, '.') - 1)
                WHEN ts_code LIKE '%.SI' THEN SUBSTR(ts_code, 1, INSTR(ts_code, '.') - 1)
                ELSE ts_code
            END AS index_code,
            name,
            fullname,
            market,
            category,
            list_date,
            exp_date,
            CASE
                WHEN exp_date IS NULL OR exp_date = '' THEN 1
                WHEN exp_date > CURRENT_DATE THEN 1
                ELSE 0
            END AS is_active,
            CASE
                WHEN ts_code IN (
                    '000001.SH', '399001.SZ', '000300.SH',
                    '000905.SH', '000016.SH', '399006.SZ'
                ) THEN 1
                ELSE 0
            END AS has_valuation
        FROM ods_index_basic
    """)

    n = con.execute("SELECT COUNT(*) FROM dim_index").fetchone()[0]
    logger.info("progress build_dim_index: done | rows=%d", n)
    return n
