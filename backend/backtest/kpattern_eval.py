"""K-pattern backtest evaluation.

Evaluates each of the 7 K-line patterns across all stocks and dates,
computing win rates, profit factors, and signal frequencies split by
market regime and holding period.

Reads from production DuckDB (read-only). Writes results to a separate
backtest DuckDB file.
"""
import duckdb
import pandas as pd
import numpy as np
from typing import Optional

from backend.backtest.engine import get_market_regime

PATTERNS = [
    "yang_bao_yin", "yang_ke_yin",
    "mu_bei_xian", "bi_lei_zhen", "gao_kai_chang_yin",
    "yin_bao_yang", "yin_ke_yang",
]

HOLDING_DAYS = [1, 3, 5, 10, 20]


def evaluate_pattern(
    prod_db_path: str,
    pattern: str,
    backtest_db_path: str,
    freq: str = "daily",
) -> dict:
    """Evaluate a single K-pattern across all stocks.

    Returns dict with keys:
      - pattern: pattern name
      - total_signals: total number of triggered signals
      - untradable_pct: percentage of signals where next day is limit-locked
      - by_regime: dict of market_regime → stats
      - by_holding: dict of holding_days → stats
    """
    view = f"v_dws_kpattern_{freq}_latest"
    dwd = f"dwd_{freq}_quote"

    con = duckdb.connect(prod_db_path, read_only=True)
    try:
        # Single batch query: join pattern signals + forward prices + index +
        # market regime + tradability — all in one SQL pass.
        holding_cols = ", ".join(
            f"LEAD(close_qfq, {d}) OVER (PARTITION BY ts_code ORDER BY trade_date) AS c{d}"
            for d in HOLDING_DAYS
        )
        idx_holding_cols = ", ".join(
            f"LEAD(close_qfq, {d}) OVER (ORDER BY trade_date) AS idx_c{d}"
            for d in HOLDING_DAYS
        )
        ret_cols = ", ".join(
            f"CASE WHEN f.entry_price > 0 THEN (f.c{d} - f.entry_price) / f.entry_price END AS ret_d{d}"
            for d in HOLDING_DAYS
        )
        idx_ret_cols = ", ".join(
            f"CASE WHEN i.idx_entry > 0 THEN (i.idx_c{d} - i.idx_entry) / i.idx_entry END AS idx_ret_d{d}"
            for d in HOLDING_DAYS
        )

        sql = f"""
            WITH fwd AS (
                SELECT ts_code, trade_date, close_qfq, open_qfq,
                    high_qfq, low_qfq, pct_chg,
                    LEAD(open_qfq, 1) OVER (PARTITION BY ts_code ORDER BY trade_date) AS entry_price,
                    {holding_cols},
                    LEAD(pct_chg, 1) OVER (PARTITION BY ts_code ORDER BY trade_date) AS next_pct,
                    LEAD(open_qfq, 1) OVER (PARTITION BY ts_code ORDER BY trade_date) AS next_o,
                    LEAD(high_qfq, 1) OVER (PARTITION BY ts_code ORDER BY trade_date) AS next_h,
                    LEAD(low_qfq, 1) OVER (PARTITION BY ts_code ORDER BY trade_date) AS next_l
                FROM {dwd} WHERE is_suspended = 0
            ),
            idx_fwd AS (
                SELECT trade_date,
                    LEAD(open_qfq, 1) OVER (ORDER BY trade_date) AS idx_entry,
                    {idx_holding_cols}
                FROM {dwd} WHERE ts_code = '000001.SH'
            ),
            regime_data AS (
                SELECT trade_date,
                    CASE WHEN ma60 IS NULL OR ma60 = 0 THEN 'sideways'
                         WHEN close_qfq / ma60 > 1.02 THEN 'bull'
                         WHEN close_qfq / ma60 < 0.98 THEN 'bear'
                         ELSE 'sideways' END AS regime
                FROM (
                    SELECT trade_date, close_qfq,
                        AVG(close_qfq) OVER (ORDER BY trade_date
                            ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60
                    FROM {dwd} WHERE ts_code = '000001.SH'
                )
            )
            SELECT k.ts_code, k.trade_date, k.strength,
                {ret_cols},
                {idx_ret_cols},
                CASE WHEN f.entry_price IS NULL THEN 'no_next_day'
                     WHEN f.next_o = f.next_h AND f.next_h = f.next_l
                          AND f.next_pct >= 9.0 THEN 'limit_up'
                     WHEN f.next_o = f.next_h AND f.next_h = f.next_l
                          AND f.next_pct <= -9.0 THEN 'limit_down'
                     ELSE NULL END AS untradable_reason,
                COALESCE(r.regime, 'sideways') AS regime
            FROM {view} k
            JOIN fwd f ON k.ts_code = f.ts_code AND k.trade_date = f.trade_date
            JOIN idx_fwd i ON k.trade_date = i.trade_date
            LEFT JOIN regime_data r ON k.trade_date = r.trade_date
            WHERE k.{pattern} = 1
        """
        result_df = con.execute(sql).df()

        if result_df.empty:
            return {
                "pattern": pattern,
                "total_signals": 0,
                "untradable_pct": 0.0,
                "by_regime": {},
                "by_holding": {},
            }

        # Derive is_tradable from untradable_reason
        result_df["is_tradable"] = result_df["untradable_reason"].isna()
        tradable_df = result_df[result_df["is_tradable"]]

        # Summary statistics
        total = len(result_df)
        untradable = total - len(tradable_df)

        by_regime = {}
        for regime in ["bull", "bear", "sideways"]:
            reg = tradable_df[tradable_df["regime"] == regime]
            if len(reg) == 0:
                continue
            by_regime[regime] = _summary_for_subset(reg, HOLDING_DAYS)

        by_holding = {}
        for d in HOLDING_DAYS:
            col = f"ret_d{d}"
            excess_col = f"excess_d{d}"
            valid = tradable_df[tradable_df[col].notna()]
            if len(valid) == 0:
                continue
            by_holding[d] = {
                "count": len(valid),
                "win_rate": (valid[col] > 0).mean(),
                "avg_return": valid[col].mean(),
                "avg_loss": valid[valid[col] < 0][col].mean()
                if (valid[col] < 0).any() else 0.0,
                "avg_win": valid[valid[col] > 0][col].mean()
                if (valid[col] > 0).any() else 0.0,
                "profit_factor": _profit_factor(valid[col]),
            }

        return {
            "pattern": pattern,
            "total_signals": total,
            "untradable_pct": untradable / total * 100 if total > 0 else 0.0,
            "by_regime": by_regime,
            "by_holding": by_holding,
        }
    finally:
        con.close()


