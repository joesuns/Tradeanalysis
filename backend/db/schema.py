"""
Complete DDL for the stock analysis data model.

Tables: ODS(7) + DIM(4) + DWD(3) + DWS(10) = 24
Views: 10 latest views + 4 ADS wide views = 14
Indexes: DWS(20) + DWD(3) + ODS(3) + DIM(1) = 27

Usage:
    from backend.db.schema import create_all_tables, drop_all_tables
"""

import duckdb

# ============================================================
# ODS LAYER (7 tables) — Original Data Source, 1:1 tushare
# ============================================================

_ODS_DDL = [
    # 3.1 ods_stock_basic
    """CREATE TABLE IF NOT EXISTS ods_stock_basic (
        ts_code        TEXT PRIMARY KEY,
        symbol         TEXT,
        name           TEXT,
        area           TEXT,
        industry       TEXT,
        exchange       TEXT,
        list_date      TEXT,
        delist_date    TEXT,
        raw_json       TEXT,
        fetched_at     TEXT DEFAULT (now())
    )""",

    # 3.2 ods_daily
    """CREATE TABLE IF NOT EXISTS ods_daily (
        ts_code        TEXT,
        trade_date     TEXT,
        open           REAL,
        high           REAL,
        low            REAL,
        close          REAL,
        vol            REAL,
        amount         REAL,
        pct_chg        REAL,
        adj_factor     REAL,
        fetched_at     TEXT DEFAULT (now()),
        PRIMARY KEY (ts_code, trade_date)
    )""",

    # 3.3 ods_daily_basic
    """CREATE TABLE IF NOT EXISTS ods_daily_basic (
        ts_code        TEXT,
        trade_date     TEXT,
        total_mv       REAL,
        circ_mv        REAL,
        pe_ttm         REAL,
        turnover_rate  REAL,
        volume_ratio   REAL,
        fetched_at     TEXT DEFAULT (now()),
        PRIMARY KEY (ts_code, trade_date)
    )""",

    # 3.4 ods_moneyflow
    """CREATE TABLE IF NOT EXISTS ods_moneyflow (
        ts_code        TEXT,
        trade_date     TEXT,
        buy_sm_vol     REAL,
        buy_sm_amount  REAL,
        sell_sm_vol    REAL,
        sell_sm_amount REAL,
        buy_md_vol     REAL,
        buy_md_amount  REAL,
        sell_md_vol    REAL,
        sell_md_amount REAL,
        buy_lg_vol     REAL,
        buy_lg_amount  REAL,
        sell_lg_vol    REAL,
        sell_lg_amount REAL,
        buy_elg_vol    REAL,
        buy_elg_amount REAL,
        sell_elg_vol   REAL,
        sell_elg_amount REAL,
        net_mf_vol     REAL,
        net_mf_amount  REAL,
        net_amount_dc  REAL,
        fetched_at     TEXT DEFAULT (now()),
        PRIMARY KEY (ts_code, trade_date)
    )""",

    # 3.5 ods_trade_cal
    """CREATE TABLE IF NOT EXISTS ods_trade_cal (
        cal_date       TEXT PRIMARY KEY,
        is_open        INTEGER,
        pretrade_date  TEXT
    )""",

    # 3.6 ods_concept_detail
    """CREATE TABLE IF NOT EXISTS ods_concept_detail (
        concept_name   TEXT,
        ts_code        TEXT,
        fetched_at     TEXT DEFAULT (now()),
        PRIMARY KEY (concept_name, ts_code)
    )""",

    # 12.2 ods_etl_log — UUID primary key avoids race conditions with concurrent ETL
    """CREATE TABLE IF NOT EXISTS ods_etl_log (
        id                TEXT PRIMARY KEY,
        step_name         TEXT,
        started_at        TEXT,
        finished_at       TEXT,
        status            TEXT,
        row_count         INTEGER,
        error_msg         TEXT,
        data_completeness TEXT,
        min_trade_date    TEXT,
        max_trade_date    TEXT
    )""",

    # 12.3 ods_calc_skip_log — records why a stock was skipped during calculation
    """CREATE TABLE IF NOT EXISTS ods_calc_skip_log (
        calc_date      TEXT NOT NULL,
        ts_code        TEXT NOT NULL,
        indicator      TEXT NOT NULL,
        freq           TEXT NOT NULL,
        reason         TEXT NOT NULL,
        detail         TEXT,
        PRIMARY KEY (calc_date, ts_code, indicator, freq)
    )""",
]

def _migrate_etl_log(con):
    """Add min_trade_date, max_trade_date columns for databases created before migration.

    Uses DESCRIBE to detect existing columns — safe to run repeatedly.
    """
    try:
        cols = {r[0] for r in con.execute("DESCRIBE ods_etl_log").fetchall()}
        if "min_trade_date" not in cols:
            con.execute("ALTER TABLE ods_etl_log ADD COLUMN min_trade_date TEXT")
        if "max_trade_date" not in cols:
            con.execute("ALTER TABLE ods_etl_log ADD COLUMN max_trade_date TEXT")
    except Exception:
        pass  # table may not exist yet (fresh DB creation race)


def _migrate_dde_trend_strength(con):
    """Add trend_strength column to DDE tables for databases created before migration.

    Uses DESCRIBE to detect existing columns — safe to run repeatedly.
    """
    for table in ("dws_dde_daily", "dws_dde_weekly"):
        try:
            cols = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
            if "trend_strength" not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN trend_strength REAL")
        except Exception:
            pass  # table may not exist yet (fresh DB creation race)


