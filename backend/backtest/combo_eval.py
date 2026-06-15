"""Multi-dimension signal resonance backtesting.

Finds stocks where K-pattern + MACD/MA/DDE/Volume signals fire
simultaneously, and evaluates combined signal quality.
"""
from typing import List, Optional

import duckdb
import pandas as pd
import numpy as np

HOLDING_DAYS = [1, 3, 5, 10, 20]

VIEW_MAP = {
    "macd": "v_dws_macd_daily_latest",
    "ma": "v_dws_ma_daily_latest",
    "dde": "v_dws_dde_daily_latest",
    "volume": "v_dws_volume_daily_latest",
    "kpattern": "v_dws_kpattern_daily_latest",
}


def _filter_tradable_divergence(
    db_path: str,
    rows: List[dict],
    trade_date: str,
    *,
    macd_divergence: Optional[str] = None,
    dde_divergence: Optional[str] = None,
) -> List[dict]:
    """Keep rows whose tradable divergence matches the requested label."""
    if not rows or (not macd_divergence and not dde_divergence):
        return rows

    from backend.etl.divergence_tradable import enrich_tradable_columns

    df = pd.DataFrame(rows)
    con = duckdb.connect(db_path, read_only=True)
    try:
        if macd_divergence and "macd_divergence" not in df.columns:
            codes = df["ts_code"].tolist()
            macd_df = con.execute(
                """
                SELECT ts_code, divergence AS macd_divergence
                FROM v_dws_macd_daily_latest
                WHERE trade_date = ? AND ts_code IN (SELECT UNNEST(?))
                """,
                [trade_date, codes],
            ).df()
            df = df.merge(macd_df, on="ts_code", how="left")

        if dde_divergence and "dde_divergence" not in df.columns:
            codes = df["ts_code"].tolist()
            dde_df = con.execute(
                """
                SELECT ts_code, divergence AS dde_divergence
                FROM v_dws_dde_daily_latest
                WHERE trade_date = ? AND ts_code IN (SELECT UNNEST(?))
                """,
                [trade_date, codes],
            ).df()
            df = df.merge(dde_df, on="ts_code", how="left")

        df, _ = enrich_tradable_columns(df, con, freq="daily")
        if macd_divergence:
            df = df[df["macd_divergence_tradable"] == macd_divergence]
        if dde_divergence:
            df = df[df["dde_divergence_tradable"] == dde_divergence]
        return df.to_dict("records")
    finally:
        con.close()


