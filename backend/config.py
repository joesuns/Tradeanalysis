import os
from dotenv import load_dotenv

load_dotenv()

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN")
if not TUSHARE_TOKEN:
    raise RuntimeError("TUSHARE_TOKEN 未设置，请检查 .env 文件")

DUCKDB_PATH = os.getenv("DUCKDB_PATH", "./data/tradeanalysis.duckdb")
# 123 transition reference (SQLite batch_trend_results)
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_DEFAULT_REF_123_SQLITE = os.path.normpath(
    os.path.join(_PROJECT_ROOT, "../123/cache/stock_data.db")
)
REF_123_SQLITE_PATH = os.getenv("REF_123_SQLITE_PATH", _DEFAULT_REF_123_SQLITE)
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
# CALC_BATCH_FULL: when on (default), mass single-indicator FULL routes through
# batch FULL phase before chunk worker. Requires CALC_BATCH_APPEND.
CALC_BATCH_FULL = os.getenv("CALC_BATCH_FULL", "1").strip() != "0"
# CALC_FAST_SKIP: chunk batch preflight — skip stocks that would all route to SKIP
# without per-stock quote/DDE loads (same-day rerun). Requires CALC_APPEND.
CALC_FAST_SKIP = os.getenv("CALC_FAST_SKIP", "1").strip() != "0"
# CALC_SKIP_STATE_REFRESH: skip dws_calc_state UPSERT when history_fp unchanged on same calc_date
CALC_SKIP_STATE_REFRESH = os.getenv("CALC_SKIP_STATE_REFRESH", "1").strip() != "0"
# CALC_RECOVER_STATE: recover missing dws_calc_state from DWS data rows on cold preflight
# path, avoiding spurious FULL recalculation after an interrupted calc run.
CALC_RECOVER_STATE = os.getenv("CALC_RECOVER_STATE", "1").strip() != "0"
# CALC_SKIP_LOG_VERBOSE: 1=每股写入 skip_log；0=同批 fingerprint_match 只写摘要行
CALC_SKIP_LOG_VERBOSE = os.getenv("CALC_SKIP_LOG_VERBOSE", "0").strip() != "0"
# CALC_STRICT_DATE: when on (default), reject calc_date > MAX(ods_daily). =0 caps to ods_max.
CALC_STRICT_DATE = os.getenv("CALC_STRICT_DATE", "1").strip() != "0"
# CALC_FORCE_HARD: when on, --force always recalculates even if same-day data unchanged.
CALC_FORCE_HARD = os.getenv("CALC_FORCE_HARD", "0").strip() == "1"
# CALC_FORCE_BATCH_REUSE: when on (default), --force on unchanged data skips batch tail SQL.
CALC_FORCE_BATCH_REUSE = os.getenv("CALC_FORCE_BATCH_REUSE", "1").strip() != "0"
# CALC_DWD_FP_GATE: downgrade spurious FULL when history_fp stale but DWS input unchanged.
CALC_DWD_FP_GATE = os.getenv("CALC_DWD_FP_GATE", "1").strip() != "0"
# DWD_INCREMENTAL: daily run rebuilds only stale stocks + tail-day INSERT (no full-history DELETE).
DWD_INCREMENTAL = os.getenv("DWD_INCREMENTAL", "1").strip() != "0"
# DWD_REBUILD_REFRESH_STATE: after DWD rebuild, realign dws_calc_state history_fp
# before calc routing (prevents new-day FULL/chunk explosion). =0 disables.
DWD_REBUILD_REFRESH_STATE = os.getenv("DWD_REBUILD_REFRESH_STATE", "1").strip() != "0"
# DWD_DAILY_BASIC_MIN_COVERAGE: fraction of total_mv coverage required before tail INSERT.
# Below this threshold, daily_basic data is considered not yet available from tushare
# and the tail INSERT is deferred to the next run (preventing NULL total_mv/pe_ttm/turnover_rate).
DWD_DAILY_BASIC_MIN_COVERAGE = float(os.getenv("DWD_DAILY_BASIC_MIN_COVERAGE", "0.80"))
# REFRESH_STATE_PARALLEL: cli refresh-state isolated phase uses parallel read-only SQL.
REFRESH_STATE_PARALLEL = os.getenv("REFRESH_STATE_PARALLEL", "1").strip() != "0"
# CALC_REUSE_REFRESH_CTX: cli run passes refresh tails+modes into calc (skip batch reload).
CALC_REUSE_REFRESH_CTX = os.getenv("CALC_REUSE_REFRESH_CTX", "1").strip() != "0"
# CALC_VECTOR_APPEND: cross-stock vectorized EMA batch in batch_append MACD/DDE paths.
CALC_VECTOR_APPEND = os.getenv("CALC_VECTOR_APPEND", "1").strip() != "0"
# CALC_B4_WEEKLY_FAST: single resample + EWM for MACD weekly B4 (FULL/write window).
CALC_B4_WEEKLY_FAST = os.getenv("CALC_B4_WEEKLY_FAST", "1").strip() != "0"
# CALC_WORKERS: optional override for calc thread-pool size (default min(cpu-1, 8)).
CALC_COLUMN_NARROW = os.getenv("CALC_COLUMN_NARROW", "1").strip() != "0"
# CALC_AUTO_SPEC_REFRESH: before batch_append, narrow FULL stale spec_version rows.
CALC_AUTO_SPEC_REFRESH = os.getenv("CALC_AUTO_SPEC_REFRESH", "1").strip() != "0"
# EXPORT_SPEC_GATE: when on, export logs WARNING if state/DWS spec lags code (non-blocking).
EXPORT_SPEC_GATE = os.getenv("EXPORT_SPEC_GATE", "0").strip() == "1"
# DuckDB temp directory for out-of-core spill files.
# Relative paths are resolved against the database directory (DUCKDB_PATH dir).
# Default "tmp" → ./data/tmp/ next to the database file.
DUCKDB_TEMP_DIRECTORY = os.getenv("DUCKDB_TEMP_DIRECTORY", "tmp")
# DuckDB max memory in MB before spilling to temp_directory (default 4096 = 4 GiB).
# Lower values spill earlier → smaller temp files, but may slow down large queries.
DUCKDB_MAX_MEMORY_MB = int(os.getenv("DUCKDB_MAX_MEMORY_MB", "4096"))
# Minimum free disk space (MB) on the data directory before pipeline start is refused.
# Below this threshold, the pipeline aborts early instead of crashing mid-run.
MIN_DISK_FREE_MB = int(os.getenv("MIN_DISK_FREE_MB", "5120"))
# Number of old pre-* DuckDB backup files to retain during prune --cleanup-backups.
PRUNE_KEEP_BACKUPS = int(os.getenv("PRUNE_KEEP_BACKUPS", "2"))
# Number of most recent calc_date snapshots to retain during DWS pruning.
# Mirrors the CLI --keep default; override-able for cron/automation.
DWS_PRUNE_KEEP_RUNS = int(os.getenv("DWS_PRUNE_KEEP_RUNS", "2"))
# DuckDB single-file lock forbids multi-process writes, so calc parallelism is
# thread-based (shared in-process instance), not multiprocessing.