def _migrate_volume_new_columns(con):
    """Add volume_ratio, trend_strength, divergence columns to existing volume tables.

    Safe to run on new databases (columns already exist = no-op).
    """
    for freq in ("daily", "weekly"):
        table = f"dws_volume_{freq}"
        for col, col_type in [
            ("volume_ratio", "REAL"),
            ("trend_strength", "REAL"),
            ("divergence", "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # column already exists


def _migrate_dde_b4_inputs(con: duckdb.DuckDBPyConnection):
    """Add circ_mv / net_amount_dc for 123-aligned DDE trend (B4 gate)."""
    for table, col in [
        ("ods_daily_basic", "circ_mv"),
        ("ods_moneyflow", "net_amount_dc"),
        ("dwd_daily_quote", "circ_mv"),
        ("dwd_daily_moneyflow", "net_amount_dc"),
    ]:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
        except Exception:
            pass


def _migrate_dws_fingerprint(con: duckdb.DuckDBPyConnection):
    """Add input_fingerprint and spec_version columns to all existing DWS tables."""
    for ind in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]:
        for freq in ["daily", "weekly"]:
            table = f"dws_{ind}_{freq}"
            for col, col_type in [
                ("input_fingerprint", "TEXT"),
                ("spec_version", "TEXT DEFAULT 'v1'"),
            ]:
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                except Exception:
                    pass



# ============================================================
# DIM LAYER (4 tables) — Dimension tables
# ============================================================

_DIM_DDL = [
    # 4.1 dim_stock
    """CREATE TABLE IF NOT EXISTS dim_stock (
        ts_code        TEXT PRIMARY KEY,
        stock_code     TEXT,
        symbol         TEXT,
        name           TEXT,
        exchange       TEXT,
        sector         TEXT,
        industry       TEXT,
        list_date      TEXT,
        delist_date    TEXT,
        is_active      INTEGER DEFAULT 1,
        is_st          INTEGER DEFAULT 0
    )""",

    # 4.2 dim_date
    """CREATE TABLE IF NOT EXISTS dim_date (
        trade_date     TEXT PRIMARY KEY,
        is_trade_day   INTEGER,
        is_week_end    INTEGER,
        is_month_end   INTEGER,
        is_year_end    INTEGER,
        year           INTEGER,
        quarter        INTEGER,
        month          INTEGER,
        week_of_year   INTEGER
    )""",

    # 4.3 dim_concept
    """CREATE TABLE IF NOT EXISTS dim_concept (
        concept_id     INTEGER PRIMARY KEY,
        concept_name   TEXT UNIQUE
    )""",

    # 4.3 dim_concept_stock
    """CREATE TABLE IF NOT EXISTS dim_concept_stock (
        concept_id     INTEGER REFERENCES dim_concept(concept_id),
        ts_code        TEXT REFERENCES dim_stock(ts_code),
        PRIMARY KEY (concept_id, ts_code)
    )""",
]

# ============================================================
# DWD LAYER (3 tables) — Detail Wide Tables
# ============================================================

_DWD_DDL = [
    # 5.1 dwd_daily_quote
    """CREATE TABLE IF NOT EXISTS dwd_daily_quote (
        ts_code        TEXT,
        trade_date     TEXT,
        open_qfq       REAL,
        high_qfq       REAL,
        low_qfq        REAL,
        close_qfq      REAL,
        vol            REAL,
        amount         REAL,
        pct_chg        REAL,
        total_mv       REAL,
        circ_mv        REAL,
        pe_ttm         REAL,
        turnover_rate  REAL,
        volume_ratio   REAL,
        is_suspended   INTEGER DEFAULT 0,
        PRIMARY KEY (ts_code, trade_date)
    )""",

    # 5.2 dwd_weekly_quote
    """CREATE TABLE IF NOT EXISTS dwd_weekly_quote (
        ts_code        TEXT,
        trade_date     TEXT,
        open_qfq       REAL,
        high_qfq       REAL,
        low_qfq        REAL,
        close_qfq      REAL,
        vol            REAL,
        amount         REAL,
        pct_chg        REAL,
        total_mv       REAL,
        pe_ttm         REAL,
        turnover_rate  REAL,
        volume_ratio   REAL,
        active_days    INTEGER,
        PRIMARY KEY (ts_code, trade_date)
    )""",

    # 5.3 dwd_daily_moneyflow
    """CREATE TABLE IF NOT EXISTS dwd_daily_moneyflow (
        ts_code        TEXT,
        trade_date     TEXT,
        net_mf_vol     REAL,
        net_mf_amount  REAL,
        buy_lg_vol     REAL,
        sell_lg_vol    REAL,
        buy_elg_vol    REAL,
        sell_elg_vol   REAL,
        total_vol      REAL,
        net_amount_dc  REAL,
        PRIMARY KEY (ts_code, trade_date)
    )""",
]

# ============================================================
# DWS LAYER (10 tables) — Technical Indicator Summary
# Template-driven: 5 indicators x 2 frequencies (daily/weekly)
# ============================================================

_DWS_DDL = {
    # 6.1 K-line Pattern
    "kpattern": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code        TEXT,
        trade_date     TEXT,
        yang_bao_yin   INTEGER,
        yang_ke_yin    INTEGER,
        mu_bei_xian    INTEGER,
        bi_lei_zhen    INTEGER,
        gao_kai_chang_yin INTEGER,
        yin_bao_yang   INTEGER,
        yin_ke_yang    INTEGER,
        strength       REAL,
        calc_date      TEXT,
        input_fingerprint TEXT,
        spec_version     TEXT DEFAULT 'v1',
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (yang_bao_yin IN (0, 1)),
        CHECK (yang_ke_yin IN (0, 1)),
        CHECK (mu_bei_xian IN (0, 1)),
        CHECK (bi_lei_zhen IN (0, 1)),
        CHECK (gao_kai_chang_yin IN (0, 1)),
        CHECK (yin_bao_yang IN (0, 1)),
        CHECK (yin_ke_yang IN (0, 1)),
        CHECK (strength IS NULL OR (strength >= 0.0 AND strength <= 1.0))
    )""",

    # 6.2 MACD
    "macd": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code        TEXT,
        trade_date     TEXT,
        ema_12         REAL,
        ema_26         REAL,
        dif            REAL,
        dea            REAL,
        macd_bar       REAL,
        divergence     TEXT,
        zone           TEXT,
        turning_point  TEXT,
        alert          TEXT,
        trend          TEXT,
        trend_strength REAL,
        calc_date      TEXT,
        input_fingerprint TEXT,
        spec_version     TEXT DEFAULT 'v1',
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (divergence IN ('top_divergence', 'bottom_divergence') OR divergence IS NULL),
        CHECK (zone IN ('bull', 'bear') OR zone IS NULL),
        CHECK (turning_point IN ('golden_cross', 'dead_cross', 'near_golden', 'near_dead') OR turning_point IS NULL),
        CHECK (alert IN ('upturn_reverse', 'downturn_reverse', 'upturn_flat', 'downturn_flat') OR alert IS NULL),
        CHECK (trend IN ('up', 'down', 'flat'))
    )""",

    # 6.3 Moving Average
    "ma": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code        TEXT,
        trade_date     TEXT,
        ma_5           REAL,
        ma_10          REAL,
        bias_ma5       REAL,
        bias_ma10      REAL,
        ma5_slope      REAL,
        ma10_slope     REAL,
        alignment      TEXT,
        turning_point  TEXT,
        calc_date      TEXT,
        input_fingerprint TEXT,
        spec_version     TEXT DEFAULT 'v1',
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (alignment IN ('bull_strong', 'bull_building', 'bull_weakening', 'bull_rolling',
                              'bear_strong', 'bear_building', 'bear_weakening', 'bear_rolling',
                              'tangle', 'sideways') OR alignment IS NULL),
        CHECK (turning_point IN ('golden_cross', 'dead_cross', 'near_golden', 'near_dead') OR turning_point IS NULL)
    )""",

    # 6.4 DDE
    "dde": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code        TEXT,
        trade_date     TEXT,
        net_mf_amount  REAL,
        ddx            REAL,
        ddx2           REAL,
        trend          TEXT,
        trend_strength REAL,
        alert          TEXT,
        divergence     TEXT,
        calc_date      TEXT,
        input_fingerprint TEXT,
        spec_version     TEXT DEFAULT 'v1',
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (trend IN ('up', 'down', 'flat')),
        CHECK (alert IN ('upturn_reverse', 'downturn_reverse', 'upturn_flat', 'downturn_flat') OR alert IS NULL),
        CHECK (divergence IN ('top_divergence', 'bottom_divergence') OR divergence IS NULL)
    )""",

    # 6.5 Volume
    "volume": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code        TEXT,
        trade_date     TEXT,
        ma_vol_5       REAL,
        pct_vol_rank   REAL,
        zone           TEXT,
        trend          TEXT,
        volume_ratio   REAL,
        trend_strength REAL,
        divergence     TEXT,
        calc_date      TEXT,
        input_fingerprint TEXT,
        spec_version     TEXT DEFAULT 'v1',
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (pct_vol_rank >= 0 AND pct_vol_rank <= 100),
        CHECK (zone IN ('explosive', 'low_volume', 'normal')),
        CHECK (trend IN ('expanding', 'shrinking', 'flat')),
        CHECK (divergence IN ('top_divergence', 'bottom_divergence') OR divergence IS NULL)
    )""",

    # 6.6 Price Position
    "price_position": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code              TEXT,
        trade_date           TEXT,
        price_position_60d   REAL,
        price_position_120d  REAL,
        price_position_250d  REAL,
        calc_date            TEXT,
        input_fingerprint TEXT,
        spec_version     TEXT DEFAULT 'v1',
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (price_position_60d IS NULL OR (price_position_60d >= 0 AND price_position_60d <= 100)),
        CHECK (price_position_120d IS NULL OR (price_position_120d >= 0 AND price_position_120d <= 100)),
        CHECK (price_position_250d IS NULL OR (price_position_250d >= 0 AND price_position_250d <= 100))
    )""",
}

