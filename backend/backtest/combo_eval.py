"""Multi-dimension signal resonance backtesting.

Finds stocks where K-pattern + MACD/MA/DDE/Volume signals fire
simultaneously, and evaluates combined signal quality.
"""
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


def find_combo_signals(db_path: str, trade_date: str, **kwargs) -> list[dict]:
    """Find stocks where specified signals co-occur on a given date.

    Parameters
    ----------
    patterns : list[str]
        K-pattern names to require (e.g. ['yang_ke_yin'])
    macd_divergence : str, optional
    macd_turning_point : str, optional
    macd_zone : str, optional
    dde_divergence : str, optional
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
        return [dict(zip(cols, row)) for row in rows]
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
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM dim_date WHERE is_trade_day = 1 "
        "AND trade_date >= '20150101' AND trade_date <= '20260602' "
        "ORDER BY trade_date"
    ).fetchall()]
    con.close()

    import time
    print(f"{'Strategy':40s} {'Signals':>8s} {'Remark'}")
    print("-" * 60)
    for s in strategies:
        t0 = time.time()
        total = 0
        for d in dates:
            signals = find_combo_signals(db_path, d,
                                         patterns=s["patterns"],
                                         macd_turning_point=s.get("macd_turning_point"),
                                         macd_divergence=s.get("macd_divergence"),
                                         dde_divergence=s.get("dde_divergence"),
                                         dde_trend=s.get("dde_trend"))
            total += len(signals)
        elapsed = time.time() - t0
        remark = ""
        if total < 100:
            remark = "insufficient data"
        print(f"{s['label']:40s} {total:>8d}  {remark}")
