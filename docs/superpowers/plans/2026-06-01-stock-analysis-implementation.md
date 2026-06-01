# 个股技术分析数据模型 — 实现计划

> **For agentic workers:** Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]` checkbox syntax.

**Goal:** 基于 tushare + DuckDB 构建覆盖全 A 股 10 年历史的个股技术分析数据管道（ETL→DWS指标→FastAPI查询→Excel导出）。

**Architecture:** DuckDB 持久化 + Python ETL + FastAPI。有状态计算(EMA)用 Python/NumPy，无状态(形态/趋势)用 DuckDB SQL 窗口函数。INSERT-only 快照模式，60 日窗口增量。

**Tech Stack:** Python ≥3.10, DuckDB ≥1.0, tushare Pro, FastAPI, pandas, openpyxl, pytest, httpx

**Spec:** `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` (v1.8)

---

## 文件结构

```
backend/
├── config.py                 # 环境变量 + 全局配置
├── db/
│   ├── connection.py         # DuckDB 连接、自检、WAL checkpoint
│   └── schema.py             # 全部 DDL (ODS/DIM/DWD/DWS/VIEW/INDEX)
├── fetch/
│   ├── client.py             # tushare API 封装 (限流/重试/退避)
│   ├── ods_stock_basic.py    # stock_basic 拉取
│   ├── ods_daily.py          # daily + daily_basic 批量拉取
│   ├── ods_moneyflow.py      # moneyflow 拉取
│   ├── ods_concept.py        # concept_detail 拉取
│   └── ods_trade_cal.py      # trade_cal 拉取
├── etl/
│   ├── base.py               # EMA/SMA/线性回归 工具函数
│   ├── build_dim.py          # DIM 层构建
│   ├── build_dwd.py          # DWD 层构建 (前复权/停牌/周线聚合)
│   ├── calc_macd.py          # MACD Calculator
│   ├── calc_ma.py            # 均线 Calculator
│   ├── calc_kpattern.py      # K线形态 Calculator
│   ├── calc_dde.py           # DDE Calculator
│   ├── calc_volume.py        # 量能 Calculator
│   ├── orchestrator.py       # ETL 编排 (自检/恢复/分批/事务)
│   └── error_handler.py      # 5级错误 + ods_etl_log 写入
├── api/
│   ├── app.py                # FastAPI app
│   ├── router.py             # /api/v1/ 路由
│   └── models.py             # Pydantic models
├── cli.py                    # CLI 入口 (check/etl/query/export/status)
└── export_wide.py            # Excel 导出
tests/
├── conftest.py               # fixtures: temp DuckDB
├── test_schema.py            # DDL + CHECK 约束
├── test_fetch/
│   └── test_client.py        # 限流/重试
├── test_etl/
│   ├── test_base.py          # EMA/SMA
│   ├── test_build_dim.py
│   ├── test_build_dwd.py
│   ├── test_calc_macd.py
│   ├── test_calc_ma.py
│   ├── test_calc_kpattern.py
│   ├── test_calc_dde.py
│   ├── test_calc_volume.py
│   └── test_orchestrator.py
├── test_api/
│   └── test_router.py
├── test_cli.py
├── test_export_wide.py
└── fixtures/
    └── golden_data.py        # 回归测试黄金数据集
```

---

### Task 1: 项目骨架

**Files:** `requirements.txt`, `.env.example`, `.gitignore`, `backend/__init__.py`, `backend/config.py`, `tests/__init__.py`, `tests/conftest.py`

- [ ] **Step 1: 创建 requirements.txt**

```bash
cat > requirements.txt << 'EOF'
duckdb>=1.0.0
tushare>=1.4.0
pandas>=2.0.0
openpyxl>=3.1.0
fastapi>=0.110.0
uvicorn>=0.30.0
python-dotenv>=1.0.0
pytest>=8.0.0
httpx>=0.27.0
EOF
```

- [ ] **Step 2: 创建 .env.example 和 .gitignore**

```bash
cat > .env.example << 'EOF'
TUSHARE_TOKEN=your_pro_token_here
DUCKDB_PATH=./data/tradeanalysis.duckdb
LOG_LEVEL=INFO
ETL_WORKERS=1
EOF

cat > .gitignore << 'EOF'
.env
data/
__pycache__/
*.pyc
*.duckdb
*.xlsx
EOF
```

- [ ] **Step 3: 创建 backend/config.py**

```python
import os
from dotenv import load_dotenv

load_dotenv()

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN")
if not TUSHARE_TOKEN:
    raise RuntimeError("TUSHARE_TOKEN 未设置，请检查 .env 文件")

DUCKDB_PATH = os.getenv("DUCKDB_PATH", "./data/tradeanalysis.duckdb")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
ETL_WORKERS = int(os.getenv("ETL_WORKERS", "1"))

os.makedirs(os.path.dirname(DUCKDB_PATH) or ".", exist_ok=True)
```

- [ ] **Step 4: 创建 tests/conftest.py**

```python
import pytest, duckdb, os, tempfile

@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    con = duckdb.connect(path)
    yield con
    con.close()
    os.unlink(path)

@pytest.fixture
def db_with_schema(temp_db):
    from backend.db.schema import create_all_tables
    create_all_tables(temp_db)
    return temp_db
```

- [ ] **Step 5: 创建空的 __init__.py 文件**

```bash
touch backend/__init__.py tests/__init__.py
mkdir -p backend/db backend/fetch backend/etl backend/api tests/test_fetch tests/test_etl tests/test_api tests/fixtures
touch backend/db/__init__.py backend/fetch/__init__.py backend/etl/__init__.py backend/api/__init__.py
touch tests/test_fetch/__init__.py tests/test_etl/__init__.py tests/test_api/__init__.py tests/fixtures/__init__.py
```

- [ ] **Step 6: 安装 + 验证**

```bash
pip install -r requirements.txt
python -c "from backend.config import DUCKDB_PATH; print(f'DuckDB path: {DUCKDB_PATH}')"
```

Expected: `DuckDB path: ./data/tradeanalysis.duckdb`

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: project scaffolding — config, deps, test fixtures"
```

---

### Task 2: DuckDB 连接 + 完整 Schema DDL

**Files:** `backend/db/connection.py`, `backend/db/schema.py`, `tests/test_schema.py`

- [ ] **Step 1: 创建 backend/db/connection.py**

```python
import duckdb, os, shutil, logging
from backend.config import DUCKDB_PATH

logger = logging.getLogger(__name__)

def get_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(DUCKDB_PATH, read_only=read_only)
    if not read_only:
        con.execute("PRAGMA enable_checkpoint_on_shutdown;")
    return con

def check_connectivity() -> dict:
    result = {"duckdb": "ok", "disk_free_mb": 0, "db_size_mb": 0, "version": ""}
    try:
        con = get_connection()
        result["version"] = con.execute("SELECT version()").fetchone()[0]
        con.execute("SELECT 1")
        con.close()
    except Exception as e:
        result["duckdb"] = f"error: {e}"
        return result
    data_dir = os.path.dirname(DUCKDB_PATH) or "."
    stat = shutil.disk_usage(data_dir)
    result["disk_free_mb"] = stat.free // (1024 * 1024)
    if os.path.exists(DUCKDB_PATH):
        result["db_size_mb"] = os.path.getsize(DUCKDB_PATH) // (1024 * 1024)
    if result["disk_free_mb"] < 100:
        result["duckdb"] = "fatal: low disk space"
    return result

def run_checkpoint(con: duckdb.DuckDBPyConnection):
    con.execute("CHECKPOINT;")
    logger.info("WAL checkpoint done")
```

- [ ] **Step 2: 创建 backend/db/schema.py — 完整 DDL**