# ============================================================
# INDEXES: DWS (36) + DWD (3) + ODS (8) + DIM (1) = 48
# ============================================================

_DWS_INDEX_DDL = []
for _indicator in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]:
    for _freq in ["daily", "weekly"]:
        _table = f"dws_{_indicator}_{_freq}"
        # Time-series: pull history for one stock
        _DWS_INDEX_DDL.append(
            f"CREATE INDEX IF NOT EXISTS idx_{_indicator}_{_freq}_cd "
            f"ON {_table}(ts_code, trade_date DESC)"
        )
        # Cross-section: pull all stocks for one date
        _DWS_INDEX_DDL.append(
            f"CREATE INDEX IF NOT EXISTS idx_{_indicator}_{_freq}_dc "
            f"ON {_table}(trade_date, ts_code)"
        )
        # Prune: seek superseded snapshots by calc_date
        _DWS_INDEX_DDL.append(
            f"CREATE INDEX IF NOT EXISTS idx_{_indicator}_{_freq}_cdate "
            f"ON {_table}(calc_date, ts_code, trade_date)"
        )

_DWD_INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_dwd_daily_cd ON dwd_daily_quote(ts_code, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_dwd_mf_cd ON dwd_daily_moneyflow(ts_code, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_dwd_weekly_cd ON dwd_weekly_quote(ts_code, trade_date)",
]

_ODS_INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_ods_daily_date ON ods_daily(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_ods_daily_basic_date ON ods_daily_basic(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_ods_moneyflow_date ON ods_moneyflow(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_etl_log_step ON ods_etl_log(step_name)",
    "CREATE INDEX IF NOT EXISTS idx_etl_log_started ON ods_etl_log(started_at)",
    "CREATE INDEX IF NOT EXISTS idx_etl_log_status ON ods_etl_log(status)",
    "CREATE INDEX IF NOT EXISTS idx_skip_log_cd ON ods_calc_skip_log(calc_date)",
    "CREATE INDEX IF NOT EXISTS idx_skip_log_ind ON ods_calc_skip_log(indicator, reason)",
]

_DIM_INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_dim_stock_code ON dim_stock(stock_code)",
]

# ============================================================
# VIEWS: 10 latest views + 4 ADS wide views = 14
# ============================================================

_LATEST_VIEW_DDL = []
for _indicator in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]:
    for _freq in ["daily", "weekly"]:
        _table = f"dws_{_indicator}_{_freq}"
        _view = f"v_dws_{_indicator}_{_freq}_latest"
        # Pick the newest snapshot per (ts_code, trade_date). QUALIFY +
        # ROW_NUMBER() scans the table once, vs the old correlated subquery
        # that re-ran MAX(calc_date) for every row (O(snapshots) per row).
        _LATEST_VIEW_DDL.append(f"""
            CREATE OR REPLACE VIEW {_view} AS
            SELECT *
            FROM {_table}
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY ts_code, trade_date
                ORDER BY calc_date DESC
            ) = 1
        """)

_DQ_SPEC_EXPECTED = {
    "macd": "v3",
    "ma": "v2",
    "kpattern": "v1",
    "dde": "v3",
    "volume": "v2",
    "price_position": "v1",
}
_DQ_ROUTE_INDICATOR = {
    "macd": "macd",
    "ma": "ma",
    "kpattern": "kpattern",
    "dde": "dde",
    "volume": "volume",
    "price_position": "priceposition",
}

_DQ_SPEC_FRESHNESS_PARTS = []
for _table_ind in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]:
    _expected = _DQ_SPEC_EXPECTED[_table_ind]
    _route_ind = _DQ_ROUTE_INDICATOR[_table_ind]
    for _freq in ["daily", "weekly"]:
        _view = f"v_dws_{_table_ind}_{_freq}_latest"
        _DQ_SPEC_FRESHNESS_PARTS.append(f"""
            SELECT '{_route_ind}' AS indicator, '{_freq}' AS freq,
                   trade_date AS anchor_trade_date,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (
                       WHERE COALESCE(spec_version, 'v1') = '{_expected}'
                   ) AS spec_ok,
                   COUNT(*) FILTER (
                       WHERE COALESCE(spec_version, 'v1') <> '{_expected}'
                   ) AS spec_stale,
                   '{_expected}' AS expected_spec
            FROM {_view}
            GROUP BY trade_date
        """)

