from datetime import datetime, date

from fastapi import APIRouter, Query, HTTPException
from backend.db.connection import get_connection
from backend.api.models import (
    FreshnessInfo, ErrorResponse, AnalysisResponse, MACDData,
    ScreeningResult, HealthResponse,
)

router = APIRouter(prefix="/api/v1")

# Whitelist of allowed field names for dynamic SELECT (prevents SQL injection)
_ALLOWED_FIELDS = {
    "trade_date", "close", "open_qfq", "high_qfq", "low_qfq", "close_qfq",
    "vol", "amount", "pct_chg", "total_mv", "pe_ttm", "turnover_rate",
    "ema_12", "ema_26", "dif", "dea", "macd_bar",
    "macd_divergence", "macd_zone", "macd_turning_point", "macd_alert", "macd_trend",
    "ma_5", "ma_10", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope",
    "ma_alignment", "ma_turning_point",
    "net_mf_amount", "ddx", "ddx2", "dde_trend", "dde_alert", "dde_divergence",
    "ma_vol_5", "pct_vol_rank", "vol_zone", "vol_trend",
    "kpattern", "kpattern_strength",
}


@router.get("/health", response_model=HealthResponse)
def health():
    """Health check: DB connectivity + per-table data freshness from v_data_freshness."""
    con = get_connection(read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, latest_date FROM v_data_freshness"
        ).fetchall()

        today = date.today()
        table_stats = {}
        max_age = 0
        global_latest = None

        for table_name, latest_date in rows:
            if latest_date is None:
                age = None
            else:
                dt = datetime.strptime(latest_date, "%Y%m%d").date()
                age = (today - dt).days
            if age is not None:
                max_age = max(max_age, age)
                if global_latest is None or latest_date > global_latest:
                    global_latest = latest_date
            table_stats[table_name] = {
                "latest_trade_date": latest_date,
                "age_days": age,
                "status": "fresh" if (age is not None and age <= 1) else "stale",
            }

        return {
            "database": "connected",
            "latest_trade_date": global_latest,
            "freshness": {
                "age_days": max_age,
                "status": "fresh" if max_age <= 1 else "stale",
            },
            "table_stats": table_stats,
        }
    finally:
        con.close()


@router.get("/analysis/{ts_code}", response_model=AnalysisResponse)
def analysis(
    ts_code: str,
    freq: str = Query("daily", pattern="^(daily|weekly)$"),
):
    """Get latest MACD analysis for a single stock."""
    con = get_connection(read_only=True)
    try:
        view = f"v_dws_macd_{freq}_latest"
        row = con.execute(
            f"""
            SELECT c.trade_date, s.stock_code, s.name, q.close_qfq, q.pct_chg,
                   c.dif, c.dea, c.macd_bar, c.zone, c.trend,
                   c.divergence, c.turning_point, c.alert
            FROM {view} c
            JOIN dim_stock s ON c.ts_code = s.ts_code
            JOIN dwd_daily_quote q ON c.ts_code = q.ts_code
                AND c.trade_date = q.trade_date
            WHERE c.ts_code = ?
            ORDER BY c.trade_date DESC LIMIT 1
            """,
            (ts_code,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "STOCK_NOT_FOUND",
                    "message": f"股票代码 '{ts_code}' 不存在",
                    "cause": "ts_code 格式错误或该股票已退市",
                    "fix": "请使用 tushare 标准代码格式（如 000001.SZ）",
                },
            )
        return AnalysisResponse(
            ts_code=ts_code,
            stock_code=row[1],
            stock_name=row[2],
            trade_date=row[0],
            freq=freq,
            close=row[3],
            pct_chg=row[4],
            macd=MACDData(
                dif=row[5], dea=row[6], macd_bar=row[7],
                zone=row[8], trend=row[9], divergence=row[10],
                turning_point=row[11], alert=row[12],
            ),
            freshness=FreshnessInfo(age_days=0, status="fresh"),
        )
    finally:
        con.close()