```python
import duckdb, logging
logger = logging.getLogger(__name__)

def create_all_tables(con: duckdb.DuckDBPyConnection):
    _create_ods(con)
    _create_dim(con)
    _create_dwd(con)
    _create_dws(con)
    _create_views(con)
    _create_indexes(con)
    con.execute("CHECKPOINT;")
    logger.info("Schema created")

def drop_all_tables(con: duckdb.DuckDBPyConnection):
    tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    for (t,) in tables:
        con.execute(f"DROP TABLE IF EXISTS {t} CASCADE")

# ── ODS (7 tables) ──
def _create_ods(con):
    con.execute("""CREATE TABLE IF NOT EXISTS ods_stock_basic (
        ts_code TEXT PRIMARY KEY, symbol TEXT, name TEXT, area TEXT, industry TEXT,
        exchange TEXT, list_date TEXT, delist_date TEXT, raw_json TEXT,
        fetched_at TEXT DEFAULT (datetime('now')))""")
    con.execute("""CREATE TABLE IF NOT EXISTS ods_daily (
        ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
        vol REAL, amount REAL, pct_chg REAL, adj_factor REAL,
        fetched_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (ts_code, trade_date))""")
    con.execute("""CREATE TABLE IF NOT EXISTS ods_daily_basic (
        ts_code TEXT, trade_date TEXT, total_mv REAL, pe_ttm REAL,
        turnover_rate REAL, volume_ratio REAL,
        fetched_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (ts_code, trade_date))""")
    con.execute("""CREATE TABLE IF NOT EXISTS ods_moneyflow (
        ts_code TEXT, trade_date TEXT,
        buy_sm_vol REAL, buy_sm_amount REAL, sell_sm_vol REAL, sell_sm_amount REAL,
        buy_md_vol REAL, buy_md_amount REAL, sell_md_vol REAL, sell_md_amount REAL,
        buy_lg_vol REAL, buy_lg_amount REAL, sell_lg_vol REAL, sell_lg_amount REAL,
        buy_elg_vol REAL, buy_elg_amount REAL, sell_elg_vol REAL, sell_elg_amount REAL,
        net_mf_vol REAL, net_mf_amount REAL,
        fetched_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (ts_code, trade_date))""")
    con.execute("""CREATE TABLE IF NOT EXISTS ods_trade_cal (
        cal_date TEXT PRIMARY KEY, is_open INTEGER, pretrade_date TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS ods_concept_detail (
        concept_name TEXT, ts_code TEXT,
        fetched_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (concept_name, ts_code))""")
    con.execute("""CREATE TABLE IF NOT EXISTS ods_etl_log (
        id INTEGER PRIMARY KEY, step_name TEXT, started_at TEXT, finished_at TEXT,
        status TEXT, row_count INTEGER, error_msg TEXT, data_completeness TEXT)""")

# ── DIM (4 tables) ──
def _create_dim(con):
    con.execute("""CREATE TABLE IF NOT EXISTS dim_stock (
        ts_code TEXT PRIMARY KEY, stock_code TEXT, symbol TEXT, name TEXT,
        exchange TEXT, sector TEXT, industry TEXT, list_date TEXT, delist_date TEXT,
        is_active INTEGER DEFAULT 1, is_st INTEGER DEFAULT 0)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dim_stock_code ON dim_stock(stock_code)")
    con.execute("""CREATE TABLE IF NOT EXISTS dim_date (
        trade_date TEXT PRIMARY KEY, is_trade_day INTEGER, is_week_end INTEGER,
        is_month_end INTEGER, is_year_end INTEGER, year INTEGER, quarter INTEGER,
        month INTEGER, week_of_year INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS dim_concept (
        concept_id INTEGER PRIMARY KEY, concept_name TEXT UNIQUE)""")
    con.execute("""CREATE TABLE IF NOT EXISTS dim_concept_stock (
        concept_id INTEGER REFERENCES dim_concept(concept_id),
        ts_code TEXT REFERENCES dim_stock(ts_code), PRIMARY KEY (concept_id, ts_code))""")

# ── DWD (3 tables) ──
def _create_dwd(con):
    con.execute("""CREATE TABLE IF NOT EXISTS dwd_daily_quote (
        ts_code TEXT, trade_date TEXT, open_qfq REAL, high_qfq REAL, low_qfq REAL,
        close_qfq REAL, vol REAL, amount REAL, pct_chg REAL, total_mv REAL,
        pe_ttm REAL, turnover_rate REAL, volume_ratio REAL, is_suspended INTEGER DEFAULT 0,
        PRIMARY KEY (ts_code, trade_date))""")
    con.execute("""CREATE TABLE IF NOT EXISTS dwd_weekly_quote (
        ts_code TEXT, trade_date TEXT, open_qfq REAL, high_qfq REAL, low_qfq REAL,
        close_qfq REAL, vol REAL, amount REAL, pct_chg REAL, total_mv REAL,
        pe_ttm REAL, turnover_rate REAL, volume_ratio REAL, active_days INTEGER,
        PRIMARY KEY (ts_code, trade_date))""")
    con.execute("""CREATE TABLE IF NOT EXISTS dwd_daily_moneyflow (
        ts_code TEXT, trade_date TEXT, net_mf_vol REAL, net_mf_amount REAL,
        buy_lg_vol REAL, sell_lg_vol REAL, buy_elg_vol REAL, sell_elg_vol REAL,
        total_vol REAL, PRIMARY KEY (ts_code, trade_date))""")

# ── DWS (10 tables: 5 indicators × daily/weekly) ──
_DWS_DDL = {
    "kpattern": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code TEXT, trade_date TEXT, yang_bao_yin INTEGER, yang_ke_yin INTEGER,
        mu_bei_xian INTEGER, bi_lei_zhen INTEGER, gao_kai_chang_yin INTEGER,
        yin_bao_yang INTEGER, yin_ke_yang INTEGER, strength REAL, calc_date TEXT,
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (yang_bao_yin IN (0,1)), CHECK (yang_ke_yin IN (0,1)),
        CHECK (mu_bei_xian IN (0,1)), CHECK (bi_lei_zhen IN (0,1)),
        CHECK (gao_kai_chang_yin IN (0,1)), CHECK (yin_bao_yang IN (0,1)),
        CHECK (yin_ke_yang IN (0,1)),
        CHECK (strength IS NULL OR (strength >= 0.0 AND strength <= 1.0)))""",
    "macd": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code TEXT, trade_date TEXT, ema_12 REAL, ema_26 REAL, dif REAL, dea REAL,
        macd_bar REAL, divergence TEXT, zone TEXT, turning_point TEXT, alert TEXT,
        trend TEXT, calc_date TEXT, PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (divergence IN ('top_divergence','bottom_divergence',NULL)),
        CHECK (zone IN ('bull','bear')),
        CHECK (turning_point IN ('golden_cross','dead_cross','near_golden','near_dead',NULL)),
        CHECK (alert IN ('upturn_reverse','downturn_reverse','upturn_flat','downturn_flat',NULL)),
        CHECK (trend IN ('up','down','flat')))""",
    "ma": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code TEXT, trade_date TEXT, ma_5 REAL, ma_10 REAL, bias_ma5 REAL,
        bias_ma10 REAL, ma5_slope REAL, ma10_slope REAL, alignment TEXT,
        turning_point TEXT, calc_date TEXT, PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (alignment IN ('bull_strong','bull_building','bull_weakening','bull_rolling',
               'bear_strong','bear_building','bear_weakening','bear_rolling','tangle',NULL)),
        CHECK (turning_point IN ('golden_cross','dead_cross','near_golden','near_dead',NULL)))""",
    "dde": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code TEXT, trade_date TEXT, net_mf_amount REAL, ddx REAL, ddx2 REAL,
        trend TEXT, alert TEXT, divergence TEXT, calc_date TEXT,
        PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (trend IN ('up','down','flat')),
        CHECK (alert IN ('upturn_reverse','downturn_reverse','upturn_flat','downturn_flat',NULL)),
        CHECK (divergence IN ('top_divergence','bottom_divergence',NULL)))""",
    "volume": """CREATE TABLE IF NOT EXISTS {table} (
        ts_code TEXT, trade_date TEXT, ma_vol_5 REAL, pct_vol_rank REAL,
        zone TEXT, trend TEXT, calc_date TEXT, PRIMARY KEY (ts_code, trade_date, calc_date),
        CHECK (pct_vol_rank >= 0 AND pct_vol_rank <= 100),
        CHECK (zone IN ('explosive','low_volume','normal')),
        CHECK (trend IN ('expanding','shrinking','flat')))"""
}

def _create_dws(con):
    for freq in ("daily", "weekly"):
        for name, ddl in _DWS_DDL.items():
            con.execute(ddl.format(table=f"dws_{name}_{freq}"))

# ── Views: v_*_latest (10) + ADS 宽表 (4) ──
def _create_views(con):
    for freq in ("daily", "weekly"):
        for name in ("kpattern", "macd", "ma", "dde", "volume"):
            t = f"dws_{name}_{freq}"
            v = f"v_dws_{name}_{freq}_latest"
            con.execute(f"""CREATE VIEW IF NOT EXISTS {v} AS SELECT * FROM {t} d
                WHERE calc_date = (SELECT MAX(calc_date) FROM {t}
                WHERE ts_code = d.ts_code AND trade_date = d.trade_date)""")
    _create_ads_views(con)

def _create_ads_views(con):
    # v_ads_analysis_wide_daily — LEFT JOIN 全 6 表 (spec 7.1)
    con.execute("""CREATE VIEW IF NOT EXISTS v_ads_analysis_wide_daily AS
    SELECT 'D' AS freq, c.trade_date, c.ts_code, s.stock_code, s.name AS stock_name,
        s.exchange, s.sector, s.industry, s.is_st,
        q.close_qfq AS close, q.pct_chg, q.vol, q.amount, q.total_mv, q.pe_ttm, q.turnover_rate,
        CASE WHEN k.yang_ke_yin=1 THEN 'yang_ke_yin' WHEN k.yang_bao_yin=1 THEN 'yang_bao_yin'
             WHEN k.yin_ke_yang=1 THEN 'yin_ke_yang' WHEN k.yin_bao_yang=1 THEN 'yin_bao_yang'
             WHEN k.mu_bei_xian=1 THEN 'mu_bei_xian' WHEN k.bi_lei_zhen=1 THEN 'bi_lei_zhen'
             WHEN k.gao_kai_chang_yin=1 THEN 'gao_kai_chang_yin' END AS kpattern,
        k.strength AS kpattern_strength,
        c.ema_12, c.ema_26, c.dif, c.dea, c.macd_bar,
        c.divergence AS macd_divergence, c.zone AS macd_zone,
        c.turning_point AS macd_turning_point, c.alert AS macd_alert, c.trend AS macd_trend,
        a.ma_5, a.ma_10, a.bias_ma5, a.bias_ma10, a.ma5_slope, a.ma10_slope,
        CASE a.alignment WHEN 'bull_strong' THEN '多头强势' WHEN 'bull_building' THEN '多头初建'
             WHEN 'bull_weakening' THEN '多头衰竭' WHEN 'bull_rolling' THEN '多头翻转'
             WHEN 'bear_strong' THEN '空头强势' WHEN 'bear_building' THEN '空头初建'
             WHEN 'bear_weakening' THEN '空头衰竭' WHEN 'bear_rolling' THEN '空头翻转'
             WHEN 'tangle' THEN '均线缠绕' END AS ma_alignment,
        a.turning_point AS ma_turning_point,
        d.net_mf_amount, d.ddx, d.ddx2, d.trend AS dde_trend, d.alert AS dde_alert,
        d.divergence AS dde_divergence,
        v.ma_vol_5, v.pct_vol_rank, v.zone AS vol_zone, v.trend AS vol_trend
    FROM v_dws_macd_daily_latest c
    LEFT JOIN dim_stock s ON c.ts_code = s.ts_code
    LEFT JOIN v_dws_kpattern_daily_latest k ON c.ts_code=k.ts_code AND c.trade_date=k.trade_date
    LEFT JOIN v_dws_ma_daily_latest a ON c.ts_code=a.ts_code AND c.trade_date=a.trade_date
    LEFT JOIN v_dws_dde_daily_latest d ON c.ts_code=d.ts_code AND c.trade_date=d.trade_date
    LEFT JOIN v_dws_volume_daily_latest v ON c.ts_code=v.ts_code AND c.trade_date=v.trade_date
    LEFT JOIN dwd_daily_quote q ON c.ts_code=q.ts_code AND c.trade_date=q.trade_date""")

    # v_ads_analysis_wide_weekly (同结构，替换 _daily → _weekly)
    con.execute("""CREATE VIEW IF NOT EXISTS v_ads_analysis_wide_weekly AS
    SELECT 'W' AS freq, cw.trade_date, cw.ts_code, s.stock_code, s.name AS stock_name,
        s.exchange, s.sector, s.industry, s.is_st,
        qw.close_qfq AS close, qw.pct_chg, qw.vol, qw.amount, qw.total_mv, qw.pe_ttm, qw.turnover_rate,
        CASE WHEN kw.yang_ke_yin=1 THEN 'yang_ke_yin' WHEN kw.yang_bao_yin=1 THEN 'yang_bao_yin'
             WHEN kw.yin_ke_yang=1 THEN 'yin_ke_yang' WHEN kw.yin_bao_yang=1 THEN 'yin_bao_yang'
             WHEN kw.mu_bei_xian=1 THEN 'mu_bei_xian' WHEN kw.bi_lei_zhen=1 THEN 'bi_lei_zhen'
             WHEN kw.gao_kai_chang_yin=1 THEN 'gao_kai_chang_yin' END AS kpattern,
        kw.strength AS kpattern_strength,
        cw.ema_12, cw.ema_26, cw.dif, cw.dea, cw.macd_bar,
        cw.divergence AS macd_divergence, cw.zone AS macd_zone,
        cw.turning_point AS macd_turning_point, cw.alert AS macd_alert, cw.trend AS macd_trend,
        aw.ma_5, aw.ma_10, aw.bias_ma5, aw.bias_ma10, aw.ma5_slope, aw.ma10_slope,
        CASE aw.alignment WHEN 'bull_strong' THEN '多头强势' WHEN 'bull_building' THEN '多头初建'
             WHEN 'bull_weakening' THEN '多头衰竭' WHEN 'bull_rolling' THEN '多头翻转'
             WHEN 'bear_strong' THEN '空头强势' WHEN 'bear_building' THEN '空头初建'
             WHEN 'bear_weakening' THEN '空头衰竭' WHEN 'bear_rolling' THEN '空头翻转'
             WHEN 'tangle' THEN '均线缠绕' END AS ma_alignment,
        aw.turning_point AS ma_turning_point,
        dw.net_mf_amount, dw.ddx, dw.ddx2, dw.trend AS dde_trend, dw.alert AS dde_alert,
        dw.divergence AS dde_divergence,
        vw.ma_vol_5, vw.pct_vol_rank, vw.zone AS vol_zone, vw.trend AS vol_trend
    FROM v_dws_macd_weekly_latest cw
    LEFT JOIN dim_stock s ON cw.ts_code = s.ts_code
    LEFT JOIN v_dws_kpattern_weekly_latest kw ON cw.ts_code=kw.ts_code AND cw.trade_date=kw.trade_date
    LEFT JOIN v_dws_ma_weekly_latest aw ON cw.ts_code=aw.ts_code AND cw.trade_date=aw.trade_date
    LEFT JOIN v_dws_dde_weekly_latest dw ON cw.ts_code=dw.ts_code AND cw.trade_date=dw.trade_date
    LEFT JOIN v_dws_volume_weekly_latest vw ON cw.ts_code=vw.ts_code AND cw.trade_date=vw.trade_date
    LEFT JOIN dwd_weekly_quote qw ON cw.ts_code=qw.ts_code AND cw.trade_date=qw.trade_date""")

    # v_ads_index_wide + v_ads_index_wide_weekly (上证指数，spec 7.1)
    con.execute("""CREATE VIEW IF NOT EXISTS v_ads_index_wide AS
    SELECT 'D' AS freq, c.trade_date, c.ts_code, '000001' AS stock_code, '上证指数' AS index_name,
        q.close_qfq AS close, q.pct_chg, q.vol, q.amount,
        CASE WHEN k.yang_ke_yin=1 THEN 'yang_ke_yin' WHEN k.yang_bao_yin=1 THEN 'yang_bao_yin'
             WHEN k.yin_ke_yang=1 THEN 'yin_ke_yang' WHEN k.yin_bao_yang=1 THEN 'yin_bao_yang'
             WHEN k.mu_bei_xian=1 THEN 'mu_bei_xian' WHEN k.bi_lei_zhen=1 THEN 'bi_lei_zhen'
             WHEN k.gao_kai_chang_yin=1 THEN 'gao_kai_chang_yin' END AS kpattern,
        k.strength AS kpattern_strength,
        c.ema_12, c.ema_26, c.dif, c.dea, c.macd_bar,
        c.divergence AS macd_divergence, c.zone AS macd_zone,
        c.turning_point AS macd_turning_point, c.alert AS macd_alert, c.trend AS macd_trend,
        a.ma_5, a.ma_10, a.bias_ma5, a.bias_ma10, a.ma5_slope, a.ma10_slope,
        CASE a.alignment WHEN 'bull_strong' THEN '多头强势' WHEN 'bull_building' THEN '多头初建'
             WHEN 'bull_weakening' THEN '多头衰竭' WHEN 'bull_rolling' THEN '多头翻转'
             WHEN 'bear_strong' THEN '空头强势' WHEN 'bear_building' THEN '空头初建'
             WHEN 'bear_weakening' THEN '空头衰竭' WHEN 'bear_rolling' THEN '空头翻转'
             WHEN 'tangle' THEN '均线缠绕' END AS ma_alignment,
        a.turning_point AS ma_turning_point,
        NULL AS net_mf_amount, NULL AS ddx, NULL AS ddx2,
        NULL AS dde_trend, NULL AS dde_alert, NULL AS dde_divergence,
        v.ma_vol_5, v.pct_vol_rank, v.zone AS vol_zone, v.trend AS vol_trend
    FROM v_dws_macd_daily_latest c
    LEFT JOIN v_dws_kpattern_daily_latest k ON c.ts_code=k.ts_code AND c.trade_date=k.trade_date
    LEFT JOIN v_dws_ma_daily_latest a ON c.ts_code=a.ts_code AND c.trade_date=a.trade_date
    LEFT JOIN v_dws_volume_daily_latest v ON c.ts_code=v.ts_code AND c.trade_date=v.trade_date
    LEFT JOIN dwd_daily_quote q ON c.ts_code=q.ts_code AND c.trade_date=q.trade_date
    WHERE c.ts_code = '000001.SH'""")

    con.execute("""CREATE VIEW IF NOT EXISTS v_ads_index_wide_weekly AS
    SELECT 'W' AS freq, c.trade_date, c.ts_code, '000001' AS stock_code, '上证指数' AS index_name,
        q.close_qfq AS close, q.pct_chg, q.vol, q.amount,
        CASE WHEN k.yang_ke_yin=1 THEN 'yang_ke_yin' WHEN k.yang_bao_yin=1 THEN 'yang_bao_yin'
             WHEN k.yin_ke_yang=1 THEN 'yin_ke_yang' WHEN k.yin_bao_yang=1 THEN 'yin_bao_yang'
             WHEN k.mu_bei_xian=1 THEN 'mu_bei_xian' WHEN k.bi_lei_zhen=1 THEN 'bi_lei_zhen'
             WHEN k.gao_kai_chang_yin=1 THEN 'gao_kai_chang_yin' END AS kpattern,
        k.strength AS kpattern_strength,
        c.ema_12, c.ema_26, c.dif, c.dea, c.macd_bar,
        c.divergence AS macd_divergence, c.zone AS macd_zone,
        c.turning_point AS macd_turning_point, c.alert AS macd_alert, c.trend AS macd_trend,
        a.ma_5, a.ma_10, a.bias_ma5, a.bias_ma10, a.ma5_slope, a.ma10_slope,
        CASE a.alignment WHEN 'bull_strong' THEN '多头强势' WHEN 'bull_building' THEN '多头初建'
             WHEN 'bull_weakening' THEN '多头衰竭' WHEN 'bull_rolling' THEN '多头翻转'
             WHEN 'bear_strong' THEN '空头强势' WHEN 'bear_building' THEN '空头初建'
             WHEN 'bear_weakening' THEN '空头衰竭' WHEN 'bear_rolling' THEN '空头翻转'
             WHEN 'tangle' THEN '均线缠绕' END AS ma_alignment,
        a.turning_point AS ma_turning_point,
        NULL AS net_mf_amount, NULL AS ddx, NULL AS ddx2,
        NULL AS dde_trend, NULL AS dde_alert, NULL AS dde_divergence,
        v.ma_vol_5, v.pct_vol_rank, v.zone AS vol_zone, v.trend AS vol_trend
    FROM v_dws_macd_weekly_latest c
    LEFT JOIN v_dws_kpattern_weekly_latest k ON c.ts_code=k.ts_code AND c.trade_date=k.trade_date
    LEFT JOIN v_dws_ma_weekly_latest a ON c.ts_code=a.ts_code AND c.trade_date=a.trade_date
    LEFT JOIN v_dws_volume_weekly_latest v ON c.ts_code=v.ts_code AND c.trade_date=v.trade_date
    LEFT JOIN dwd_weekly_quote q ON c.ts_code=q.ts_code AND c.trade_date=q.trade_date
    WHERE c.ts_code = '000001.SH'""")

# ── Indexes (26 total) ──
def _create_indexes(con):
    for freq in ("daily", "weekly"):
        for name in ("kpattern", "macd", "ma", "dde", "volume"):
            t = f"dws_{name}_{freq}"
            con.execute(f"CREATE INDEX IF NOT EXISTS idx_{name}_{freq}_cd ON {t}(ts_code, trade_date DESC)")
            con.execute(f"CREATE INDEX IF NOT EXISTS idx_{name}_{freq}_dc ON {t}(trade_date, ts_code)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dwd_daily_cd ON dwd_daily_quote(ts_code, trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dwd_mf_cd ON dwd_daily_moneyflow(ts_code, trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dwd_weekly_cd ON dwd_weekly_quote(ts_code, trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ods_daily_date ON ods_daily(trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ods_daily_basic_date ON ods_daily_basic(trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ods_moneyflow_date ON ods_moneyflow(trade_date)")
```