_V_DQ_SPEC_FRESHNESS_DDL = (
    "CREATE OR REPLACE VIEW v_dq_spec_freshness AS\n"
    + "\nUNION ALL\n".join(_DQ_SPEC_FRESHNESS_PARTS)
)

_V_INDICATOR_AVAILABILITY_DDL = """
CREATE VIEW IF NOT EXISTS v_indicator_availability AS
WITH latest_calc AS (
    SELECT COALESCE(MAX(calc_date), '') AS calc_date FROM ods_calc_skip_log
),
indicators AS (
    SELECT 'macd' AS indicator, 'daily' AS freq
    UNION ALL SELECT 'macd', 'weekly'
    UNION ALL SELECT 'ma', 'daily'
    UNION ALL SELECT 'ma', 'weekly'
    UNION ALL SELECT 'kpattern', 'daily'
    UNION ALL SELECT 'kpattern', 'weekly'
    UNION ALL SELECT 'dde', 'daily'
    UNION ALL SELECT 'dde', 'weekly'
    UNION ALL SELECT 'volume', 'daily'
    UNION ALL SELECT 'volume', 'weekly'
    UNION ALL SELECT 'price_position', 'daily'
    UNION ALL SELECT 'price_position', 'weekly'
),
stock_ind AS (
    SELECT s.ts_code, s.name, s.exchange, s.list_date, s.delist_date,
           i.indicator, i.freq
    FROM dim_stock s
    CROSS JOIN indicators i
    WHERE s.is_active = 1
)
SELECT
    si.ts_code,
    si.name,
    si.exchange,
    si.indicator,
    si.freq,
    CASE
        WHEN sl.reason IS NULL THEN 'unknown'
        WHEN sl.reason = 'source_unavailable' THEN 'unavailable'
        WHEN sl.reason = 'delisted' THEN 'historical'
        WHEN sl.reason = 'insufficient_rows' THEN 'partial'
        WHEN sl.reason IN ('no_dwd_data', 'fetch_failed') THEN 'missing'
        ELSE 'available'
    END AS status,
    COALESCE(sl.detail, '') AS detail
FROM stock_ind si
LEFT JOIN ods_calc_skip_log sl
    ON si.ts_code = sl.ts_code
    AND si.indicator = sl.indicator
    AND si.freq = sl.freq
    AND sl.calc_date = (SELECT calc_date FROM latest_calc)
"""

