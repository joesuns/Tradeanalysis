"""DIM layer construction from ODS data."""


def build_dim_stock(con) -> int:
    """Build dim_stock from ods_stock_basic. Full refresh (DELETE + INSERT).
    Maps exchange codes to Chinese names, derives sector from ts_code prefix,
    detects ST/*ST from name. Returns row count."""
    con.execute("DELETE FROM dim_stock")
    con.execute("""
    INSERT INTO dim_stock (ts_code, stock_code, symbol, name, exchange, sector, industry,
                           list_date, delist_date, is_active, is_st)
    SELECT
        ts_code,
        symbol AS stock_code,
        symbol,
        name,
        CASE exchange
            WHEN 'SSE'  THEN '上海'
            WHEN 'SZSE' THEN '深圳'
            WHEN 'BSE'  THEN '北京'
            ELSE exchange
        END AS exchange,
        CASE
            WHEN ts_code LIKE '60%' THEN '主板'
            WHEN ts_code LIKE '00%' THEN '主板'
            WHEN ts_code LIKE '30%' THEN '创业板'
            WHEN ts_code LIKE '68%' THEN '科创板'
            ELSE '北交所'
        END AS sector,
        industry,
        list_date,
        delist_date,
        CASE WHEN delist_date IS NULL OR delist_date = '' THEN 1 ELSE 0 END AS is_active,
        CASE WHEN name LIKE '%ST%' OR name LIKE '%*ST%' THEN 1 ELSE 0 END AS is_st
    FROM ods_stock_basic
    """)
    return con.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]


def build_dim_date(con) -> int:
    """Build dim_date from ods_trade_cal. Trading days only.
    Computes is_week_end, is_month_end, is_year_end, year, quarter, month, week_of_year."""
    con.execute("DELETE FROM dim_date")
    con.execute("""
    INSERT INTO dim_date (trade_date, is_trade_day, is_week_end, is_month_end, is_year_end,
                          year, quarter, month, week_of_year)
    WITH dates AS (
        SELECT
            cal_date,
            is_open,
            CAST(substr(cal_date,1,4) || '-' || substr(cal_date,5,2) || '-' || substr(cal_date,7,2) AS DATE) AS dt
        FROM ods_trade_cal
        WHERE is_open = 1
    )
    SELECT
        cal_date AS trade_date,
        is_open AS is_trade_day,
        CASE WHEN cal_date = (
            SELECT MAX(o2.cal_date) FROM dates o2
            WHERE strftime(o2.dt, '%Y-%W') = strftime(dates.dt, '%Y-%W'))
            THEN 1 ELSE 0 END AS is_week_end,
        CASE WHEN cal_date = (
            SELECT MAX(o2.cal_date) FROM dates o2
            WHERE substr(o2.cal_date,1,6) = substr(dates.cal_date,1,6))
            THEN 1 ELSE 0 END AS is_month_end,
        CASE WHEN cal_date = (
            SELECT MAX(o2.cal_date) FROM dates o2
            WHERE substr(o2.cal_date,1,4) = substr(dates.cal_date,1,4))
            THEN 1 ELSE 0 END AS is_year_end,
        CAST(substr(cal_date,1,4) AS INTEGER) AS year,
        (CAST(substr(cal_date,5,2) AS INTEGER) - 1) // 3 + 1 AS quarter,
        CAST(substr(cal_date,5,2) AS INTEGER) AS month,
        CAST(strftime(dt, '%W') AS INTEGER) AS week_of_year
    FROM dates
    """)
    return con.execute("SELECT COUNT(*) FROM dim_date").fetchone()[0]


def build_dim_concept(con) -> tuple[int, int]:
    """Build dim_concept + dim_concept_stock from ods_concept_detail.
    Returns (concept_count, mapping_count)."""
    con.execute("DELETE FROM dim_concept_stock")
    con.execute("DELETE FROM dim_concept")
    con.execute("""
    INSERT INTO dim_concept (concept_id, concept_name)
    SELECT ROW_NUMBER() OVER (ORDER BY concept_name), concept_name
    FROM (SELECT DISTINCT concept_name FROM ods_concept_detail)
    """)
    con.execute("""
    INSERT INTO dim_concept_stock (concept_id, ts_code)
    SELECT c.concept_id, o.ts_code
    FROM ods_concept_detail o
    JOIN dim_concept c ON o.concept_name = c.concept_name
    """)
    concepts = con.execute("SELECT COUNT(*) FROM dim_concept").fetchone()[0]
    mappings = con.execute("SELECT COUNT(*) FROM dim_concept_stock").fetchone()[0]
    return concepts, mappings
