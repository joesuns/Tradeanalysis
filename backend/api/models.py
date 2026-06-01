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


class AnalysisResponse(BaseModel):
    ts_code: str
    stock_code: Optional[str] = None
    stock_name: Optional[str] = None
    trade_date: str
    freq: str
    freshness: FreshnessInfo