_ADS_WIDE_VIEWS_DDL = [
    # 7.1 v_ads_analysis_wide_daily
    """CREATE OR REPLACE VIEW v_ads_analysis_wide_daily AS
    SELECT
        'D'             AS freq,
        q.trade_date,
        q.ts_code,
        s.stock_code,
        s.name           AS stock_name,
        s.exchange       AS exchange,
        s.sector         AS sector,
        s.industry       AS industry,
        s.is_st          AS is_st,

        q.close_qfq      AS close,
        q.pct_chg        AS pct_chg,
        q.vol            AS vol,
        q.amount         AS amount,
        q.total_mv       AS total_mv,
        q.pe_ttm         AS pe_ttm,
        q.turnover_rate  AS turnover_rate,

        CASE
            WHEN k.yang_ke_yin = 1    THEN 'yang_ke_yin'
            WHEN k.yang_bao_yin = 1   THEN 'yang_bao_yin'
            WHEN k.yin_ke_yang = 1    THEN 'contrarian_yin_ke_yang'
            WHEN k.yin_bao_yang = 1   THEN 'contrarian_yin_bao_yang'
            WHEN k.mu_bei_xian = 1    THEN 'mu_bei_xian'
            WHEN k.bi_lei_zhen = 1    THEN 'bi_lei_zhen'
            WHEN k.gao_kai_chang_yin = 1 THEN 'gao_kai_chang_yin'
            ELSE NULL
        END              AS kpattern,
        k.strength       AS kpattern_strength,

        pp.price_position_60d,
        pp.price_position_120d,
        pp.price_position_250d,

        -- Composite volume-price signals
        CASE
            -- 缩量突破（最强：筹码锁定，次日胜率最高）
            WHEN pp.price_position_60d > 98 AND q.pct_chg > 3
                 AND v.volume_ratio < 0.9 AND v.pct_vol_rank < 60
                THEN 'breakout_tight'
            -- 温和放量突破（中等）
            WHEN pp.price_position_60d > 98 AND q.pct_chg > 3
                 AND v.volume_ratio BETWEEN 0.9 AND 1.5
                THEN 'breakout_moderate'
            -- 爆量突破（警惕：多空分歧大，次日胜率最低）
            WHEN pp.price_position_60d > 98 AND q.pct_chg > 3
                 AND v.volume_ratio > 1.5 AND v.pct_vol_rank > 80
                THEN 'breakout_heavy'
            -- 以下保留原有逻辑
            WHEN pp.price_position_60d > 85 AND v.zone = 'explosive'
                 AND q.pct_chg BETWEEN -2 AND 2
                THEN 'volume_climax'
            WHEN pp.price_position_60d < 15 AND v.zone = 'low_volume'
                THEN 'volume_dry_up'
            WHEN c.turning_point = 'golden_cross' AND v.divergence = 'top_divergence'
                THEN 'golden_cross_weakened'
            WHEN c.turning_point = 'dead_cross' AND v.divergence = 'bottom_divergence'
                THEN 'dead_cross_weakened'
            ELSE NULL
        END              AS vol_signal,

        c.ema_12, c.ema_26, c.dif, c.dea, c.macd_bar,
        c.divergence     AS macd_divergence,
        c.zone           AS macd_zone,
        c.turning_point  AS macd_turning_point,
        c.alert          AS macd_alert,
        c.trend          AS macd_trend,
        c.trend_strength AS macd_trend_strength,

        a.ma_5, a.ma_10,
        a.bias_ma5, a.bias_ma10,
        a.ma5_slope, a.ma10_slope,
        CASE a.alignment
            WHEN 'bull_strong'    THEN '多头强势 — 两线同步上行，持仓舒适区'
            WHEN 'bull_building'  THEN '多头初建 — MA5已拐头向上，MA10惯性下行'
            WHEN 'bull_weakening' THEN '多头衰竭 — MA5先拐头向下，即将死叉前兆'
            WHEN 'bull_rolling'   THEN '多头翻转 — 两线均下行，死叉边缘'
            WHEN 'bear_strong'    THEN '空头强势 — 两线同步下行，持币观望区'
            WHEN 'bear_building'  THEN '空头初建 — 死叉后MA10惯性未消，下跌中继'
            WHEN 'bear_weakening' THEN '空头衰竭 — MA5尝试上拐，空方减弱'
            WHEN 'bear_rolling'   THEN '空头翻转 — 两线均上行，金叉边缘'
            WHEN 'sideways'      THEN '均线走平 — 双斜率近零，方向待定'
            WHEN 'tangle'         THEN '均线缠绕 — 方向不明，观望'
            ELSE NULL
        END              AS ma_alignment,
        a.turning_point  AS ma_turning_point,

        d.net_mf_amount, d.ddx, d.ddx2,
        d.trend          AS dde_trend,
        d.trend_strength AS dde_trend_strength,
        d.alert          AS dde_alert,
        d.divergence     AS dde_divergence,

        v.ma_vol_5, v.pct_vol_rank,
        v.zone           AS vol_zone,
        v.trend          AS vol_trend,
        v.volume_ratio   AS vol_ratio,
        v.trend_strength AS vol_trend_strength,
        v.divergence     AS vol_divergence,

        -- Sparse risk alerts (~10/day) — unusual volume without clear catalyst
        CASE
            WHEN v.volume_ratio > 2.5
                 AND pp.price_position_60d BETWEEN 20 AND 80
                 AND v.pct_vol_rank > 85
                THEN '异常放量 —— 非极端价位突然爆量，注意规避'
            ELSE NULL
        END              AS risk_alert

    FROM dwd_daily_quote q
    LEFT JOIN dim_stock s                  ON q.ts_code = s.ts_code
    LEFT JOIN v_dws_macd_daily_latest           c ON q.ts_code = c.ts_code AND q.trade_date = c.trade_date
    LEFT JOIN v_dws_kpattern_daily_latest      k ON q.ts_code = k.ts_code AND q.trade_date = k.trade_date
    LEFT JOIN v_dws_ma_daily_latest            a ON q.ts_code = a.ts_code AND q.trade_date = a.trade_date
    LEFT JOIN v_dws_dde_daily_latest           d ON q.ts_code = d.ts_code AND q.trade_date = d.trade_date
    LEFT JOIN v_dws_volume_daily_latest        v ON q.ts_code = v.ts_code AND q.trade_date = v.trade_date
    LEFT JOIN v_dws_price_position_daily_latest pp ON q.ts_code = pp.ts_code AND q.trade_date = pp.trade_date""",

    # 7.1 v_ads_analysis_wide_weekly
    """CREATE OR REPLACE VIEW v_ads_analysis_wide_weekly AS
    SELECT
        'W'             AS freq,
        qw.trade_date,
        qw.ts_code,
        s.stock_code,
        s.name           AS stock_name,
        s.exchange       AS exchange,
        s.sector         AS sector,
        s.industry       AS industry,
        s.is_st          AS is_st,

        qw.close_qfq     AS close,
        qw.pct_chg       AS pct_chg,
        qw.vol           AS vol,
        qw.amount        AS amount,
        qw.total_mv      AS total_mv,
        qw.pe_ttm        AS pe_ttm,
        qw.turnover_rate AS turnover_rate,

        CASE
            WHEN kw.yang_ke_yin = 1    THEN 'yang_ke_yin'
            WHEN kw.yang_bao_yin = 1   THEN 'yang_bao_yin'
            WHEN kw.yin_ke_yang = 1    THEN 'contrarian_yin_ke_yang'
            WHEN kw.yin_bao_yang = 1   THEN 'contrarian_yin_bao_yang'
            WHEN kw.mu_bei_xian = 1    THEN 'mu_bei_xian'
            WHEN kw.bi_lei_zhen = 1    THEN 'bi_lei_zhen'
            WHEN kw.gao_kai_chang_yin = 1 THEN 'gao_kai_chang_yin'
            ELSE NULL
        END              AS kpattern,
        kw.strength      AS kpattern_strength,

        ppw.price_position_60d,
        ppw.price_position_120d,
        ppw.price_position_250d,

        -- Composite volume-price signals
        CASE
            -- 缩量突破（最强：筹码锁定，次日胜率最高）
            WHEN ppw.price_position_60d > 98 AND qw.pct_chg > 3
                 AND vw.volume_ratio < 0.9 AND vw.pct_vol_rank < 60
                THEN 'breakout_tight'
            -- 温和放量突破（中等）
            WHEN ppw.price_position_60d > 98 AND qw.pct_chg > 3
                 AND vw.volume_ratio BETWEEN 0.9 AND 1.5
                THEN 'breakout_moderate'
            -- 爆量突破（警惕：多空分歧大，次日胜率最低）
            WHEN ppw.price_position_60d > 98 AND qw.pct_chg > 3
                 AND vw.volume_ratio > 1.5 AND vw.pct_vol_rank > 80
                THEN 'breakout_heavy'
            -- 以下保留原有逻辑
            WHEN ppw.price_position_60d > 85 AND vw.zone = 'explosive'
                 AND qw.pct_chg BETWEEN -2 AND 2
                THEN 'volume_climax'
            WHEN ppw.price_position_60d < 15 AND vw.zone = 'low_volume'
                THEN 'volume_dry_up'
            WHEN cw.turning_point = 'golden_cross' AND vw.divergence = 'top_divergence'
                THEN 'golden_cross_weakened'
            WHEN cw.turning_point = 'dead_cross' AND vw.divergence = 'bottom_divergence'
                THEN 'dead_cross_weakened'
            ELSE NULL
        END              AS vol_signal,

        cw.ema_12, cw.ema_26, cw.dif, cw.dea, cw.macd_bar,
        cw.divergence    AS macd_divergence,
        cw.zone          AS macd_zone,
        cw.turning_point AS macd_turning_point,
        cw.alert         AS macd_alert,
        cw.trend         AS macd_trend,
        cw.trend_strength AS macd_trend_strength,

        aw.ma_5, aw.ma_10,
        aw.bias_ma5, aw.bias_ma10,
        aw.ma5_slope, aw.ma10_slope,
        CASE aw.alignment
            WHEN 'bull_strong'    THEN '多头强势 — 两线同步上行，持仓舒适区'
            WHEN 'bull_building'  THEN '多头初建 — MA5已拐头向上，MA10惯性下行'
            WHEN 'bull_weakening' THEN '多头衰竭 — MA5先拐头向下，即将死叉前兆'
            WHEN 'bull_rolling'   THEN '多头翻转 — 两线均下行，死叉边缘'
            WHEN 'bear_strong'    THEN '空头强势 — 两线同步下行，持币观望区'
            WHEN 'bear_building'  THEN '空头初建 — 死叉后MA10惯性未消，下跌中继'
            WHEN 'bear_weakening' THEN '空头衰竭 — MA5尝试上拐，空方减弱'
            WHEN 'bear_rolling'   THEN '空头翻转 — 两线均上行，金叉边缘'
            WHEN 'sideways'      THEN '均线走平 — 双斜率近零，方向待定'
            WHEN 'tangle'         THEN '均线缠绕 — 方向不明，观望'
            ELSE NULL
        END              AS ma_alignment,
        aw.turning_point AS ma_turning_point,

        dw.net_mf_amount, dw.ddx, dw.ddx2,
        dw.trend          AS dde_trend,
        dw.trend_strength AS dde_trend_strength,
        dw.alert          AS dde_alert,
        dw.divergence     AS dde_divergence,

        vw.ma_vol_5, vw.pct_vol_rank,
        vw.zone           AS vol_zone,
        vw.trend          AS vol_trend,
        vw.volume_ratio   AS vol_ratio,
        vw.trend_strength AS vol_trend_strength,
        vw.divergence     AS vol_divergence,

        -- Sparse risk alerts (~1/week) — unusual volume without clear catalyst
        CASE
            WHEN vw.volume_ratio > 2.5
                 AND ppw.price_position_60d BETWEEN 20 AND 80
                 AND vw.pct_vol_rank > 85
                THEN '异常放量 —— 非极端价位突然爆量，注意规避'
            ELSE NULL
        END              AS risk_alert

    FROM dwd_weekly_quote qw
    LEFT JOIN dim_stock s                      ON qw.ts_code = s.ts_code
    LEFT JOIN v_dws_macd_weekly_latest           cw ON qw.ts_code = cw.ts_code AND qw.trade_date = cw.trade_date
    LEFT JOIN v_dws_kpattern_weekly_latest      kw ON qw.ts_code = kw.ts_code AND qw.trade_date = kw.trade_date
    LEFT JOIN v_dws_ma_weekly_latest            aw ON qw.ts_code = aw.ts_code AND qw.trade_date = aw.trade_date
    LEFT JOIN v_dws_dde_weekly_latest           dw ON qw.ts_code = dw.ts_code AND qw.trade_date = dw.trade_date
    LEFT JOIN v_dws_volume_weekly_latest        vw ON qw.ts_code = vw.ts_code AND qw.trade_date = vw.trade_date
    LEFT JOIN v_dws_price_position_weekly_latest ppw ON qw.ts_code = ppw.ts_code AND qw.trade_date = ppw.trade_date""",

    # 7.1 v_ads_index_wide (uses c.trade_date, c.ts_code — NOT m.trade_date)
    """CREATE OR REPLACE VIEW v_ads_index_wide AS
    SELECT
        'D'             AS freq,
        c.trade_date,
        c.ts_code,
        '000001'        AS stock_code,
        '上证指数' AS index_name,

        q.close_qfq      AS close,
        q.pct_chg        AS pct_chg,
        q.vol            AS vol,
        q.amount         AS amount,

        CASE
            WHEN k.yang_ke_yin = 1    THEN 'yang_ke_yin'
            WHEN k.yang_bao_yin = 1   THEN 'yang_bao_yin'
            WHEN k.yin_ke_yang = 1    THEN 'contrarian_yin_ke_yang'
            WHEN k.yin_bao_yang = 1   THEN 'contrarian_yin_bao_yang'
            WHEN k.mu_bei_xian = 1    THEN 'mu_bei_xian'
            WHEN k.bi_lei_zhen = 1    THEN 'bi_lei_zhen'
            WHEN k.gao_kai_chang_yin = 1 THEN 'gao_kai_chang_yin'
            ELSE NULL
        END              AS kpattern,
        k.strength       AS kpattern_strength,

        c.ema_12, c.ema_26, c.dif, c.dea, c.macd_bar,
        c.divergence     AS macd_divergence,
        c.zone           AS macd_zone,
        c.turning_point  AS macd_turning_point,
        c.alert          AS macd_alert,
        c.trend          AS macd_trend,
        c.trend_strength AS macd_trend_strength,

        a.ma_5, a.ma_10,
        a.bias_ma5, a.bias_ma10,
        a.ma5_slope, a.ma10_slope,
        CASE a.alignment
            WHEN 'bull_strong'    THEN '多头强势 — 两线同步上行，持仓舒适区'
            WHEN 'bull_building'  THEN '多头初建 — MA5已拐头向上，MA10惯性下行'
            WHEN 'bull_weakening' THEN '多头衰竭 — MA5先拐头向下，即将死叉前兆'
            WHEN 'bull_rolling'   THEN '多头翻转 — 两线均下行，死叉边缘'
            WHEN 'bear_strong'    THEN '空头强势 — 两线同步下行，持币观望区'
            WHEN 'bear_building'  THEN '空头初建 — 死叉后MA10惯性未消，下跌中继'
            WHEN 'bear_weakening' THEN '空头衰竭 — MA5尝试上拐，空方减弱'
            WHEN 'bear_rolling'   THEN '空头翻转 — 两线均上行，金叉边缘'
            WHEN 'sideways'      THEN '均线走平 — 双斜率近零，方向待定'
            WHEN 'tangle'         THEN '均线缠绕 — 方向不明，观望'
            ELSE NULL
        END              AS ma_alignment,
        a.turning_point  AS ma_turning_point,

        NULL             AS net_mf_amount,
        NULL             AS ddx,
        NULL             AS ddx2,
        NULL             AS dde_trend,
        NULL             AS dde_alert,
        NULL             AS dde_divergence,

        v.ma_vol_5, v.pct_vol_rank,
        v.zone           AS vol_zone,
        v.trend          AS vol_trend,
        v.volume_ratio   AS vol_ratio,
        v.trend_strength AS vol_trend_strength,
        v.divergence     AS vol_divergence,

        pp.price_position_60d,
        pp.price_position_120d,
        pp.price_position_250d

    FROM v_dws_macd_daily_latest c
    LEFT JOIN v_dws_kpattern_daily_latest k ON c.ts_code = k.ts_code AND c.trade_date = k.trade_date
    LEFT JOIN v_dws_ma_daily_latest      a ON c.ts_code = a.ts_code AND c.trade_date = a.trade_date
    LEFT JOIN v_dws_volume_daily_latest  v ON c.ts_code = v.ts_code AND c.trade_date = v.trade_date
    LEFT JOIN v_dws_price_position_daily_latest pp ON c.ts_code = pp.ts_code AND c.trade_date = pp.trade_date
    LEFT JOIN dwd_daily_quote            q ON c.ts_code = q.ts_code AND c.trade_date = q.trade_date
    WHERE c.ts_code = '000001.SH'""",

    # 7.1 v_ads_index_wide_weekly (uses c.trade_date, c.ts_code)
    """CREATE OR REPLACE VIEW v_ads_index_wide_weekly AS
    SELECT
        'W'             AS freq,
        c.trade_date,
        c.ts_code,
        '000001'        AS stock_code,
        '上证指数' AS index_name,
        q.close_qfq      AS close,  q.pct_chg AS pct_chg,  q.vol AS vol,  q.amount AS amount,
        CASE
            WHEN k.yang_ke_yin = 1    THEN 'yang_ke_yin'
            WHEN k.yang_bao_yin = 1   THEN 'yang_bao_yin'
            WHEN k.yin_ke_yang = 1    THEN 'contrarian_yin_ke_yang'
            WHEN k.yin_bao_yang = 1   THEN 'contrarian_yin_bao_yang'
            WHEN k.mu_bei_xian = 1    THEN 'mu_bei_xian'
            WHEN k.bi_lei_zhen = 1    THEN 'bi_lei_zhen'
            WHEN k.gao_kai_chang_yin = 1 THEN 'gao_kai_chang_yin'
            ELSE NULL
        END              AS kpattern,
        k.strength       AS kpattern_strength,
        c.ema_12, c.ema_26, c.dif, c.dea, c.macd_bar,
        c.divergence AS macd_divergence, c.zone AS macd_zone,
        c.turning_point AS macd_turning_point, c.alert AS macd_alert, c.trend AS macd_trend,
        c.trend_strength AS macd_trend_strength,
        a.ma_5, a.ma_10, a.bias_ma5, a.bias_ma10, a.ma5_slope, a.ma10_slope,
        CASE a.alignment
            WHEN 'bull_strong'    THEN '多头强势 — 两线同步上行，持仓舒适区'
            WHEN 'bull_building'  THEN '多头初建 — MA5已拐头向上，MA10惯性下行'
            WHEN 'bull_weakening' THEN '多头衰竭 — MA5先拐头向下，即将死叉前兆'
            WHEN 'bull_rolling'   THEN '多头翻转 — 两线均下行，死叉边缘'
            WHEN 'bear_strong'    THEN '空头强势 — 两线同步下行，持币观望区'
            WHEN 'bear_building'  THEN '空头初建 — 死叉后MA10惯性未消，下跌中继'
            WHEN 'bear_weakening' THEN '空头衰竭 — MA5尝试上拐，空方减弱'
            WHEN 'bear_rolling'   THEN '空头翻转 — 两线均上行，金叉边缘'
            WHEN 'sideways'      THEN '均线走平 — 双斜率近零，方向待定'
            WHEN 'tangle'         THEN '均线缠绕 — 方向不明，观望'
            ELSE NULL
        END              AS ma_alignment,
        a.turning_point AS ma_turning_point,
        NULL AS net_mf_amount, NULL AS ddx, NULL AS ddx2, NULL AS dde_trend, NULL AS dde_alert,
        NULL AS dde_divergence,
        v.ma_vol_5, v.pct_vol_rank, v.zone AS vol_zone, v.trend AS vol_trend,
        v.volume_ratio AS vol_ratio, v.trend_strength AS vol_trend_strength,
        v.divergence AS vol_divergence,
        pp.price_position_60d, pp.price_position_120d, pp.price_position_250d
    FROM v_dws_macd_weekly_latest c
    LEFT JOIN v_dws_kpattern_weekly_latest k ON c.ts_code = k.ts_code AND c.trade_date = k.trade_date
    LEFT JOIN v_dws_ma_weekly_latest      a ON c.ts_code = a.ts_code AND c.trade_date = a.trade_date
    LEFT JOIN v_dws_volume_weekly_latest  v ON c.ts_code = v.ts_code AND c.trade_date = v.trade_date
    LEFT JOIN v_dws_price_position_weekly_latest pp ON c.ts_code = pp.ts_code AND c.trade_date = pp.trade_date
    LEFT JOIN dwd_weekly_quote            q ON c.ts_code = q.ts_code AND c.trade_date = q.trade_date
    WHERE c.ts_code = '000001.SH'""",
]


