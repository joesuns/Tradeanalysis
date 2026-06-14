"""Extract B4 hard-gate columns from Tradeanalysis DuckDB or 123 SQLite."""
import sqlite3
from typing import List, Optional, Union

import pandas as pd

from backend.b4_gate.columns import (
    B4_DAILY_FIELDS,
    B4_WEEKLY_FIELDS,
    MAP_123_DAILY,
    MAP_123_WEEKLY,
    weekly_field_name,
)
from backend.b4_gate.enums import normalize_value

REF_123_TABLE = "batch_trend_results"


def resolve_week_end(con, analysis_date: str) -> Optional[str]:
    row = con.execute(
        """
        SELECT MAX(trade_date) FROM dim_date
        WHERE is_trade_day = 1 AND is_week_end = 1 AND trade_date <= ?
        """,
        [analysis_date],
    ).fetchone()
    return row[0] if row and row[0] else None


def _daily_b4_sql(ph: str) -> str:
    return f"""
        SELECT q.ts_code,
               q.trade_date,
               m.trend AS macd_trend,
               m.turning_point AS macd_zone,
               m.alert AS macd_alert,
               a.alignment AS ma_alignment,
               d.trend AS dde_trend,
               d.alert AS dde_alert,
               v.trend AS vol_trend
        FROM dwd_daily_quote q
        LEFT JOIN v_dws_macd_daily_latest m
          ON q.ts_code = m.ts_code AND q.trade_date = m.trade_date
        LEFT JOIN v_dws_ma_daily_latest a
          ON q.ts_code = a.ts_code AND q.trade_date = a.trade_date
        LEFT JOIN v_dws_dde_daily_latest d
          ON q.ts_code = d.ts_code AND q.trade_date = d.trade_date
        LEFT JOIN v_dws_volume_daily_latest v
          ON q.ts_code = v.ts_code AND q.trade_date = v.trade_date
        WHERE q.trade_date = ? AND q.ts_code IN ({ph})
          AND q.is_suspended = 0
    """


def _weekly_b4_sql(ph: str) -> str:
    cols = ", ".join([
        f"m.trend AS {weekly_field_name('macd_trend')}",
        f"m.turning_point AS {weekly_field_name('macd_zone')}",
        f"m.alert AS {weekly_field_name('macd_alert')}",
        f"a.alignment AS {weekly_field_name('ma_alignment')}",
        f"d.trend AS {weekly_field_name('dde_trend')}",
        f"d.alert AS {weekly_field_name('dde_alert')}",
        f"v.trend AS {weekly_field_name('vol_trend')}",
    ])
    return f"""
        SELECT qw.ts_code,
               {cols}
        FROM dwd_weekly_quote qw
        JOIN dim_date dd ON qw.trade_date = dd.trade_date AND dd.is_week_end = 1
        LEFT JOIN v_dws_macd_weekly_latest m
          ON qw.ts_code = m.ts_code AND qw.trade_date = m.trade_date
        LEFT JOIN v_dws_ma_weekly_latest a
          ON qw.ts_code = a.ts_code AND qw.trade_date = a.trade_date
        LEFT JOIN v_dws_dde_weekly_latest d
          ON qw.ts_code = d.ts_code AND qw.trade_date = d.trade_date
        LEFT JOIN v_dws_volume_weekly_latest v
          ON qw.ts_code = v.ts_code AND qw.trade_date = v.trade_date
        WHERE qw.trade_date = ? AND qw.ts_code IN ({ph})
    """


def extract_ta_b4(
    con,
    analysis_date: str,
    ts_codes: List[str],
) -> pd.DataFrame:
    if not ts_codes:
        return pd.DataFrame()
    ph = ",".join(["?"] * len(ts_codes))
    daily = con.execute(
        _daily_b4_sql(ph), [analysis_date] + list(ts_codes)
    ).df()
    week_end = resolve_week_end(con, analysis_date)
    if week_end:
        weekly = con.execute(
            _weekly_b4_sql(ph), [week_end] + list(ts_codes)
        ).df()
        out = daily.merge(weekly, on="ts_code", how="left")
        out["week_end"] = week_end
    else:
        out = daily.copy()
        out["week_end"] = None
        for f in B4_WEEKLY_FIELDS:
            out[weekly_field_name(f)] = None
    return out


def _build_123_select_sql() -> str:
    parts = [
        f"{col_123} AS {ta_field}"
        for col_123, ta_field in MAP_123_DAILY.items()
    ]
    parts.extend(
        f"{col_123} AS {weekly_field_name(ta_field)}"
        for col_123, ta_field in MAP_123_WEEKLY.items()
    )
    return ", ".join(parts)


def extract_123_b4(
    sqlite_path_or_con: Union[str, sqlite3.Connection],
    analysis_date: str,
    ts_codes: List[str],
    weekly_date: Optional[str] = None,
) -> pd.DataFrame:
    """Read 123 B4 from SQLite ``batch_trend_results``.

    Daily and weekly B4 columns live on the same row keyed by ``analysis_date``.
    ``weekly_date`` is metadata only (TA week-end anchor for diff context).
    """
    if not ts_codes:
        return pd.DataFrame()

    owned_con = False
    if isinstance(sqlite_path_or_con, str):
        con = sqlite3.connect(sqlite_path_or_con)
        owned_con = True
    else:
        con = sqlite_path_or_con

    ph = ",".join(["?"] * len(ts_codes))
    select_sql = _build_123_select_sql()
    try:
        cur = con.execute(
            f"""
            SELECT ts_code, {select_sql}
            FROM {REF_123_TABLE}
            WHERE analysis_date = ? AND ts_code IN ({ph})
            """,
            [analysis_date] + list(ts_codes),
        )
        rows = cur.fetchall()
        cols = ["ts_code"] + list(MAP_123_DAILY.values()) + [
            weekly_field_name(f) for f in MAP_123_WEEKLY.values()
        ]
        out = pd.DataFrame(rows, columns=cols)
    finally:
        if owned_con:
            con.close()

    out["trade_date"] = analysis_date
    out["week_end"] = weekly_date

    for col in B4_DAILY_FIELDS:
        if col in out.columns:
            out[col] = out[col].map(lambda x, c=col: normalize_value(c, x, "123"))
    for f in B4_WEEKLY_FIELDS:
        wcol = weekly_field_name(f)
        if wcol in out.columns:
            out[wcol] = out[wcol].map(
                lambda x, c=f: normalize_value(c, x, "123")
            )
    return out
