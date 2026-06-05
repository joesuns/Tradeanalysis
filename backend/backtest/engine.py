"""Backtest engine core — market regime, tradability, forward returns.

All queries are READ-ONLY on the production database. Backtest results
are written to a separate DuckDB file.
"""
import duckdb
import numpy as np
import pandas as pd


# ── market regime (cached batch computation) ────────────────────────

_regime_cache: dict = {}


def _build_regime_cache(db_path: str) -> dict:
    """Pre-compute all market regimes in a single query."""
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute("""
            WITH ma_data AS (
                SELECT trade_date, close_qfq,
                    AVG(close_qfq) OVER (
                        ORDER BY trade_date
                        ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
                    ) AS ma_60
                FROM dwd_daily_quote
                WHERE ts_code = '000001.SH'
            )
            SELECT trade_date,
                CASE
                    WHEN ma_60 IS NULL OR ma_60 = 0 THEN 'sideways'
                    WHEN close_qfq / ma_60 > 1.02 THEN 'bull'
                    WHEN close_qfq / ma_60 < 0.98 THEN 'bear'
                    ELSE 'sideways'
                END AS regime
            FROM ma_data
            WHERE ma_60 IS NOT NULL
        """).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        con.close()


_global_regime_cache: dict = {}
_global_regime_db: str = ""


def get_market_regime(db_path: str, trade_date: str) -> str:
    """Classify market regime on a given date using MA60 rule.

    Uses a cached pre-computed lookup — 1 query for all dates.
    """
    global _global_regime_cache, _global_regime_db
    if db_path != _global_regime_db:
        _global_regime_cache = _build_regime_cache(db_path)
        _global_regime_db = db_path
    return _global_regime_cache.get(trade_date, "sideways")