- [ ] **Step 3: 编写 schema 测试**

```python
# tests/test_schema.py
import pytest

def test_all_tables_created(db_with_schema):
    tables = {r[0] for r in db_with_schema.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "ods_daily" in tables
    assert "dim_stock" in tables
    assert "dwd_daily_quote" in tables
    assert "dws_macd_daily" in tables
    assert "dws_macd_weekly" in tables
    assert "ods_etl_log" in tables

def test_dws_check_constraint_rejects_invalid(db_with_schema):
    with pytest.raises(Exception):
        db_with_schema.execute("""
        INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, trend)
        VALUES ('000001.SZ','20260101','20260101','invalid')""")

def test_dws_check_constraint_accepts_valid(db_with_schema):
    db_with_schema.execute("""
    INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, trend)
    VALUES ('000001.SZ','20260101','20260101','up')""")
    row = db_with_schema.execute(
        "SELECT trend FROM dws_macd_daily WHERE ts_code='000001.SZ'").fetchone()
    assert row[0] == 'up'

def test_latest_views_exist(db_with_schema):
    views = {r[0] for r in db_with_schema.execute(
        "SELECT name FROM sqlite_master WHERE type='view'").fetchall()}
    assert "v_dws_macd_daily_latest" in views
    assert "v_dws_macd_weekly_latest" in views
    assert "v_ads_analysis_wide_daily" in views

def test_indexes_exist(db_with_schema):
    indexes = {r[0] for r in db_with_schema.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_macd_daily_cd" in indexes
    assert "idx_dwd_daily_cd" in indexes
    assert "idx_ods_daily_date" in indexes
```