@router.get("/analysis/{ts_code}/history")
def analysis_history(
    ts_code: str,
    freq: str = Query("daily", pattern="^(daily|weekly)$"),
    fields: str = Query("trade_date,dif,dea,macd_bar"),
    from_date: str = Query(None, alias="from"),
    to: str = Query(None),
):
    """Get historical MACD analysis for a single stock with field selection and date filtering."""
    con = get_connection(read_only=True)
    try:
        view = f"v_dws_macd_{freq}_latest"

        # Parse and validate requested fields
        requested = [f.strip() for f in fields.split(",") if f.strip() in _ALLOWED_FIELDS]
        if not requested:
            requested = ["trade_date", "close", "dif", "dea", "macd_bar"]

        # Ensure trade_date is always first for ordering
        if "trade_date" not in requested:
            requested = ["trade_date"] + requested
        select_clause = ", ".join(requested)

        # Build query with optional date filters
        query = f"SELECT {select_clause} FROM {view} WHERE ts_code = ?"
        params = [ts_code]
        if from_date:
            query += " AND trade_date >= ?"
            params.append(from_date)
        if to:
            query += " AND trade_date <= ?"
            params.append(to)
        query += " ORDER BY trade_date"

        rows = con.execute(query, params).fetchall()
        cols = [d[0] for d in con.description]

        # Build row dicts keyed by field name
        data = []
        for row in rows:
            data.append({cols[j]: row[j] for j in range(len(cols))})

        return {
            "ts_code": ts_code,
            "freq": freq,
            "fields": requested,
            "count": len(data),
            "rows": data,
            "freshness": {"age_days": 0, "status": "fresh"},
        }
    finally:
        con.close()


@router.get("/screening")
def screening(
    freq: str = Query("daily", pattern="^(daily|weekly)$"),
    macd_zone: str = Query(None),
    ma_alignment: str = Query(None),
    min_ddx: float = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """Screen stocks by technical indicator conditions."""
    con = get_connection(read_only=True)
    try:
        macd_view = f"v_dws_macd_{freq}_latest"
        ma_view = f"v_dws_ma_{freq}_latest"
        dde_view = f"v_dws_dde_{freq}_latest"
        quote_table = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"

        query = f"""
            SELECT q.ts_code, s.stock_code, s.name, q.close_qfq, q.pct_chg,
                   m.zone AS macd_zone, ma.alignment AS ma_alignment, d.ddx
            FROM {quote_table} q
            JOIN dim_stock s ON q.ts_code = s.ts_code
            JOIN {macd_view} m ON q.ts_code = m.ts_code AND q.trade_date = m.trade_date
            JOIN {ma_view} ma ON q.ts_code = ma.ts_code AND q.trade_date = ma.trade_date
            JOIN {dde_view} d ON q.ts_code = d.ts_code AND q.trade_date = d.trade_date
            WHERE q.trade_date = (SELECT MAX(trade_date) FROM {quote_table})
              AND q.is_suspended = 0
        """
        conditions = []
        params = []

        if macd_zone:
            conditions.append("m.zone = ?")
            params.append(macd_zone)
        if ma_alignment:
            conditions.append("ma.alignment = ?")
            params.append(ma_alignment)
        if min_ddx is not None:
            conditions.append("d.ddx >= ?")
            params.append(min_ddx)

        if conditions:
            query += " AND " + " AND ".join(conditions)

        query += " ORDER BY d.ddx DESC LIMIT ?"
        params.append(limit)

        rows = con.execute(query, params).fetchall()
        results = []
        for row in rows:
            results.append({
                "ts_code": row[0],
                "stock_code": row[1],
                "stock_name": row[2],
                "close": row[3],
                "pct_chg": row[4],
                "macd_zone": row[5],
                "ma_alignment": row[6],
                "ddx": row[7],
            })

        return {
            "conditions": {
                "freq": freq,
                "macd_zone": macd_zone,
                "ma_alignment": ma_alignment,
                "min_ddx": min_ddx,
            },
            "count": len(results),
            "results": results,
            "freshness": {"age_days": 0, "status": "fresh"},
        }
    finally:
        con.close()
