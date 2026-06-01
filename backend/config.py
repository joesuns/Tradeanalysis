import os
from dotenv import load_dotenv

load_dotenv()

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN")
if not TUSHARE_TOKEN:
    raise RuntimeError("TUSHARE_TOKEN 未设置，请检查 .env 文件")

DUCKDB_PATH = os.getenv("DUCKDB_PATH", "./data/tradeanalysis.duckdb")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
ETL_WORKERS = int(os.getenv("ETL_WORKERS", "1"))