- [ ] **Step 4: 运行测试 → Commit**

```bash
pytest tests/test_schema.py -v
```

Expected: 5 tests PASS

---

### Task 3: tushare API 客户端

**Files:** `backend/fetch/client.py`, `tests/test_fetch/test_client.py`

- [ ] **Step 1: 编写失败测试**

```python
# tests/test_fetch/test_client.py
from unittest.mock import patch, MagicMock
import pytest

@patch.dict('os.environ', {'TUSHARE_TOKEN': 'test'})
@patch('backend.fetch.client.ts.pro_api')
def test_retry_on_failure_then_succeed(mock_pro):
    mock = MagicMock()
    mock.daily.side_effect = [Exception("timeout"), Exception("timeout"),
                               MagicMock(empty=False, to_dict=lambda _: [{"ts_code":"TEST"}])]
    mock_pro.return_value = mock
    from backend.fetch.client import TushareClient
    client = TushareClient()
    results = client.call("daily", ts_code="TEST")
    assert len(results) == 1
    assert mock.daily.call_count == 3

@patch.dict('os.environ', {'TUSHARE_TOKEN': 'test'})
@patch('backend.fetch.client.ts.pro_api')
def test_empty_response_returns_empty_list(mock_pro):
    mock = MagicMock()
    mock.daily.return_value = None
    mock_pro.return_value = mock
    from backend.fetch.client import TushareClient
    client = TushareClient()
    assert client.call("daily", ts_code="NONE") == []
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_fetch/test_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'backend.fetch.client'`

