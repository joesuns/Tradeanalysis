import os
from dotenv import load_dotenv

load_dotenv()

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN")
if not TUSHARE_TOKEN:
    raise RuntimeError("TUSHARE_TOKEN 未设置，请检查 .env 文件")

DUCKDB_PATH = os.getenv("DUCKDB_PATH", "./data/tradeanalysis.duckdb")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "./data/tradeanalysis.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
# Progress logging — count throttle + time heartbeat (stderr anti-stall)
LOG_PROGRESS_HEARTBEAT_SEC = float(os.getenv("LOG_PROGRESS_HEARTBEAT_SEC", "30"))
LOG_PROGRESS_DAY_STEP = int(os.getenv("LOG_PROGRESS_DAY_STEP", "5"))
LOG_PROGRESS_STOCK_STEP = int(os.getenv("LOG_PROGRESS_STOCK_STEP", "5"))
ETL_WORKERS = int(os.getenv("ETL_WORKERS", "1"))
CALC_INCREMENTAL = os.getenv("CALC_INCREMENTAL", "1").strip() != "0"
# CALC_APPEND: when on (default), new-trading-day calc routes most stocks to the
# vectorized APPEND fast path (compute only new bars). =0 falls back to the
# CALC_INCREMENTAL narrow-window recompute; CALC_INCREMENTAL=0 falls back to full.
CALC_APPEND = os.getenv("CALC_APPEND", "1").strip() != "0"
# CALC_BATCH_APPEND: when on (default), new-day APPEND routes eligible stocks
# through cross-stock batch path. Requires CALC_APPEND.
CALC_BATCH_APPEND = os.getenv("CALC_BATCH_APPEND", "1").strip() != "0"
# CALC_FAST_SKIP: chunk batch preflight — skip stocks that would all route to SKIP
# without per-stock quote/DDE loads (same-day rerun). Requires CALC_APPEND.
CALC_FAST_SKIP = os.getenv("CALC_FAST_SKIP", "1").strip() != "0"
# CALC_SKIP_STATE_REFRESH: skip dws_calc_state UPSERT when history_fp unchanged on same calc_date
CALC_SKIP_STATE_REFRESH = os.getenv("CALC_SKIP_STATE_REFRESH", "1").strip() != "0"
# CALC_STRICT_DATE: when on (default), reject calc_date > MAX(ods_daily). =0 caps to ods_max.
CALC_STRICT_DATE = os.getenv("CALC_STRICT_DATE", "1").strip() != "0"
# CALC_WORKERS: optional override for calc thread-pool size (default min(cpu-1, 8)).
# DuckDB single-file lock forbids multi-process writes, so calc parallelism is
# thread-based (shared in-process instance), not multiprocessing.
