from fastapi import FastAPI
from backend.api.router import router

app = FastAPI(title="TradeAnalysis API", version="1.0")
app.include_router(router)