_V_DATA_FRESHNESS_DDL = """
CREATE VIEW IF NOT EXISTS v_data_freshness AS
SELECT 'ods_daily'        AS table_name, MAX(trade_date) AS latest_date FROM ods_daily
UNION ALL
SELECT 'ods_daily_basic', MAX(trade_date) FROM ods_daily_basic
UNION ALL
SELECT 'ods_moneyflow',   MAX(trade_date) FROM ods_moneyflow
UNION ALL
SELECT 'dwd_daily_quote', MAX(trade_date) FROM dwd_daily_quote
"""

# ============================================================
# Public API
# ============================================================

def create_all_tables(con: duckdb.DuckDBPyConnection):
    """Create all tables, indexes, and views in the correct dependency order.

    Executes: ODS -> DIM -> DWD -> DWS -> Indexes -> Views
    """
    # ODS (7 tables)
    for ddl in _ODS_DDL:
        con.execute(ddl)
    _migrate_etl_log(con)

    # DIM (4 tables)
    for ddl in _DIM_DDL:
        con.execute(ddl)

    # DWD (3 tables)
    for ddl in _DWD_DDL:
        con.execute(ddl)

    # DWS (10 tables: 5 indicators x 2 frequencies)
    _create_dws(con)

    # DIM index
    for ddl in _DIM_INDEX_DDL:
        con.execute(ddl)

    # DWD indexes (3)
    for ddl in _DWD_INDEX_DDL:
        con.execute(ddl)

    # ODS indexes (3)
    for ddl in _ODS_INDEX_DDL:
        con.execute(ddl)

    # DWS indexes (20)
    for ddl in _DWS_INDEX_DDL:
        con.execute(ddl)

    # Migrations for existing databases
    _migrate_dde_trend_strength(con)
    _migrate_volume_new_columns(con)
    _migrate_dde_b4_inputs(con)
    _migrate_dws_fingerprint(con)

    # Latest views (10)
    for ddl in _LATEST_VIEW_DDL:
        con.execute(ddl)

    # Spec freshness DQ view (ops spec-status / health_check Section J)
    con.execute(_V_DQ_SPEC_FRESHNESS_DDL)

    # ADS wide views (4)
    for ddl in _ADS_WIDE_VIEWS_DDL:
        con.execute(ddl)

    # Indicator availability view
    con.execute(_V_INDICATOR_AVAILABILITY_DDL)

    # Data freshness view
    con.execute(_V_DATA_FRESHNESS_DDL)

    # Spec freshness DQ view (generated from INDICATOR_SPEC_VERSIONS)
    con.execute(_build_dq_spec_freshness_ddl())