- [ ] **Step 3: 实现 client.py**

```python
# backend/fetch/client.py
import tushare as ts
import time, logging
from backend.config import TUSHARE_TOKEN

logger = logging.getLogger(__name__)

class TushareClient:
    MAX_RETRIES = 3
    BASE_DELAY = 2  # seconds

    def __init__(self):
        ts.set_token(TUSHARE_TOKEN)
        self.pro = ts.pro_api()
        self._calls = 0
        self._window_start = time.time()

    def _rate_limit(self):
        self._calls += 1
        elapsed = time.time() - self._window_start
        if elapsed < 60 and self._calls >= 200:
            wait = 60 - elapsed + 1
            logger.info(f"Rate limit: sleeping {wait:.0f}s")
            time.sleep(wait)
            self._calls = 0
            self._window_start = time.time()
        elif elapsed >= 60:
            self._calls = 0
            self._window_start = time.time()

    def call(self, func_name: str, **kwargs) -> list[dict]:
        func = getattr(self.pro, func_name, None)
        if func is None:
            raise ValueError(f"Unknown tushare API: {func_name}")
        for attempt in range(self.MAX_RETRIES + 1):
            self._rate_limit()
            try:
                result = func(**kwargs)
                if result is None or result.empty:
                    return []
                return result.to_dict("records")
            except Exception as e:
                if attempt < self.MAX_RETRIES:
                    delay = self.BASE_DELAY * (2 ** attempt)
                    logger.warning(f"{func_name} retry {attempt+1}: {e}. Waiting {delay}s")
                    time.sleep(delay)
                else:
                    logger.error(f"{func_name} failed after {self.MAX_RETRIES} retries: {e}")
                    raise
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_fetch/test_client.py -v
```

Expected: 2 tests PASS

- [ ] **Step 5: Commit**

---

### Task 4: ODS Fetch — stock_basic, trade_cal, concept

**Files:** `backend/fetch/ods_stock_basic.py`, `backend/fetch/ods_trade_cal.py`, `backend/fetch/ods_concept.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_fetch/test_ods_static.py
import json

def test_fetch_stock_basic(db_with_schema, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test")
    from unittest.mock import patch, MagicMock
    with patch('backend.fetch.client.ts.pro_api') as mock_pro:
        mock = MagicMock()
        mock.stock_basic.return_value = MagicMock(empty=False, to_dict=lambda _: [
            {"ts_code":"000001.SZ","symbol":"000001","name":"平安银行","area":"深圳",
             "industry":"银行","exchange":"SZSE","list_date":"19910403","delist_date":""}])
        mock_pro.return_value = mock
        from backend.fetch.ods_stock_basic import fetch_stock_basic
        from backend.fetch.client import TushareClient
        n = fetch_stock_basic(TushareClient(), db_with_schema)
        assert n == 1
        row = db_with_schema.execute("SELECT name, exchange, raw_json FROM ods_stock_basic WHERE ts_code='000001.SZ'").fetchone()
        assert row[0] == "平安银行"
        assert row[1] == "SZSE"
        assert json.loads(row[2])["name"] == "平安银行"
```

- [ ] **Step 2: 实现 ods_stock_basic.py**

```python
# backend/fetch/ods_stock_basic.py
import json

def fetch_stock_basic(client, con) -> int:
    records = client.call("stock_basic", exchange="", list_status="L",
        fields="ts_code,symbol,name,area,industry,exchange,list_date,delist_date")
    for r in records:
        con.execute("""INSERT OR REPLACE INTO ods_stock_basic
            (ts_code, symbol, name, area, industry, exchange, list_date, delist_date, raw_json, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (r["ts_code"], r["symbol"], r["name"], r.get("area",""), r.get("industry",""),
             r["exchange"], r.get("list_date",""), r.get("delist_date",""),
             json.dumps(r, ensure_ascii=False)))
    return len(records)
```

- [ ] **Step 3: 实现 ods_trade_cal.py 和 ods_concept.py**（同模式：`client.call(...)` → `INSERT OR REPLACE` 循环）

- [ ] **Step 4: 测试 → Commit**

---

### Task 5: ODS Fetch — daily + daily_basic 批量拉取

**Files:** `backend/fetch/ods_daily.py`

- [ ] **Step 1: 实现批量拉取**

```python
# backend/fetch/ods_daily.py
import logging
logger = logging.getLogger(__name__)

def fetch_daily_batch(client, con, ts_codes: list[str], start: str, end: str) -> tuple[int, list[str]]:
    """返回 (总行数, 失败的 ts_code 列表)。"""
    failed = []
    rows = 0
    for ts_code in ts_codes:
        try:
            recs = client.call("daily", ts_code=ts_code, start_date=start, end_date=end)
            for r in recs:
                con.execute("""INSERT OR REPLACE INTO ods_daily
                    (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, adj_factor, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                    (r["ts_code"], r["trade_date"], r["open"], r["high"], r["low"],
                     r["close"], r["vol"], r["amount"], r["pct_chg"], r.get("adj_factor")))
                rows += 1
            basics = client.call("daily_basic", ts_code=ts_code, start_date=start, end_date=end)
            for r in basics:
                con.execute("""INSERT OR REPLACE INTO ods_daily_basic
                    (ts_code, trade_date, total_mv, pe_ttm, turnover_rate, volume_ratio, fetched_at)
                    VALUES (?,?,?,?,?,?,datetime('now'))""",
                    (r["ts_code"], r["trade_date"], r.get("total_mv"), r.get("pe_ttm"),
                     r.get("turnover_rate"), r.get("volume_ratio")))
                rows += 1
        except Exception as e:
            logger.error(f"Failed {ts_code}: {e}")
            failed.append(ts_code)
    return rows, failed

def get_all_active_codes(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT ts_code FROM ods_stock_basic WHERE delist_date IS NULL OR delist_date=''").fetchall()]
```

- [ ] **Step 2: 编写测试 → Commit**

---

### Task 6: ODS Fetch — moneyflow

**Files:** `backend/fetch/ods_moneyflow.py`

同 Task 5 模式，16 个字段的 `INSERT OR REPLACE`。省略重复代码。

- [ ] **Step 1: 实现 + 测试 → Commit**

---

### Task 7: ETL 基础工具 (EMA/SMA/线性回归)

**Files:** `backend/etl/base.py`, `tests/test_etl/test_base.py`

- [ ] **Step 1: 编写失败测试**

```python
# tests/test_etl/test_base.py
import numpy as np
from backend.etl.base import ema, sma, linear_regression_slope

def test_ema_seed_is_sma_of_first_n():
    prices = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 15.0, 20.0], dtype=float)
    result = ema(prices, 5)
    # seed at index 4 = mean of first 5 = 10.0
    assert abs(result[4] - 10.0) < 0.01
    # index 5: alpha=2/6=0.333, EMA = 0.333*15 + 0.667*10 = 11.667
    assert abs(result[5] - 11.667) < 0.1

def test_ema_skips_nan():
    prices = np.array([10.0, np.nan, np.nan, 12.0, 13.0, 14.0, 15.0, 16.0], dtype=float)
    result = ema(prices, 5)
    # NaN positions should carry forward previous value
    assert result[1] == result[0]
    assert result[2] == result[0]

def test_sma_basic():
    prices = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = sma(prices, 3)
    assert np.isnan(result[0]) and np.isnan(result[1])
    assert abs(result[2] - 2.0) < 0.01  # (1+2+3)/3
    assert abs(result[3] - 3.0) < 0.01  # (2+3+4)/3

def test_linear_regression_slope_positive():
    y = np.array([1.0, 2.0, 4.0, 8.0, 16.0])
    slope = linear_regression_slope(y)
    assert slope > 0  # 对数空间斜率 > 0
```

- [ ] **Step 2: 运行验证失败 → 实现 base.py**