def evaluate_all(
    prod_db_path: str,
    backtest_db_path: str,
    freq: str = "daily",
) -> pd.DataFrame:
    """Evaluate all 7 patterns and return a comparison DataFrame."""
    rows = []
    for pattern in PATTERNS:
        result = evaluate_pattern(prod_db_path, pattern, backtest_db_path, freq)
        # Extract top-level stats into a flat row
        hold_stats = result["by_holding"]
        row = {
            "pattern": pattern,
            "total_signals": result["total_signals"],
            "untradable_pct": f"{result['untradable_pct']:.1f}%",
        }
        for d in HOLDING_DAYS:
            if d in hold_stats:
                row[f"d{d}_wr"] = f"{hold_stats[d]['win_rate']:.1%}"
                row[f"d{d}_avg"] = f"{hold_stats[d]['avg_return']:.2%}"
                row[f"d{d}_pf"] = f"{hold_stats[d]['profit_factor']:.2f}"
                row[f"d{d}_n"] = hold_stats[d]["count"]
            else:
                row[f"d{d}_wr"] = "N/A"
                row[f"d{d}_avg"] = "N/A"
                row[f"d{d}_pf"] = "N/A"
                row[f"d{d}_n"] = 0
        # Per-regime win rates
        for regime in ["bull", "bear", "sideways"]:
            if regime in result["by_regime"]:
                row[f"{regime}_d5_wr"] = f"{result['by_regime'][regime].get(5, {}).get('win_rate', 0):.1%}"
            else:
                row[f"{regime}_d5_wr"] = "N/A"
        rows.append(row)

    return pd.DataFrame(rows)


# ── internal helpers ──────────────────────────────────────────────


def _check_tradable(con, ts_code: str, trade_date: str, freq: str, dwd: str) -> tuple:
    """Check if the signal is tradable (next day is NOT a one-sided limit board).

    Returns (True, None) if tradable, (False, reason) if not.
    """
    next_date_row = con.execute(f"""
        SELECT trade_date FROM {dwd}
        WHERE ts_code = ? AND trade_date > ?
        ORDER BY trade_date LIMIT 1
    """, (ts_code, trade_date)).fetchone()

    if next_date_row is None:
        return False, "no_next_trading_day"

    next_date = next_date_row[0]
    row = con.execute(f"""
        SELECT open_qfq, high_qfq, low_qfq, close_qfq, pct_chg, is_suspended
        FROM {dwd}
        WHERE ts_code = ? AND trade_date = ?
    """, (ts_code, next_date)).fetchone()

    if row is None:
        return False, "next_day_missing"

    o, h, l, c, pct, suspended = row
    if suspended:
        return False, "next_day_suspended"

    # One-sided limit board: open == high == low == close
    is_limit = (o == h == l == c)
    # Only flag as untradable if it's a limit board in the direction
    # that prevents our trade
    if is_limit and pct >= 9.0:
        return False, "limit_up"
    if is_limit and pct <= -9.0:
        return False, "limit_down"

    return True, None