def _build_dq_spec_freshness_ddl() -> str:
    """Build v_dq_spec_freshness from current Calculator.SPEC_VERSION registry."""
    from backend.etl.calc_indicators import INDICATOR_SPEC_VERSIONS, dws_latest_view

    daily_anchor = (
        "(SELECT MAX(trade_date) FROM dwd_daily_quote WHERE is_suspended=0)"
    )
    weekly_anchor = """
        (SELECT MAX(trade_date) FROM dim_date
         WHERE is_trade_day=1 AND is_week_end=1
           AND trade_date <= (SELECT MAX(trade_date) FROM ods_daily))
    """
    parts = []
    for (ind, freq), expected in sorted(INDICATOR_SPEC_VERSIONS.items()):
        view = dws_latest_view(ind, freq)
        anchor = daily_anchor if freq == "daily" else weekly_anchor
        parts.append(f"""
        SELECT
            '{ind}' AS indicator,
            '{freq}' AS freq,
            {anchor} AS anchor_trade_date,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE COALESCE(spec_version, 'v1') = '{expected}') AS spec_ok,
            COUNT(*) FILTER (WHERE COALESCE(spec_version, 'v1') <> '{expected}') AS spec_stale,
            '{expected}' AS expected_spec
        FROM {view}
        WHERE trade_date = {anchor}
        """.strip())
    body = "\nUNION ALL\n".join(parts)
    return f"CREATE OR REPLACE VIEW v_dq_spec_freshness AS\n{body}"