```python
# backend/etl/base.py
import numpy as np

def ema(series: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(series), np.nan)
    alpha = 2.0 / (period + 1)
    valid_idx = np.where(~np.isnan(series))[0]
    if len(valid_idx) < period:
        return result
    seed_slice = series[valid_idx[:period]]
    seed = np.nanmean(seed_slice)
    result[valid_idx[period - 1]] = seed
    for i in range(valid_idx[period - 1] + 1, len(series)):
        if np.isnan(series[i]):
            result[i] = result[i - 1]
        else:
            result[i] = alpha * series[i] + (1 - alpha) * result[i - 1]
    return result

def sma(series: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(series), np.nan)
    for i in range(period - 1, len(series)):
        window = series[i - period + 1:i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) > 0:
            result[i] = np.mean(valid)
    return result

def linear_regression_slope(y: np.ndarray) -> float:
    y = np.array(y, dtype=float)
    mask = ~np.isnan(y) & (y > 0)
    if mask.sum() < 2:
        return 0.0
    x = np.arange(len(y))[mask]
    log_y = np.log(y[mask])
    slope = np.polyfit(x, log_y, 1)[0]
    return slope
```

- [ ] **Step 3: 运行测试 → PASS → Commit**

```bash
pytest tests/test_etl/test_base.py -v
```

---

### Task 8: DIM 层构建

**Files:** `backend/etl/build_dim.py`, `tests/test_etl/test_build_dim.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_etl/test_build_dim.py
def test_build_dim_stock_exchange_mapping(db_with_schema):
    db_with_schema.execute("INSERT INTO ods_stock_basic (ts_code,symbol,name,exchange) VALUES ('000001.SZ','000001','平安银行','SZSE')")
    db_with_schema.execute("INSERT INTO ods_stock_basic (ts_code,symbol,name,exchange) VALUES ('600001.SH','600001','上证测试','SSE')")
    from backend.etl.build_dim import build_dim_stock
    n = build_dim_stock(db_with_schema)
    assert n == 2
    row = db_with_schema.execute("SELECT exchange, sector, stock_code FROM dim_stock WHERE ts_code='000001.SZ'").fetchone()
    assert row[0] == "深圳"
    assert row[1] == "主板"
    assert row[2] == "000001"

def test_build_dim_stock_st_detection(db_with_schema):
    db_with_schema.execute("INSERT INTO ods_stock_basic (ts_code,symbol,name,exchange) VALUES ('000001.SZ','000001','*ST平安','SZSE')")
    from backend.etl.build_dim import build_dim_stock
    build_dim_stock(db_with_schema)
    row = db_with_schema.execute("SELECT is_st FROM dim_stock WHERE ts_code='000001.SZ'").fetchone()
    assert row[0] == 1
```

- [ ] **Step 2: 实现 → 测试 PASS → Commit**

```python
# backend/etl/build_dim.py
def build_dim_stock(con) -> int:
    con.execute("DELETE FROM dim_stock")
    con.execute("""INSERT INTO dim_stock (ts_code, stock_code, symbol, name, exchange, sector, industry, list_date, delist_date, is_active, is_st)
    SELECT ts_code, symbol AS stock_code, symbol, name,
        CASE exchange WHEN 'SSE' THEN '上海' WHEN 'SZSE' THEN '深圳' WHEN 'BSE' THEN '北京' ELSE exchange END,
        CASE WHEN ts_code LIKE '60%' THEN '主板' WHEN ts_code LIKE '00%' THEN '主板'
             WHEN ts_code LIKE '30%' THEN '创业板' WHEN ts_code LIKE '68%' THEN '科创板' ELSE '北交所' END,
        industry, list_date, delist_date,
        CASE WHEN delist_date IS NULL OR delist_date='' THEN 1 ELSE 0 END,
        CASE WHEN name LIKE '%ST%' OR name LIKE '%*ST%' THEN 1 ELSE 0 END
    FROM ods_stock_basic""")
    return con.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
```

- [ ] **Step 3: 实现 build_dim_date + build_dim_concept**（同模式，代码省略）

---

### Task 9: DWD 层 — 前复权 + 停牌填充 + 周线聚合

**Files:** `backend/etl/build_dwd.py`, `tests/test_etl/test_build_dwd.py`

- [ ] **Step 1: 编写前复权测试**

```python
# tests/test_etl/test_build_dwd.py
def test_qfq_formula(db_with_schema):
    """前复权: price_qfq = price × adj_factor / latest_adj_factor"""
    db_with_schema.execute("INSERT INTO ods_stock_basic (ts_code,symbol,name) VALUES ('TEST.SZ','TEST','测试')")
    db_with_schema.execute("INSERT INTO ods_trade_cal (cal_date,is_open) VALUES ('20260101',1),('20260102',1)")
    db_with_schema.execute("INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor) VALUES ('TEST.SZ','20260101',10.0,4.0),('TEST.SZ','20260102',12.0,2.0)")
    db_with_schema.execute("INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv) VALUES ('TEST.SZ','20260101',1000),('TEST.SZ','20260102',1200)")
    from backend.etl.build_dwd import build_dwd_daily_quote
    build_dwd_daily_quote(db_with_schema, ["TEST.SZ"])
    rows = db_with_schema.execute("SELECT trade_date, close_qfq FROM dwd_daily_quote WHERE ts_code='TEST.SZ' ORDER BY trade_date").fetchall()
    # 20260101: latest_adj=2.0, close_qfq = 10.0 × 4.0 / 2.0 = 20.0
    assert abs(rows[0][1] - 20.0) < 0.01
    # 20260102: close_qfq = 12.0 × 2.0 / 2.0 = 12.0
    assert abs(rows[1][1] - 12.0) < 0.01
```

- [ ] **Step 2: 实现 → 测试 PASS → Commit**

```python
# backend/etl/build_dwd.py
def build_dwd_daily_quote(con, ts_codes: list[str] | None = None) -> int:
    if ts_codes is None:
        ts_codes = [r[0] for r in con.execute("SELECT ts_code FROM ods_stock_basic").fetchall()]
    for ts_code in ts_codes:
        latest = con.execute(
            "SELECT adj_factor FROM ods_daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
            (ts_code,)).fetchone()
        if latest is None:
            continue
        la = latest[0]
        con.execute("""INSERT OR REPLACE INTO dwd_daily_quote
            (ts_code, trade_date, open_qfq, high_qfq, low_qfq, close_qfq,
             vol, amount, pct_chg, total_mv, pe_ttm, turnover_rate, volume_ratio, is_suspended)
        SELECT d.ts_code, d.trade_date,
            d.open*? , d.high*? , d.low*? , d.close*? ,
            d.vol, d.amount, d.pct_chg,
            b.total_mv, b.pe_ttm, b.turnover_rate, b.volume_ratio, 0
        FROM ods_daily d
        LEFT JOIN ods_daily_basic b ON d.ts_code=b.ts_code AND d.trade_date=b.trade_date
        WHERE d.ts_code=?""", (la, la, la, la, ts_code))
    return len(ts_codes)
```

- [ ] **Step 3: 实现 dwd_weekly_quote + dwd_daily_moneyflow**（代码省略）

---

### Task 10: DWS — MACD Calculator

**Files:** `backend/etl/calc_macd.py`, `tests/test_etl/test_calc_macd.py`

- [ ] **Step 1: 编写单元测试**

```python
# tests/test_etl/test_calc_macd.py
import pandas as pd
from backend.etl.calc_macd import MACDCalculator

def test_macd_basic_calculation():
    """已知 OHLCV → 验证 EMA12/EMA26/DIF/DEA/MACD柱。"""
    df = pd.DataFrame({
        "trade_date": [f"202601{i:02d}" for i in range(1, 31)],
        "close_qfq": [10.0 + i * 0.1 for i in range(30)]  # 10.0 → 12.9
    })
    calc = MACDCalculator.__new__(MACDCalculator)
    result = calc._compute_indicators(df)
    # EMA12 种子在第 11 个位置 (index 11)
    assert not pd.isna(result["ema_12"].iloc[11])
    # MACD柱 = 2 × (DIF - DEA)
    idx = 27  # 第28个值，EMA26种子+DEA种子都已完成
    expected_bar = 2.0 * (result["dif"].iloc[idx] - result["dea"].iloc[idx])
    assert abs(result["macd_bar"].iloc[idx] - expected_bar) < 0.001

def test_macd_golden_cross_detection():
    """MACD柱从负翻正 → 金叉。"""
    df = pd.DataFrame({
        "trade_date": [f"202601{i:02d}" for i in range(1, 31)],
        "close_qfq": [10.0] * 30
    })
    calc = MACDCalculator.__new__(MACDCalculator)
    result = calc._compute_indicators(df)
    result = calc._compute_turning_points(result)
    # 常量价格 → MACD柱 = 0 → 不应有金叉（从负翻正，不能从0翻正）
    golden = [r for r in result["turning_point"] if r == "golden_cross"]
    # 常量价格下 EMA12=EMA26 → DIF=0 → MACD柱=0，无金叉
    valid = [v for v in result["turning_point"].dropna() if v is not None]
    # 应该没有 golden_cross（因为 MACD柱始终为0，不是从负翻正）
    assert "golden_cross" not in str(valid)
```

