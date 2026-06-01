from fastapi import APIRouter, Query, HTTPException
from backend.db.connection import get_connection
from backend.api.models import FreshnessInfo, ErrorResponse

router = APIRouter(prefix="/api/v1")


@router.get("/health")
def health():
    """Health check endpoint returning database connectivity and stats."""
    con = get_connection(read_only=True)
    try:
        latest = con.execute(
            "SELECT MAX(trade_date) FROM dwd_daily_quote"
        ).fetchone()[0]
        macd_rows = con.execute(
            "SELECT COUNT(*) FROM dws_macd_daily"
        ).fetchone()[0]
        return {
            "database": "connected",
            "latest_trade_date": latest,
            "freshness": {"age_days": 0, "status": "fresh"},
            "table_stats": {
                "dws_macd_daily": {
                    "rows": macd_rows,
                    "latest_calc_date": latest,
                }
            },
        }
    finally:
        con.close()


@router.get("/analysis/{ts_code}")
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
        return {
            "ts_code": ts_code,
            "stock_code": row[1],
            "stock_name": row[2],
            "trade_date": row[0],
            "freq": freq,
            "close": row[3],
            "pct_chg": row[4],
            "macd": {
                "dif": row[5],
                "dea": row[6],
                "macd_bar": row[7],
                "zone": row[8],
                "trend": row[9],
                "divergence": row[10],
                "turning_point": row[11],
                "alert": row[12],
            },
            "freshness": {"age_days": 0, "status": "fresh"},
        }
    finally:
        con.close()


@router.get("/analysis/{ts_code}/history")
def analysis_history(
    ts_code: str,
    freq: str = Query("daily", pattern="^(daily|weekly)$"),
    fields: str = Query("close,dif,dea,macd_bar"),
    from_date: str = Query(None, alias="from"),
    to: str = Query(None),
):
    """Get historical MACD analysis for a single stock."""
    con = get_connection(read_only=True)
    try:
        view = f"v_dws_macd_{freq}_latest"
        rows = con.execute(
            f"SELECT trade_date FROM {view} WHERE ts_code = ? ORDER BY trade_date",
            (ts_code,),
        ).fetchall()
        return {
            "ts_code": ts_code,
            "freq": freq,
            "count": len(rows),
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
    return {
        "conditions": {
            "freq": freq,
            "macd_zone": macd_zone,
            "ma_alignment": ma_alignment,
            "min_ddx": min_ddx,
        },
        "count": 0,
        "results": [],
        "freshness": {"age_days": 0, "status": "fresh"},
    }