_DWS_CALC_STATE_DDL = """
    CREATE TABLE IF NOT EXISTS dws_calc_state (
        ts_code           VARCHAR NOT NULL,
        freq              VARCHAR NOT NULL,
        indicator         VARCHAR NOT NULL,
        last_trade_date   VARCHAR NOT NULL,
        history_fp        VARCHAR NOT NULL,
        quote_latest_adj  DOUBLE,
        spec_version      VARCHAR DEFAULT 'v1',
        updated_calc_date VARCHAR NOT NULL,
        PRIMARY KEY (ts_code, freq, indicator)
    )
"""


def ensure_calc_state_table(con: duckdb.DuckDBPyConnection):
    """Idempotently create dws_calc_state (append-only calc routing state).

    Called at calc startup so an existing DB created before this table was
    added gets it without a manual schema re-init. CREATE TABLE IF NOT EXISTS.
    """
    con.execute(_DWS_CALC_STATE_DDL)


def _create_dws(con: duckdb.DuckDBPyConnection):
    """Create all 12 DWS tables from templates + the calc-state table."""
    for freq in ("daily", "weekly"):
        for name, ddl in _DWS_DDL.items():
            table = f"dws_{name}_{freq}"
            con.execute(ddl.format(table=table))
    ensure_calc_state_table(con)


def drop_all_tables(con: duckdb.DuckDBPyConnection):
    """Drop all tables and views in reverse dependency order (for testing only)."""
    # Views first
    _all_views = (
        ["v_indicator_availability", "v_dq_spec_freshness",
         "v_ads_index_wide_weekly", "v_ads_index_wide",
         "v_ads_analysis_wide_weekly", "v_ads_analysis_wide_daily",
         "v_data_freshness"]
        + [f"v_dws_{ind}_{freq}_latest"
           for ind in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]
           for freq in ["daily", "weekly"]]
    )
    for view in _all_views:
        con.execute(f"DROP VIEW IF EXISTS {view}")

    # Tables in reverse dependency order
    _all_tables = (
        # DWS (10)
        [f"dws_{ind}_{freq}"
         for ind in ["kpattern", "macd", "ma", "dde", "volume", "price_position"]
         for freq in ["daily", "weekly"]]
        + ["dws_calc_state"]
        # DWD (3)
        + ["dwd_daily_moneyflow", "dwd_weekly_quote", "dwd_daily_quote"]
        # DIM (4) — FK tables first
        + ["dim_concept_stock", "dim_concept", "dim_date", "dim_stock"]
        # ODS (7)
        + ["ods_etl_log", "ods_calc_skip_log", "ods_concept_detail", "ods_trade_cal",
           "ods_moneyflow", "ods_daily_basic", "ods_daily", "ods_stock_basic"]
    )
    for table in _all_tables:
        con.execute(f"DROP TABLE IF EXISTS {table}")