def _compute_forward_returns(
    con, ts_code: str, entry_date: str, entry_price: float,
    holding_days: list, freq: str, dwd: str,
) -> dict:
    """Compute forward returns for each holding period.

    Uses entry at NEXT day's open (T+1 execution).
    """
    # Get next trading day's open as actual entry price
    next_open_row = con.execute(f"""
        SELECT open_qfq FROM {dwd}
        WHERE ts_code = ? AND trade_date > ?
        ORDER BY trade_date LIMIT 1
    """, (ts_code, entry_date)).fetchone()

    if next_open_row is None:
        return {d: None for d in holding_days}

    actual_entry = next_open_row[0]
    if actual_entry <= 0:
        return {d: None for d in holding_days}

    # Get all future prices up to max holding
    max_days = max(holding_days)
    future = con.execute(f"""
        SELECT close_qfq FROM {dwd}
        WHERE ts_code = ? AND trade_date > ?
        ORDER BY trade_date
        LIMIT ?
    """, (ts_code, entry_date, max_days + 1)).fetchall()

    result = {}
    for d in holding_days:
        if d <= len(future):
            target_close = future[d - 1][0]  # 0-indexed: d=1 means first future day
            if target_close > 0:
                result[d] = (target_close - actual_entry) / actual_entry
            else:
                result[d] = None
        else:
            result[d] = None

    return result


def _compute_index_forward_returns(
    con, entry_date: str, holding_days: list, freq: str, dwd: str,
) -> dict:
    """Compute 000001.SH forward returns for excess return calculation."""
    # Get next trading day's open
    next_open_row = con.execute(f"""
        SELECT open_qfq FROM {dwd}
        WHERE ts_code = '000001.SH' AND trade_date > ?
        ORDER BY trade_date LIMIT 1
    """, (entry_date,)).fetchone()

    if next_open_row is None:
        return {d: None for d in holding_days}

    idx_entry = next_open_row[0]
    max_days = max(holding_days)
    future = con.execute(f"""
        SELECT close_qfq FROM {dwd}
        WHERE ts_code = '000001.SH' AND trade_date > ?
        ORDER BY trade_date LIMIT ?
    """, (entry_date, max_days + 1)).fetchall()

    result = {}
    for d in holding_days:
        if d <= len(future):
            result[d] = (future[d - 1][0] - idx_entry) / idx_entry
        else:
            result[d] = None

    return result


def _summary_for_subset(df: pd.DataFrame, holding_days: list) -> dict:
    """Compute summary stats for each holding period."""
    result = {}
    for d in holding_days:
        col = f"ret_d{d}"
        valid = df[col].dropna()
        if len(valid) < 5:
            continue
        wins = valid[valid > 0]
        losses = valid[valid < 0]
        result[d] = {
            "count": len(valid),
            "win_rate": (valid > 0).mean(),
            "avg_return": valid.mean(),
            "avg_win": wins.mean() if len(wins) > 0 else 0.0,
            "avg_loss": losses.mean() if len(losses) > 0 else 0.0,
            "profit_factor": _profit_factor(valid),
        }
    return result


def _profit_factor(returns: pd.Series) -> float:
    """Profit factor = gross profit / gross loss (absolute)."""
    wins = returns[returns > 0].sum()
    losses = abs(returns[returns < 0].sum())
    return wins / losses if losses > 0 else float("inf")


# ── CLI entry point ───────────────────────────────────────────────


if __name__ == "__main__":
    import sys
    prod_db = sys.argv[1] if len(sys.argv) > 1 else "data/tradeanalysis.duckdb"
    bt_db = sys.argv[2] if len(sys.argv) > 2 else "data/backtest.duckdb"

    print(f"Evaluating K-patterns from {prod_db}...")
    print(f"  Backtest results → {bt_db}\n")

    summary = evaluate_all(prod_db, bt_db)

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 12)
    print(summary.to_string(index=False))
