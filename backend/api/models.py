from pydantic import BaseModel
from typing import Optional


class FreshnessInfo(BaseModel):
    age_days: int = 0
    status: str = "fresh"  # "fresh" | "stale"


class ErrorResponse(BaseModel):
    code: str
    message: str
    cause: str
    fix: str
    doc_url: Optional[str] = None


class MACDData(BaseModel):
    dif: Optional[float] = None
    dea: Optional[float] = None
    macd_bar: Optional[float] = None
    zone: Optional[str] = None
    trend: Optional[str] = None
    divergence: Optional[str] = None
    turning_point: Optional[str] = None
    alert: Optional[str] = None


class AnalysisResponse(BaseModel):
    ts_code: str
    stock_code: Optional[str] = None
    stock_name: Optional[str] = None
    trade_date: str
    freq: str
    close: Optional[float] = None
    pct_chg: Optional[float] = None
    macd: MACDData
    freshness: FreshnessInfo


class ScreeningResult(BaseModel):
    ts_code: str
    stock_code: Optional[str] = None
    stock_name: Optional[str] = None
    close: Optional[float] = None
    pct_chg: Optional[float] = None
    macd_zone: Optional[str] = None
    ma_alignment: Optional[str] = None
    ddx: Optional[float] = None


class HealthResponse(BaseModel):
    database: str
    latest_trade_date: Optional[str] = None
    freshness: FreshnessInfo
    table_stats: dict