- [ ] **Step 2: 运行验证失败 → 实现 MACDCalculator**

```python
# backend/etl/calc_macd.py
import numpy as np
import pandas as pd
from backend.etl.base import ema

class MACDCalculator:
    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        src = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.dws = f"dws_macd_{freq}"
        self.src = src

    def calculate(self, ts_codes: list[str], calc_date: str):
        for ts_code in ts_codes:
            df = self.con.execute(f"""
            SELECT trade_date, close_qfq FROM {self.src}
            WHERE ts_code=? AND is_suspended=0 ORDER BY trade_date
            """, (ts_code,)).df()
            if df.empty or len(df) < 27:
                continue
            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date)

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["close_qfq"].values
        df["ema_12"] = ema(c, 12)
        df["ema_26"] = ema(c, 26)
        df["dif"] = df["ema_12"] - df["ema_26"]
        df["dea"] = ema(df["dif"].values, 9)
        df["macd_bar"] = 2.0 * (df["dif"] - df["dea"])
        df["zone"] = df["macd_bar"].apply(lambda x: "bull" if x > 0 else ("bear" if x < 0 else None))
        df["trend"] = self._trend(df["macd_bar"].values)
        df["divergence"] = self._divergence(df)
        df["turning_point"] = self._turning(df)
        df["alert"] = self._alerts(df)
        return df

    def _trend(self, bar: np.ndarray) -> list:
        r = [None] * len(bar)
        for i in range(3, len(bar)):
            if all(bar[i-j] > bar[i-j-1] for j in range(3)):
                r[i] = "up"
            elif all(bar[i-j] < bar[i-j-1] for j in range(3)):
                r[i] = "down"
            else:
                r[i] = "flat"
        return r

    def _divergence(self, df: pd.DataFrame) -> list:
        r = [None] * len(df)
        w = 60
        for i in range(w, len(df)):
            c_hi = df["close_qfq"].iloc[i-w:i+1].max()
            c_lo = df["close_qfq"].iloc[i-w:i+1].min()
            d_hi = df["dif"].iloc[i-w:i+1].max()
            d_lo = df["dif"].iloc[i-w:i+1].min()
            if df["close_qfq"].iloc[i] >= c_hi and df["dif"].iloc[i] < d_hi:
                pk = df["dif"].iloc[i-w:i+1].idxmax()
                if pk < i:
                    r[i] = "top_divergence"
            if df["close_qfq"].iloc[i] <= c_lo and df["dif"].iloc[i] > d_lo:
                vl = df["dif"].iloc[i-w:i+1].idxmin()
                if vl < i:
                    r[i] = "bottom_divergence"
        return r

    def _turning(self, df: pd.DataFrame) -> list:
        r = [None] * len(df)
        bar = df["macd_bar"].values
        for i in range(1, len(df)):
            if bar[i-1] is not None and bar[i] is not None and not np.isnan(bar[i-1]) and not np.isnan(bar[i]):
                if bar[i-1] <= 0 and bar[i] > 0:
                    r[i] = "golden_cross"
                elif bar[i-1] >= 0 and bar[i] < 0:
                    r[i] = "dead_cross"
        return r

    def _alerts(self, df: pd.DataFrame) -> list:
        r = [None] * len(df)
        bar = df["macd_bar"].values
        for i in range(4, len(df)):
            if all(bar[i-3-j] > bar[i-4-j] for j in range(3)) and bar[i] < bar[i-1]:
                r[i] = "upturn_reverse"
            elif all(bar[i-3-j] < bar[i-4-j] for j in range(3)) and bar[i] > bar[i-1]:
                r[i] = "downturn_reverse"
        return r

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        for _, row in df.iterrows():
            self.con.execute(f"""INSERT OR REPLACE INTO {self.dws}
            (ts_code, trade_date, ema_12, ema_26, dif, dea, macd_bar,
             divergence, zone, turning_point, alert, trend, calc_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts_code, row["trade_date"],
             row.get("ema_12"), row.get("ema_26"), row.get("dif"),
             row.get("dea"), row.get("macd_bar"),
             row.get("divergence"), row.get("zone"),
             row.get("turning_point"), row.get("alert"), row.get("trend"),
             calc_date))
```

- [ ] **Step 3: 运行测试 → PASS → Commit**

---

### Tasks 11-14: DWS Calculators — MA, KPattern, DDE, Volume

Each follows the same pattern as Task 10: `Calculator.__init__(con, freq)` → `calculate(ts_codes, calc_date)` → `_compute_indicators(df)` → `_insert(...)`.

Key formulas (from spec sections 6.2-6.5):

| Task | File | Key outputs |
|------|------|------------|
| 11 | `calc_ma.py` | MA5, MA10, bias(乖离率), slope(3日斜率), alignment(9值), turning_point |
| 12 | `calc_kpattern.py` | 7 种形态布尔 + strength(0-1), IPO过滤, 涨跌停过滤(9.9%), ST过滤(4.9%) |
| 13 | `calc_dde.py` | DDX=(lg+elg净买入)/total_vol, DDX2=EMA(DDX,5), divergence, trend, alert |
| 14 | `calc_volume.py` | MA5_vol, pct_vol_rank(120日百分位), zone(爆量/地量/正常 含迟滞), trend(对数斜率) |

Each task: write test first → verify it fails → implement → verify PASS → commit.

---

### Task 15: ETL 编排器 + 错误处理

**Files:** `backend/etl/orchestrator.py`, `backend/etl/error_handler.py`, `tests/test_etl/test_orchestrator.py`

- [ ] **Step 1: 实现 error_handler.py**

```python
# backend/etl/error_handler.py
import logging, json
from datetime import datetime

logger = logging.getLogger(__name__)

def log_etl(con, step_name: str, status: str, row_count: int = 0,
            error_msg: str = "", data_completeness: dict | None = None):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    comp = json.dumps(data_completeness) if data_completeness else None
    con.execute("""INSERT INTO ods_etl_log (step_name, started_at, finished_at, status, row_count, error_msg, data_completeness)
        VALUES (?,?,?,?,?,?,?)""", (step_name, now, now, status, row_count, error_msg, comp))
    if status in ("failed", "degraded"):
        logger.warning(f"ETL {step_name}: {status} — {error_msg}")

def check_data_completeness(con) -> dict:
    tables = ["ods_daily", "ods_daily_basic", "ods_moneyflow"]
    result = {}
    for t in tables:
        row = con.execute(f"SELECT MAX(trade_date) FROM {t}").fetchone()
        result[t] = row[0] if row and row[0] else None
    return result
```

- [ ] **Step 2: 实现编排器核心逻辑**

```python
# backend/etl/orchestrator.py
import logging
from datetime import datetime
from backend.db.connection import get_connection, check_connectivity, run_checkpoint
from backend.etl.error_handler import log_etl, check_data_completeness

logger = logging.getLogger(__name__)

def run_etl(step: str = "build-all", ts_codes: list[str] | None = None,
            start: str | None = None, end: str | None = None,
            batch_size: int = 100, force_full: bool = False):
    con = get_connection()
    try:
        # 0. 自检
        health = check_connectivity()
        if "fatal" in health.get("duckdb", ""):
            log_etl(con, "health_check", "failed", error_msg=health["duckdb"])
            raise RuntimeError(health["duckdb"])

        # 1. 断层检测
        latest_ods = con.execute("SELECT MAX(trade_date) FROM ods_daily").fetchone()[0]
        latest_cal = con.execute("SELECT MAX(trade_date) FROM dim_date WHERE is_trade_day=1").fetchone()[0]
        gap = _compute_gap(latest_ods, latest_cal)

        if gap <= 1 and not force_full:
            _run_incremental(con, ts_codes, start, end, batch_size)
        elif gap <= 60:
            _run_window_recalc(con, ts_codes, batch_size)
        else:
            _run_full_recalc(con, ts_codes, batch_size)

        run_checkpoint(con)
    finally:
        con.close()
```

