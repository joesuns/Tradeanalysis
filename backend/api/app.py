"""FastAPI application with request logging middleware."""

import logging
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.api.router import router

logger = logging.getLogger(__name__)

app = FastAPI(title="TradeAnalysis API", version="1.0")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with method, path, status, and latency."""
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start) * 1000)
    logger.info(
        f"{request.method} {request.url.path} -> {response.status_code} "
        f"({duration_ms}ms)"
    )
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Log unhandled exceptions with full traceback."""
    logger.exception(
        f"Unhandled error: {request.method} {request.url.path} — {exc}"
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


app.include_router(router)