def find_combo_signals(
    db_path: str,
    trade_date: str,
    use_tradable: bool = True,
    **kwargs,
) -> List[dict]:
    """Find stocks where specified signals co-occur on a given date.

    Parameters
    ----------
    patterns : list[str]
        K-pattern names to require (e.g. ['yang_ke_yin'])
    macd_divergence : str, optional
        When use_tradable=True (default), filters on tradable divergence label.
    macd_turning_point : str, optional
    macd_zone : str, optional
    dde_divergence : str, optional
        Same as macd_divergence for DDE.
    use_tradable : bool
        If True (default), divergence filters apply to tradable layer, not L1 structure.
    dde_trend : str, optional
    ma_alignment : str, optional
    vol_zone : str, optional

    Returns
    -------
    list[dict] with ts_code, trade_date, and matched signal columns.
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        joins = [f"FROM {VIEW_MAP['kpattern']} k"]
        conditions = ["k.trade_date = ?"]
        params = [trade_date]
        select_cols = ["k.ts_code", "k.trade_date"]

        patterns = kwargs.get("patterns", [])
        kp_cols = ["yang_bao_yin", "yang_ke_yin", "mu_bei_xian", "bi_lei_zhen",
                    "gao_kai_chang_yin", "yin_bao_yang", "yin_ke_yang"]
        for p in patterns:
            if p in kp_cols:
                conditions.append(f"k.{p} = 1")
                select_cols.append(f"'{p}' AS kpattern_type")

        # MACD join
        macd_cols = ["divergence", "turning_point", "zone"]
        macd_needed = any(kwargs.get(f"macd_{c}") for c in macd_cols)
        if macd_needed:
            joins.append(f"JOIN {VIEW_MAP['macd']} m "
                         "ON k.ts_code = m.ts_code AND k.trade_date = m.trade_date")
            for c in macd_cols:
                val = kwargs.get(f"macd_{c}")
                if val:
                    conditions.append(f"m.{c} = ?")
                    params.append(val)
                    select_cols.append(f"m.{c} AS macd_{c}")

        # DDE join
        dde_cols = ["divergence", "trend"]
        dde_needed = any(kwargs.get(f"dde_{c}") for c in dde_cols)
        if dde_needed:
            joins.append(f"JOIN {VIEW_MAP['dde']} d "
                         "ON k.ts_code = d.ts_code AND k.trade_date = d.trade_date")
            for c in dde_cols:
                val = kwargs.get(f"dde_{c}")
                if val:
                    conditions.append(f"d.{c} = ?")
                    params.append(val)
                    select_cols.append(f"d.{c} AS dde_{c}")

        # MA join
        if kwargs.get("ma_alignment"):
            joins.append(f"JOIN {VIEW_MAP['ma']} a "
                         "ON k.ts_code = a.ts_code AND k.trade_date = a.trade_date")
            conditions.append("a.alignment = ?")
            params.append(kwargs["ma_alignment"])

        # Volume join
        if kwargs.get("vol_zone"):
            joins.append(f"JOIN {VIEW_MAP['volume']} v "
                         "ON k.ts_code = v.ts_code AND k.trade_date = v.trade_date")
            conditions.append("v.zone = ?")
            params.append(kwargs["vol_zone"])

        sql = (f"SELECT DISTINCT {', '.join(select_cols)} "
               f"{' '.join(joins)} WHERE {' AND '.join(conditions)}")
        rows = con.execute(sql, params).fetchall()

        if not rows:
            return []

        cols = [d[0] for d in con.description]
        results = [dict(zip(cols, row)) for row in rows]

        if use_tradable:
            macd_div = kwargs.get("macd_divergence")
            dde_div = kwargs.get("dde_divergence")
            if macd_div or dde_div:
                results = _filter_tradable_divergence(
                    db_path,
                    results,
                    trade_date,
                    macd_divergence=macd_div,
                    dde_divergence=dde_div,
                )
        return results
    finally:
        con.close()


def count_combo_signals(
    db_path: str,
    start_date: str,
    end_date: str,
    use_tradable: bool = True,
    **kwargs,
) -> int:
    """Count resonance signals across a trade-date range (one scan, not per-day)."""
    con = duckdb.connect(db_path, read_only=True)
    try:
        joins = [f"FROM {VIEW_MAP['kpattern']} k"]
        conditions = ["k.trade_date >= ?", "k.trade_date <= ?"]
        params = [start_date, end_date]
        select_cols = ["k.ts_code", "k.trade_date"]

        patterns = kwargs.get("patterns", [])
        kp_cols = ["yang_bao_yin", "yang_ke_yin", "mu_bei_xian", "bi_lei_zhen",
                    "gao_kai_chang_yin", "yin_bao_yang", "yin_ke_yang"]
        for p in patterns:
            if p in kp_cols:
                conditions.append(f"k.{p} = 1")

        macd_cols = ["divergence", "turning_point", "zone"]
        if any(kwargs.get(f"macd_{c}") for c in macd_cols):
            joins.append(
                f"JOIN {VIEW_MAP['macd']} m "
                "ON k.ts_code = m.ts_code AND k.trade_date = m.trade_date"
            )
            for c in macd_cols:
                val = kwargs.get(f"macd_{c}")
                if val:
                    conditions.append(f"m.{c} = ?")
                    params.append(val)

        dde_cols = ["divergence", "trend"]
        if any(kwargs.get(f"dde_{c}") for c in dde_cols):
            joins.append(
                f"JOIN {VIEW_MAP['dde']} d "
                "ON k.ts_code = d.ts_code AND k.trade_date = d.trade_date"
            )
            for c in dde_cols:
                val = kwargs.get(f"dde_{c}")
                if val:
                    conditions.append(f"d.{c} = ?")
                    params.append(val)

        if kwargs.get("ma_alignment"):
            joins.append(
                f"JOIN {VIEW_MAP['ma']} a "
                "ON k.ts_code = a.ts_code AND k.trade_date = a.trade_date"
            )
            conditions.append("a.alignment = ?")
            params.append(kwargs["ma_alignment"])

        if kwargs.get("vol_zone"):
            joins.append(
                f"JOIN {VIEW_MAP['volume']} v "
                "ON k.ts_code = v.ts_code AND k.trade_date = v.trade_date"
            )
            conditions.append("v.zone = ?")
            params.append(kwargs["vol_zone"])

        macd_div = kwargs.get("macd_divergence")
        dde_div = kwargs.get("dde_divergence")
        if use_tradable and (macd_div or dde_div):
            sql = (
                f"SELECT DISTINCT {', '.join(select_cols)} "
                f"{' '.join(joins)} WHERE {' AND '.join(conditions)}"
            )
            rows = con.execute(sql, params).fetchall()
            if not rows:
                return 0
            cols = [d[0] for d in con.description]
            results = [dict(zip(cols, row)) for row in rows]
            by_date = {}
            for row in results:
                by_date.setdefault(row["trade_date"], []).append(row)
            filtered = []
            for td, chunk in by_date.items():
                filtered.extend(
                    _filter_tradable_divergence(
                        db_path,
                        chunk,
                        td,
                        macd_divergence=macd_div,
                        dde_divergence=dde_div,
                    )
                )
            return len(filtered)

        sql = (
            f"SELECT COUNT(*) FROM (SELECT DISTINCT k.ts_code, k.trade_date "
            f"{' '.join(joins)} WHERE {' AND '.join(conditions)}) sub"
        )
        return int(con.execute(sql, params).fetchone()[0] or 0)
    finally:
        con.close()


if __name__ == "__main__":
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/tradeanalysis.duckdb"

    strategies = [
        {"label": "阳克阴 + MACD金叉",
         "patterns": ["yang_ke_yin"], "macd_turning_point": "golden_cross"},
        {"label": "阳克阴 + MACD底背离",
         "patterns": ["yang_ke_yin"], "macd_divergence": "bottom_divergence"},
        {"label": "阳克阴 + DDE底背离",
         "patterns": ["yang_ke_yin"], "dde_divergence": "bottom_divergence"},
        {"label": "阳克阴 + DDE上升趋势",
         "patterns": ["yang_ke_yin"], "dde_trend": "up"},
        {"label": "墓碑线 + MACD顶背离",
         "patterns": ["mu_bei_xian"], "macd_divergence": "top_divergence"},
        {"label": "墓碑线 + DDE顶背离",
         "patterns": ["mu_bei_xian"], "dde_divergence": "top_divergence"},
        {"label": "避雷针 + MACD顶背离",
         "patterns": ["bi_lei_zhen"], "macd_divergence": "top_divergence"},
        {"label": "阴包阳(反向) + MACD金叉",
         "patterns": ["yin_bao_yang"], "macd_turning_point": "golden_cross"},
    ]

    con = duckdb.connect(db_path, read_only=True)
    end_date = con.execute(
        "SELECT MAX(trade_date) FROM dim_date WHERE is_trade_day = 1"
    ).fetchone()[0] or "20260612"
    con.close()
    start_date = "20150101"

    import time
    import sys
    print(f"Combo scan: {start_date} .. {end_date} (batch COUNT)", flush=True)
    print(f"{'Strategy':40s} {'Signals':>8s} {'Sec':>6s} {'Remark'}", flush=True)
    print("-" * 70, flush=True)
    for s in strategies:
        t0 = time.time()
        print(f"  scanning: {s['label']}...", flush=True)
        kw = {
            "patterns": s["patterns"],
            "macd_turning_point": s.get("macd_turning_point"),
            "macd_divergence": s.get("macd_divergence"),
            "dde_divergence": s.get("dde_divergence"),
            "dde_trend": s.get("dde_trend"),
        }
        kw = {k: v for k, v in kw.items() if v is not None}
        total = count_combo_signals(
            db_path, start_date, end_date, use_tradable=True, **kw,
        )
        elapsed = time.time() - t0
        remark = "insufficient data" if total < 100 else ""
        print(f"{s['label']:40s} {total:>8d} {elapsed:>6.1f}  {remark}", flush=True)