- [ ] **Step 3: 编写编排器测试 → Commit**

---

### Task 16: CLI 入口

**Files:** `backend/cli.py`, `tests/test_cli.py`

5 个子命令 (spec 8.1): `check`, `etl`, `query`, `export`, `status`。使用 `argparse`。

- [ ] **Step 1: 编写 smoke test**

```python
# tests/test_cli.py
import subprocess, sys

def test_cli_check_runs():
    result = subprocess.run([sys.executable, "-m", "backend.cli", "check"],
                            capture_output=True, text=True)
    assert result.returncode in (0, 1)
    assert "DuckDB" in result.stdout or "DuckDB" in result.stderr or "tushare" in result.stdout
```

- [ ] **Step 2: 实现 CLI（关键子命令）**

```python
# backend/cli.py
import argparse, sys, logging
from backend.config import LOG_LEVEL
logging.basicConfig(level=getattr(logging, LOG_LEVEL))

def cmd_check(_args):
    from backend.db.connection import check_connectivity
    from backend.fetch.client import TushareClient
    db = check_connectivity()
    print(f"DuckDB: {db['duckdb']} (v{db['version']})")
    print(f"Disk free: {db['disk_free_mb']} MB")
    try:
        TushareClient().call("stock_basic", exchange="", list_status="L", limit=1)
        print("tushare: connected")
    except Exception as e:
        print(f"tushare: error — {e}")

def cmd_etl(args):
    from backend.etl.orchestrator import run_etl
    run_etl(step=args.step, start=args.start, end=args.end,
            batch_size=args.batch_size, force_full=args.force_full)

def cmd_query(args):
    from backend.db.connection import get_connection
    con = get_connection(read_only=True)
    sql = f"SELECT * FROM v_dws_macd_{args.freq}_latest WHERE ts_code=? AND trade_date=(SELECT MAX(trade_date) FROM v_dws_macd_{args.freq}_latest WHERE ts_code=?)"
    row = con.execute(sql, (args.ts_code, args.ts_code)).fetchone()
    if row:
        cols = [d[0] for d in con.description]
        for c, v in zip(cols, row):
            print(f"{c}: {v}")
    else:
        print(f"No data for {args.ts_code}")

def cmd_export(args):
    from backend.export_wide import export_wide_to_excel
    n = export_wide_to_excel(args.db_path or "data/tradeanalysis.duckdb",
                             args.date, args.output, freq=args.freq,
                             filter_st=not args.include_st)
    print(f"Exported {n} rows → {args.output}")

def cmd_status(_args):
    from backend.db.connection import get_connection
    con = get_connection(read_only=True)
    for table in ["ods_daily", "ods_daily_basic", "ods_moneyflow",
                   "dwd_daily_quote", "dws_macd_daily"]:
        try:
            cnt = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            latest = con.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()[0]
            print(f"{table:25s} {cnt:>12,}  {latest or 'N/A'}")
        except Exception:
            print(f"{table:25s}  (not found)")

def main():
    p = argparse.ArgumentParser(prog="tradeanalysis")
    sp = p.add_subparsers(dest="command")
    sp.add_parser("check", help="Check connectivity")
    ep = sp.add_parser("etl", help="Run ETL")
    ep.add_argument("--step", default="build-all")
    ep.add_argument("--start")
    ep.add_argument("--end")
    ep.add_argument("--batch-size", type=int, default=100)
    ep.add_argument("--force-full", action="store_true")
    qp = sp.add_parser("query", help="Query DWS")
    qp.add_argument("--ts-code", required=True)
    qp.add_argument("--freq", default="daily")
    xp = sp.add_parser("export", help="Export Excel")
    xp.add_argument("--date", required=True)
    xp.add_argument("--output", default="analysis.xlsx")
    xp.add_argument("--freq", default="daily")
    xp.add_argument("--db-path")
    xp.add_argument("--include-st", action="store_true")
    sp.add_parser("status", help="Show database status")
    args = p.parse_args()
    {"check": cmd_check, "etl": cmd_etl, "query": cmd_query,
     "export": cmd_export, "status": cmd_status}.get(args.command, lambda _: p.print_help())(args)

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 测试 → Commit**

---

### Task 17: FastAPI 端点

**Files:** `backend/api/app.py`, `backend/api/router.py`, `backend/api/models.py`, `tests/test_api/test_router.py`

5 端点 (spec 11.2): `GET /api/v1/analysis/{ts_code}`, `GET .../history`, `GET /screening`, `GET /market/overview`, `GET /health`。使用 FastAPI `TestClient` + `httpx` 测试。

- [ ] **Step 1: 实现 models.py + router.py + app.py → 测试 → Commit**

---

### Task 18: Excel 导出

**Files:** `backend/export_wide.py`, `tests/test_export_wide.py`

代码直接来自 spec 7.2 节（`export_wide_to_excel` + `_reorder_signal_first` + `_write_sheet`）。

- [ ] **Step 1: 复制 spec 7.2 代码 → 测试 → Commit**

---

### Task 19: 集成测试 + 黄金数据集回归

**Files:** `tests/fixtures/golden_data.py`

覆盖 10 只股票（主板≥3 + 创业板≥2 + 科创板≥1 + ≥3 行业）+ 极端行情日 (2015-07-08, 2020-03-23, 2024-02-05)。

- [ ] **Step 1: 端到端测试：temp_db → create_all_tables → fetch mock data → build DIM → build DWD → calc all DWS → query views → export Excel**

```python
# tests/test_integration.py
def test_full_pipeline_single_stock(db_with_schema):
    """单只股票完整流水线：ODS → DIM → DWD → DWS → ADS → Export"""
    # 插入最小 ODS 数据（10 个交易日）
    db_with_schema.execute("INSERT INTO ods_stock_basic (ts_code,symbol,name,exchange) VALUES ('000001.SZ','000001','平安银行','SZSE')")
    for i in range(1, 11):
        db_with_schema.execute("INSERT INTO ods_daily (ts_code,trade_date,close,adj_factor) VALUES (?,?,?,?)",
            ('000001.SZ', f'202601{i:02d}', 10.0 + i * 0.5, 1.0))
        db_with_schema.execute("INSERT INTO ods_daily_basic (ts_code,trade_date,total_mv,pe_ttm) VALUES (?,?,?,?)",
            ('000001.SZ', f'202601{i:02d}', 10000 + i * 100, 8.0 + i * 0.1))
        db_with_schema.execute("INSERT INTO ods_trade_cal (cal_date,is_open) VALUES (?,1)", (f'202601{i:02d}',))
    # 运行完整 ETL
    from backend.etl.build_dim import build_dim_stock, build_dim_date
    from backend.etl.build_dwd import build_dwd_daily_quote
    from backend.etl.calc_macd import MACDCalculator
    build_dim_stock(db_with_schema)
    build_dim_date(db_with_schema)
    build_dwd_daily_quote(db_with_schema, ["000001.SZ"])
    MACDCalculator(db_with_schema, "daily").calculate(["000001.SZ"], "20260110")
    # 验证 DWS 有输出
    cnt = db_with_schema.execute("SELECT COUNT(*) FROM dws_macd_daily").fetchone()[0]
    assert cnt > 0
    # 验证 latest 视图可查询
    row = db_with_schema.execute("SELECT * FROM v_dws_macd_daily_latest WHERE ts_code='000001.SZ' LIMIT 1").fetchone()
    assert row is not None
```

- [ ] **Step 2: 运行全部测试**

```bash
pytest tests/ -v --cov=backend --cov-report=term-missing
```

---

## 验证

```bash
# 全部单元测试
pytest tests/ -v

# 带覆盖率（目标 >80%）
pytest tests/ -v --cov=backend --cov-report=term-missing

# CLI smoke
python -m backend.cli check
python -m backend.cli status

# API smoke (需要先跑 ETL 有数据)
uvicorn backend.api.app:app --reload &
curl http://localhost:8000/api/v1/health
```
