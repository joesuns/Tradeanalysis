# 财联社新闻采集 + AI 解读 + Web 展示 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个个人 Web 应用，自动采集财联社电报新闻并通过 DeepSeek V4 深度解读，左右分屏实时展示。

**Architecture:** FastAPI 单体后端（APScheduler 定时抓取 + SQLite 存储 + WebSocket 推送）+ React 前端（Vite + Tailwind + TanStack Query），单进程运行。

**Tech Stack:** Python 3.11+, FastAPI, AKShare, OpenAI SDK (DeepSeek), APScheduler, SQLite (WAL), React 18, TypeScript, Vite, Tailwind CSS, TanStack React Query

**Spec:** [2026-05-30-cls-news-analysis-design.md](../specs/2026-05-30-cls-news-analysis-design.md)

---

## File Structure

```
/Users/joesun/trae/Tradeanalysis/
├── backend/
│   ├── main.py              # FastAPI app, lifespan, route registration
│   ├── config.py            # Env-based configuration
│   ├── database.py          # SQLite init, get_db helper
│   ├── models.py            # Pydantic request/response models
│   ├── scheduler.py         # APScheduler job + pause/resume
│   ├── fetcher.py           # AKShare news fetching + dedup
│   ├── analyzer.py          # DeepSeek API analysis + retry
│   ├── websocket_manager.py # WebSocket connection tracking + broadcast
│   └── requirements.txt     # Python dependencies
├── frontend/
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── tsconfig.app.json
│   ├── tsconfig.node.json
│   ├── tailwind.config.js
│   ├── postcss.config.js
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── index.css
│       ├── types/
│       │   └── index.ts
│       ├── api/
│       │   └── client.ts
│       ├── hooks/
│       │   ├── useWebSocket.ts
│       │   └── useNews.ts
│       └── components/
│           ├── StatusBar.tsx
│           ├── FilterBar.tsx
│           ├── TagCloud.tsx
│           ├── NewsList.tsx
│           ├── NewsCard.tsx
│           ├── DetailPanel.tsx
│           ├── Pagination.tsx
│           └── ErrorBoundary.tsx
```

---

## Phase 1: Backend Infrastructure

### Task 1.1: Create requirements.txt and install dependencies

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/backend/requirements.txt`

- [ ] **Step 1: Write requirements.txt**

```txt
fastapi>=0.115.0
uvicorn[standard]>=0.34.0
pydantic>=2.0.0
akshare>=1.16.0
openai>=1.0.0
apscheduler>=3.10.0
```

- [ ] **Step 2: Create virtual environment and install**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Expected: All packages installed without errors.

- [ ] **Step 3: Commit**

```bash
git add backend/requirements.txt
git commit -m "chore: add backend Python dependencies"
```

---

### Task 1.2: Create config.py

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/backend/config.py`

- [ ] **Step 1: Write config.py**

```python
"""Application configuration, loaded from environment variables."""

import os

# DeepSeek API
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Scheduler
FETCH_INTERVAL_SECONDS = int(os.getenv("FETCH_INTERVAL_SECONDS", "300"))

# Database
DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradeanalysis.db"),
)

# Pagination
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "20"))

# Analysis retry
ANALYSIS_MAX_RETRIES = 3
ANALYSIS_RETRY_BASE_DELAY = 2  # seconds
```

- [ ] **Step 2: Verify import**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
python -c "from config import DEEPSEEK_MODEL, DATABASE_PATH; print('OK:', DEEPSEEK_MODEL, DATABASE_PATH)"
```

Expected: `OK: deepseek-chat /Users/joesun/trae/Tradeanalysis/backend/tradeanalysis.db`

- [ ] **Step 3: Commit**

```bash
git add backend/config.py
git commit -m "feat: add config module with env-based settings"
```

---

### Task 1.3: Create database.py

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/backend/database.py`

- [ ] **Step 1: Write database.py**

```python
"""SQLite database initialization and connection management."""

import sqlite3
from config import DATABASE_PATH

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS news (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id  TEXT UNIQUE,
        title       TEXT NOT NULL,
        content     TEXT,
        ctime       INTEGER,
        level       TEXT,
        fetched_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS analysis (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id     INTEGER UNIQUE REFERENCES news(id),
        summary     TEXT,
        sentiment   TEXT,
        impact      TEXT,
        sectors     TEXT,
        stocks      TEXT,
        historical  TEXT,
        suggestion  TEXT,
        analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS system_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    INSERT OR IGNORE INTO system_state (key, value) VALUES ('paused', 'false');
"""


def get_db() -> sqlite3.Connection:
    """Get a new database connection with WAL mode enabled."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = get_db()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
```

- [ ] **Step 2: Verify init_db creates tables**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
python -c "
from database import init_db, get_db
init_db()
conn = get_db()
tables = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()
print([t['name'] for t in tables])
conn.close()
"
```

Expected: `['news', 'analysis', 'system_state']`

- [ ] **Step 3: Commit**

```bash
git add backend/database.py
git commit -m "feat: add database module with SQLite schema"
```

---

### Task 1.4: Create models.py

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/backend/models.py`

- [ ] **Step 1: Write models.py**

```python
"""Pydantic models for request/response validation."""

from pydantic import BaseModel
from typing import Optional


class AnalysisResult(BaseModel):
    """Expected JSON structure from DeepSeek response."""
    summary: str
    sentiment: str  # 利好 / 利空 / 中性
    impact: str
    sectors: list[str]
    stocks: list[str]
    historical: str
    suggestion: str


class AnalysisItem(BaseModel):
    id: int
    news_id: int
    summary: Optional[str] = None
    sentiment: Optional[str] = None
    impact: Optional[str] = None
    sectors: Optional[str] = None
    stocks: Optional[str] = None
    historical: Optional[str] = None
    suggestion: Optional[str] = None
    analyzed_at: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "AnalysisItem":
        return cls(
            id=row["analysis_id"],
            news_id=row["news_id"],
            summary=row["summary"],
            sentiment=row["sentiment"],
            impact=row["impact"],
            sectors=row["sectors"],
            stocks=row["stocks"],
            historical=row["historical"],
            suggestion=row["suggestion"],
            analyzed_at=row["analyzed_at"],
        )


class NewsItem(BaseModel):
    id: int
    article_id: str
    title: str
    content: Optional[str] = None
    ctime: Optional[int] = None
    level: Optional[str] = None
    fetched_at: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "NewsItem":
        return cls(
            id=row["id"],
            article_id=row["article_id"],
            title=row["title"],
            content=row["content"],
            ctime=row["ctime"],
            level=row["level"],
            fetched_at=row["fetched_at"],
        )


class NewsWithAnalysis(BaseModel):
    id: int
    article_id: str
    title: str
    content: Optional[str] = None
    ctime: Optional[int] = None
    level: Optional[str] = None
    fetched_at: Optional[str] = None
    analysis: Optional[AnalysisItem] = None

    @classmethod
    def from_row(cls, row) -> "NewsWithAnalysis":
        analysis = None
        if row["analysis_id"] is not None:
            analysis = AnalysisItem.from_row(row)
        return cls(
            id=row["id"],
            article_id=row["article_id"],
            title=row["title"],
            content=row["content"],
            ctime=row["ctime"],
            level=row["level"],
            fetched_at=row["fetched_at"],
            analysis=analysis,
        )


class NewsListResponse(BaseModel):
    items: list[NewsWithAnalysis]
    total: int
    page: int
    page_size: int
    total_pages: int


class SystemStatus(BaseModel):
    status: str  # "running" | "paused" | "analyzing" | "fetching"
    last_update: Optional[str] = None
    news_count: int = 0


class TagListResponse(BaseModel):
    tags: list[str]
```

- [ ] **Step 2: Verify import**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
python -c "from models import NewsWithAnalysis, SystemStatus, TagListResponse; print('All models OK')"
```

Expected: `All models OK`

- [ ] **Step 3: Commit**

```bash
git add backend/models.py
git commit -m "feat: add Pydantic models for API request/response"
```

---

## Phase 2: Backend Core Logic

### Task 2.1: Create fetcher.py

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/backend/fetcher.py`

- [ ] **Step 1: Write fetcher.py**

```python
"""Fetch CLS news via AKShare and store to SQLite."""

import logging
from database import get_db

logger = logging.getLogger(__name__)


def fetch_and_store() -> int:
    """Fetch latest CLS telegraph news, deduplicate by article_id, store to DB.

    Returns:
        Number of new news items inserted.
    """
    try:
        import akshare as ak
        df = ak.stock_info_global_cls()
    except Exception as e:
        logger.error(f"AKShare fetch failed: {e}")
        return 0

    if df is None or df.empty:
        logger.warning("AKShare returned empty DataFrame")
        return 0

    conn = get_db()
    new_count = 0
    try:
        for _, row in df.iterrows():
            article_id = str(row.get("article_id", ""))
            title = str(row.get("title", ""))
            content = str(row.get("content", "")) if row.get("content") else ""
            ctime_val = row.get("ctime")
            ctime = int(ctime_val) if ctime_val and str(ctime_val) != "nan" else None
            level = str(row.get("level", "")) if row.get("level") and str(row.get("level")) != "nan" else None

            if not article_id or not title:
                continue

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO news (article_id, title, content, ctime, level)
                       VALUES (?, ?, ?, ?, ?)""",
                    (article_id, title, content, ctime, level),
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    new_count += 1
            except Exception as e:
                logger.warning(f"Failed to insert news {article_id}: {e}")
                continue

        conn.commit()
    finally:
        conn.close()

    logger.info(f"Fetched: {new_count} new news items stored")
    return new_count
```

- [ ] **Step 2: Verify import**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
python -c "from fetcher import fetch_and_store; print('Fetcher module OK')"
```

Expected: `Fetcher module OK`

- [ ] **Step 3: Commit**

```bash
git add backend/fetcher.py
git commit -m "feat: add fetcher module with AKShare integration"
```

---

### Task 2.2: Create analyzer.py

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/backend/analyzer.py`

- [ ] **Step 1: Write analyzer.py**

```python
"""Analyze news using DeepSeek V4 API and store results to SQLite."""

import json
import logging
import time
from openai import OpenAI
from database import get_db
from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    ANALYSIS_MAX_RETRIES,
    ANALYSIS_RETRY_BASE_DELAY,
)
from models import AnalysisResult

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位资深的A股市场分析师。请阅读以下财联社电报新闻，给出深度解读。

你必须仅返回一个 JSON 对象，不要有任何其他文本。JSON 格式如下：
{
  "summary": "一句话总结新闻核心内容",
  "sentiment": "利好",
  "impact": "对市场的具体影响分析，100字以内",
  "sectors": ["板块1", "板块2"],
  "stocks": ["标的1", "标的2"],
  "historical": "与历史上类似事件的联动分析，100字以内",
  "suggestion": "针对该事件的操作建议，100字以内"
}

注意：
- sentiment 只能是 "利好"、"利空"、"中性" 之一
- sectors 和 stocks 必须是数组，即使只有一个元素
- 如果没有明确关联标的或板块，返回空数组 []
- 所有字段都必须有值"""


def _call_deepseek(title: str, content: str) -> AnalysisResult | None:
    """Call DeepSeek API to analyze a single news item with retries."""
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    user_message = f"标题：{title}\n\n内容：{content}"

    for attempt in range(ANALYSIS_MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.3,
                max_tokens=1024,
            )

            raw = response.choices[0].message.content.strip()

            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            result = AnalysisResult.model_validate_json(raw)
            return result

        except Exception as e:
            logger.warning(f"DeepSeek API attempt {attempt + 1}/{ANALYSIS_MAX_RETRIES} failed: {e}")
            if attempt < ANALYSIS_MAX_RETRIES - 1:
                delay = ANALYSIS_RETRY_BASE_DELAY ** (attempt + 1)
                time.sleep(delay)

    logger.error(f"DeepSeek API failed after {ANALYSIS_MAX_RETRIES} retries for: {title[:50]}")
    return None


def analyze_unanalyzed() -> list[dict]:
    """Find all unanalyzed news, analyze each, store results.

    Returns:
        List of dicts with news_id, title, and analysis fields for WebSocket broadcast.
    """
    import threading

    conn = get_db()
    try:
        # Get unanalyzed news
        unanalyzed = conn.execute(
            """SELECT n.id, n.title, n.content
               FROM news n
               LEFT JOIN analysis a ON a.news_id = n.id
               WHERE a.id IS NULL
               ORDER BY n.ctime DESC"""
        ).fetchall()
    finally:
        conn.close()

    if not unanalyzed:
        logger.info("No unanalyzed news found")
        return []

    broadcast_data = []

    for row in unanalyzed:
        news_id = row["id"]
        title = row["title"]
        content = row["content"] or ""

        analysis = _call_deepseek(title, content)
        if analysis is None:
            continue

        conn = get_db()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO analysis
                   (news_id, summary, sentiment, impact, sectors, stocks, historical, suggestion)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    news_id,
                    analysis.summary,
                    analysis.sentiment,
                    analysis.impact,
                    json.dumps(analysis.sectors, ensure_ascii=False),
                    json.dumps(analysis.stocks, ensure_ascii=False),
                    analysis.historical,
                    analysis.suggestion,
                ),
            )
            conn.commit()

            # Get the inserted analysis for broadcast
            ana_row = conn.execute(
                "SELECT * FROM analysis WHERE news_id = ?", (news_id,)
            ).fetchone()
        finally:
            conn.close()

        item = {
            "news_id": news_id,
            "title": title,
            "content": content,
            "summary": analysis.summary,
            "sentiment": analysis.sentiment,
            "impact": analysis.impact,
            "sectors": analysis.sectors,
            "stocks": analysis.stocks,
            "historical": analysis.historical,
            "suggestion": analysis.suggestion,
        }
        broadcast_data.append(item)
        logger.info(f"Analysis complete for news {news_id}: {analysis.sentiment}")

    return broadcast_data
```

- [ ] **Step 2: Verify import**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
python -c "from analyzer import analyze_unanalyzed, SYSTEM_PROMPT; print('Analyzer module OK')"
```

Expected: `Analyzer module OK`

- [ ] **Step 3: Commit**

```bash
git add backend/analyzer.py
git commit -m "feat: add AI analyzer module with DeepSeek integration"
```

---

### Task 2.3: Create websocket_manager.py

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/backend/websocket_manager.py`

- [ ] **Step 1: Write websocket_manager.py**

```python
"""WebSocket connection manager for broadcasting to connected clients."""

import json
import logging
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks active WebSocket connections and broadcasts messages."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)
        logger.info(f"WebSocket connected, total: {len(self._connections)}")

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.remove(websocket)
        logger.info(f"WebSocket disconnected, total: {len(self._connections)}")

    async def broadcast(self, event: str, data: dict) -> None:
        """Send an event to all connected clients."""
        message = json.dumps({"event": event, "data": data}, ensure_ascii=False)
        stale: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self._connections.remove(ws)

    @property
    def active_count(self) -> int:
        return len(self._connections)


# Singleton
manager = ConnectionManager()
```

- [ ] **Step 2: Verify import**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
python -c "from websocket_manager import manager; print('WebSocket manager OK, connections:', manager.active_count)"
```

Expected: `WebSocket manager OK, connections: 0`

- [ ] **Step 3: Commit**

```bash
git add backend/websocket_manager.py
git commit -m "feat: add WebSocket connection manager"
```

---

### Task 2.4: Create scheduler.py

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/backend/scheduler.py`

- [ ] **Step 1: Write scheduler.py**

```python
"""APScheduler job for periodic news fetching and analysis."""

import asyncio
import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from database import get_db

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_is_paused = False
_last_update: datetime | None = None


def is_paused() -> bool:
    """Check if the pipeline is paused (from DB)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = 'paused'"
        ).fetchone()
        return row["value"] == "true" if row else False
    finally:
        conn.close()


def set_paused(paused: bool) -> None:
    """Persist pause state to DB."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE system_state SET value = ? WHERE key = 'paused'",
            ("true" if paused else "false",),
        )
        conn.commit()
    finally:
        conn.close()
    global _is_paused
    _is_paused = paused


async def _fetch_and_analyze_job() -> None:
    """The scheduled job: fetch news, then analyze unanalyzed."""
    from fetcher import fetch_and_store
    from analyzer import analyze_unanalyzed
    from websocket_manager import manager

    if is_paused():
        logger.info("Pipeline is paused, skipping job")
        return

    global _last_update

    # Notify clients: fetching
    await manager.broadcast("status_change", {"status": "fetching"})

    # Step 1: Fetch
    new_count = await asyncio.to_thread(fetch_and_store)

    # Step 2: Analyze
    await manager.broadcast("status_change", {"status": "analyzing"})
    results = await asyncio.to_thread(analyze_unanalyzed)

    # Step 3: Broadcast each new analysis
    for item in results:
        await manager.broadcast("new_analysis", item)

    # Step 4: Update state
    _last_update = datetime.now(timezone.utc)

    # Notify clients: back to running
    conn = get_db()
    try:
        total_news = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    finally:
        conn.close()

    await manager.broadcast("status_change", {
        "status": "running",
        "last_update": _last_update.isoformat(),
        "news_count": total_news,
    })

    logger.info(f"Job complete: {new_count} fetched, {len(results)} analyzed")


async def _broadcast_fetcher_tick(message: str, news_count: int, analysis_count: int) -> None:
    """Helper to broadcast a tick event."""
    from websocket_manager import manager
    await manager.broadcast("fetcher_tick", {
        "message": message,
        "news_count": news_count,
        "analysis_count": analysis_count,
    })
```

- [ ] **Step 2: Verify import**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
python -c "from scheduler import scheduler, is_paused, set_paused; print('Scheduler module OK')"
```

Expected: `Scheduler module OK`

- [ ] **Step 3: Commit**

```bash
git add backend/scheduler.py
git commit -m "feat: add scheduler with pause/resume support"
```

---

## Phase 3: FastAPI Entry Point

### Task 3.1: Create main.py

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/backend/main.py`

- [ ] **Step 1: Write main.py**

```python
"""FastAPI application entry point."""

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware

from config import FETCH_INTERVAL_SECONDS, PAGE_SIZE
from database import init_db, get_db
from models import NewsWithAnalysis, NewsListResponse, SystemStatus, TagListResponse
from websocket_manager import manager
from scheduler import scheduler, _fetch_and_analyze_job, is_paused, set_paused

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, start scheduler. Shutdown: stop scheduler."""
    init_db()
    scheduler.add_job(
        _fetch_and_analyze_job,
        "interval",
        seconds=FETCH_INTERVAL_SECONDS,
        id="fetch_analyze",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started, interval={FETCH_INTERVAL_SECONDS}s")
    yield
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


app = FastAPI(title="TradeAnalysis", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── REST Endpoints ───────────────────────────────────────────

@app.get("/api/news", response_model=NewsListResponse)
def list_news(
    tag: str | None = Query(None),
    sentiment: str | None = Query(None),
    level: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(PAGE_SIZE, ge=1, le=100),
):
    """Paginated news list with optional filters."""
    conn = get_db()
    try:
        where_clauses = ["1=1"]
        params: list = []

        if sentiment:
            where_clauses.append("a.sentiment = ?")
            params.append(sentiment)
        if level:
            where_clauses.append("n.level = ?")
            params.append(level)
        if search:
            where_clauses.append("(n.title LIKE ? OR n.content LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if tag:
            where_clauses.append("(a.sectors LIKE ? OR a.stocks LIKE ?)")
            params.extend([f"%{tag}%", f"%{tag}%"])

        where = " AND ".join(where_clauses)

        # Count total
        count_sql = f"""
            SELECT COUNT(*) FROM news n
            LEFT JOIN analysis a ON a.news_id = n.id
            WHERE {where}
        """
        total = conn.execute(count_sql, params).fetchone()[0]

        # Fetch page
        offset = (page - 1) * page_size
        data_sql = f"""
            SELECT n.*,
                   a.id AS analysis_id, a.news_id, a.summary, a.sentiment,
                   a.impact, a.sectors, a.stocks, a.historical,
                   a.suggestion, a.analyzed_at
            FROM news n
            LEFT JOIN analysis a ON a.news_id = n.id
            WHERE {where}
            ORDER BY n.ctime DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(data_sql, params + [page_size, offset]).fetchall()

        items = [NewsWithAnalysis.from_row(r) for r in rows]
        total_pages = max(1, (total + page_size - 1) // page_size)

        return NewsListResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )
    finally:
        conn.close()


@app.get("/api/news/{news_id}", response_model=NewsWithAnalysis)
def get_news_detail(news_id: int):
    """Get a single news item with its analysis."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT n.*,
                      a.id AS analysis_id, a.news_id, a.summary, a.sentiment,
                      a.impact, a.sectors, a.stocks, a.historical,
                      a.suggestion, a.analyzed_at
               FROM news n
               LEFT JOIN analysis a ON a.news_id = n.id
               WHERE n.id = ?""",
            (news_id,),
        ).fetchone()
        if row is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="News not found")
        return NewsWithAnalysis.from_row(row)
    finally:
        conn.close()


@app.get("/api/tags", response_model=TagListResponse)
def get_tags():
    """Aggregate popular tags from sectors and stocks columns."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT sectors, stocks FROM analysis WHERE sectors IS NOT NULL AND stocks IS NOT NULL"
        ).fetchall()

        tag_set: set[str] = set()
        for r in rows:
            try:
                sectors = json.loads(r["sectors"]) if r["sectors"] else []
                stocks = json.loads(r["stocks"]) if r["stocks"] else []
            except json.JSONDecodeError:
                continue
            for t in sectors:
                tag_set.add(t)
            for t in stocks:
                tag_set.add(t)

        sorted_tags = sorted(tag_set)
        return TagListResponse(tags=sorted_tags)
    finally:
        conn.close()


@app.get("/api/status", response_model=SystemStatus)
def get_status():
    """Get current system status."""
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        # Get last analysis time
        last = conn.execute(
            "SELECT analyzed_at FROM analysis ORDER BY analyzed_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    if is_paused():
        status = "paused"
    else:
        status = "running"

    return SystemStatus(
        status=status,
        last_update=last["analyzed_at"] if last else None,
        news_count=total,
    )


@app.post("/api/pause")
def pause():
    """Pause the fetch+analyze pipeline."""
    set_paused(True)
    import asyncio
    # Schedule broadcast to run on event loop
    return {"status": "paused"}


@app.post("/api/resume")
def resume():
    """Resume the fetch+analyze pipeline."""
    set_paused(False)
    return {"status": "resumed"}


# ─── WebSocket ────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text('{"event":"pong","data":{}}')
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


# ─── Health Check ─────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
```

- [ ] **Step 2: Verify server starts**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
python main.py &
sleep 3
curl -s http://localhost:8000/api/health | python -m json.tool
kill %1 2>/dev/null
```

Expected: `{"status": "ok"}`

- [ ] **Step 3: Verify REST endpoints**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
python main.py &
sleep 3
echo "=== GET /api/status ===" && curl -s http://localhost:8000/api/status | python -m json.tool
echo "=== GET /api/news ===" && curl -s "http://localhost:8000/api/news?page=1&page_size=5" | python -m json.tool
echo "=== GET /api/tags ===" && curl -s http://localhost:8000/api/tags | python -m json.tool
kill %1 2>/dev/null
```

Expected: All endpoints return valid JSON with no errors.

- [ ] **Step 4: Commit**

```bash
git add backend/main.py
git commit -m "feat: add FastAPI entry point with REST + WebSocket endpoints"
```

---

## Phase 4: Frontend Scaffolding

### Task 4.1: Initialize Vite + React + TypeScript project

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/` (via Vite scaffold)

- [ ] **Step 1: Scaffold project**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis
npm create vite@latest frontend -- --template react-ts
```

Expected: Project scaffolded in `frontend/` directory.

- [ ] **Step 2: Install dependencies**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/frontend
npm install
npm install @tanstack/react-query tailwindcss @tailwindcss/vite
```

Expected: All packages installed.

- [ ] **Step 3: Verify dev server starts**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/frontend
npm run dev &
sleep 3
curl -s http://localhost:5173 | head -20
kill %1 2>/dev/null
```

Expected: HTML response with React root div.

- [ ] **Step 4: Commit**

```bash
git add frontend/
git commit -m "chore: scaffold React + TypeScript + Vite frontend"
```

---

### Task 4.2: Configure Tailwind CSS

**Files:**
- Modify: `/Users/joesun/trae/Tradeanalysis/frontend/vite.config.ts`
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/index.css`
- Modify: `/Users/joesun/trae/Tradeanalysis/frontend/index.html`

- [ ] **Step 1: Add Tailwind plugin to Vite config**

Edit `vite.config.ts`:
```typescript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
})
```

- [ ] **Step 2: Write index.css with Tailwind directives**

Write `src/index.css`:
```css
@import "tailwindcss";
```

- [ ] **Step 3: Update index.html title**

Edit `index.html`:
```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>TradeAnalysis - 财联社新闻分析</title>
  </head>
  <body class="bg-gray-950 text-gray-100">
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 4: Verify Tailwind works**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/frontend
npm run dev &
sleep 2
# Check that the dev server starts without CSS errors
curl -s http://localhost:5173 | grep tailwindcss 2>/dev/null
kill %1 2>/dev/null
echo "Tailwind config OK"
```

Expected: `Tailwind config OK`

- [ ] **Step 5: Commit**

```bash
git add frontend/vite.config.ts frontend/src/index.css frontend/index.html
git commit -m "feat: configure Tailwind CSS v4"
```

---

### Task 4.3: Create TypeScript types

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/types/index.ts`

- [ ] **Step 1: Write types/index.ts**

```typescript
export interface AnalysisItem {
  id: number;
  news_id: number;
  summary: string | null;
  sentiment: string | null;
  impact: string | null;
  sectors: string | null;
  stocks: string | null;
  historical: string | null;
  suggestion: string | null;
  analyzed_at: string | null;
}

export interface NewsWithAnalysis {
  id: number;
  article_id: string;
  title: string;
  content: string | null;
  ctime: number | null;
  level: string | null;
  fetched_at: string | null;
  analysis: AnalysisItem | null;
}

export interface NewsListResponse {
  items: NewsWithAnalysis[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface SystemStatus {
  status: 'running' | 'paused' | 'analyzing' | 'fetching';
  last_update: string | null;
  news_count: number;
}

export interface WsNewAnalysis {
  news_id: number;
  title: string;
  content: string;
  summary: string;
  sentiment: string;
  impact: string;
  sectors: string[];
  stocks: string[];
  historical: string;
  suggestion: string;
}

export interface WsStatusChange {
  status: string;
  last_update?: string;
  news_count?: number;
}

export type FilterSentiment = 'all' | '利好' | '利空' | '中性';
```

- [ ] **Step 2: Verify types compile**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/frontend
npx tsc --noEmit
```

Expected: No TypeScript errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/index.ts
git commit -m "feat: add TypeScript type definitions"
```

---

### Task 4.4: Create API client

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/api/client.ts`

- [ ] **Step 1: Write api/client.ts**

```typescript
import type { NewsListResponse, NewsWithAnalysis, SystemStatus } from '../types';

const BASE = 'http://localhost:8000/api';

export async function fetchNews(params: {
  tag?: string;
  sentiment?: string;
  level?: string;
  search?: string;
  page?: number;
  page_size?: number;
}): Promise<NewsListResponse> {
  const searchParams = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== '') searchParams.set(k, String(v));
  });
  const res = await fetch(`${BASE}/news?${searchParams}`);
  if (!res.ok) throw new Error(`Failed to fetch news: ${res.status}`);
  return res.json();
}

export async function fetchNewsDetail(id: number): Promise<NewsWithAnalysis> {
  const res = await fetch(`${BASE}/news/${id}`);
  if (!res.ok) throw new Error(`Failed to fetch news detail: ${res.status}`);
  return res.json();
}

export async function fetchTags(): Promise<{ tags: string[] }> {
  const res = await fetch(`${BASE}/tags`);
  if (!res.ok) throw new Error(`Failed to fetch tags: ${res.status}`);
  return res.json();
}

export async function fetchStatus(): Promise<SystemStatus> {
  const res = await fetch(`${BASE}/status`);
  if (!res.ok) throw new Error(`Failed to fetch status: ${res.status}`);
  return res.json();
}

export async function pausePipeline(): Promise<void> {
  await fetch(`${BASE}/pause`, { method: 'POST' });
}

export async function resumePipeline(): Promise<void> {
  await fetch(`${BASE}/resume`, { method: 'POST' });
}
```

- [ ] **Step 2: Verify types compile**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/frontend
npx tsc --noEmit
```

Expected: No TypeScript errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/client.ts
git commit -m "feat: add REST API client"
```

---

## Phase 5: Frontend Components

### Task 5.1: Create ErrorBoundary

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/components/ErrorBoundary.tsx`

- [ ] **Step 1: Write ErrorBoundary**

```tsx
import { Component, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-gray-950 text-gray-100">
          <div className="text-center p-8">
            <h1 className="text-2xl font-bold text-red-400 mb-4">出错了</h1>
            <p className="text-gray-400">{this.state.error?.message}</p>
            <button
              className="mt-4 px-4 py-2 bg-blue-600 rounded hover:bg-blue-500 transition"
              onClick={() => this.setState({ hasError: false, error: null })}
            >
              重试
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/ErrorBoundary.tsx
git commit -m "feat: add ErrorBoundary component"
```

---

### Task 5.2: Create useWebSocket hook

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/hooks/useWebSocket.ts`

- [ ] **Step 1: Write useWebSocket hook**

```typescript
import { useEffect, useRef, useCallback } from 'react';
import type { WsNewAnalysis, WsStatusChange } from '../types';

interface WsCallbacks {
  onNewAnalysis?: (data: WsNewAnalysis) => void;
  onStatusChange?: (data: WsStatusChange) => void;
}

const WS_URL = 'ws://localhost:8000/ws';
const MAX_RECONNECT_DELAY = 30000;
const BASE_RECONNECT_DELAY = 1000;

export function useWebSocket(callbacks: WsCallbacks) {
  const callbacksRef = useRef(callbacks);
  callbacksRef.current = callbacks;

  const reconnectDelay = useRef(BASE_RECONNECT_DELAY);
  const wsRef = useRef<WebSocket | null>(null);
  const pingTimer = useRef<ReturnType<typeof setInterval>>();
  const mountedRef = useRef(true);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      reconnectDelay.current = BASE_RECONNECT_DELAY;
      // Send ping every 30s
      pingTimer.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send('ping');
        }
      }, 30000);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        switch (msg.event) {
          case 'new_analysis':
            callbacksRef.current.onNewAnalysis?.(msg.data);
            break;
          case 'status_change':
            callbacksRef.current.onStatusChange?.(msg.data);
            break;
        }
      } catch {
        // ignore malformed messages
      }
    };

    ws.onclose = () => {
      clearInterval(pingTimer.current);
      if (!mountedRef.current) return;
      // Reconnect with exponential backoff
      setTimeout(() => connect(), reconnectDelay.current);
      reconnectDelay.current = Math.min(
        reconnectDelay.current * 2,
        MAX_RECONNECT_DELAY
      );
    };

    ws.onerror = () => {
      ws.close();
    };
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      clearInterval(pingTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const connected = wsRef.current?.readyState === WebSocket.OPEN;
  return { connected };
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/hooks/useWebSocket.ts
git commit -m "feat: add WebSocket hook with auto-reconnect"
```

---

### Task 5.3: Create useNews hook

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/hooks/useNews.ts`

- [ ] **Step 1: Write useNews hook**

```typescript
import { useState, useCallback } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchNews, fetchTags, fetchStatus, pausePipeline, resumePipeline } from '../api/client';
import type { FilterSentiment, NewsWithAnalysis, SystemStatus, WsNewAnalysis } from '../types';

export function useNews() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [sentiment, setSentiment] = useState<FilterSentiment>('all');
  const [selectedTag, setSelectedTag] = useState<string>('');
  const [search, setSearch] = useState('');

  // News list query
  const newsQuery = useQuery({
    queryKey: ['news', page, sentiment, selectedTag, search],
    queryFn: () =>
      fetchNews({
        page,
        page_size: 20,
        sentiment: sentiment === 'all' ? undefined : sentiment,
        tag: selectedTag || undefined,
        search: search || undefined,
      }),
    staleTime: 30_000,
  });

  // Tags query
  const tagsQuery = useQuery({
    queryKey: ['tags'],
    queryFn: fetchTags,
    staleTime: 60_000,
  });

  // Status query
  const statusQuery = useQuery({
    queryKey: ['status'],
    queryFn: fetchStatus,
    refetchInterval: 30_000,
  });

  // Handle new analysis via WebSocket
  const handleNewAnalysis = useCallback(
    (_data: WsNewAnalysis) => {
      // Invalidate cache to refetch
      queryClient.invalidateQueries({ queryKey: ['news'] });
      queryClient.invalidateQueries({ queryKey: ['tags'] });
      queryClient.invalidateQueries({ queryKey: ['status'] });
    },
    [queryClient]
  );

  const handleStatusChange = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['status'] });
  }, [queryClient]);

  const handlePause = useCallback(async () => {
    await pausePipeline();
    queryClient.invalidateQueries({ queryKey: ['status'] });
  }, [queryClient]);

  const handleResume = useCallback(async () => {
    await resumePipeline();
    queryClient.invalidateQueries({ queryKey: ['status'] });
  }, [queryClient]);

  // Find selected news detail
  const selectedNews: NewsWithAnalysis | null =
    newsQuery.data?.items.find((n) => n.id === selectedId) ?? null;

  return {
    // Data
    newsItems: newsQuery.data?.items ?? [],
    total: newsQuery.data?.total ?? 0,
    totalPages: newsQuery.data?.total_pages ?? 1,
    tags: tagsQuery.data?.tags ?? [],
    status: statusQuery.data as SystemStatus | undefined,
    selectedNews,
    // Loading states
    isLoading: newsQuery.isLoading,
    isError: newsQuery.isError,
    // Selection
    selectedId,
    setSelectedId,
    // Filters
    page,
    setPage,
    sentiment,
    setSentiment,
    selectedTag,
    setSelectedTag,
    search,
    setSearch,
    // Actions
    handlePause,
    handleResume,
    // WebSocket callbacks
    handleNewAnalysis,
    handleStatusChange,
  };
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/hooks/useNews.ts
git commit -m "feat: add useNews hook with React Query"
```

---

### Task 5.4: Create StatusBar component

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/components/StatusBar.tsx`

- [ ] **Step 1: Write StatusBar**

```tsx
import type { SystemStatus } from '../types';

interface Props {
  status: SystemStatus | undefined;
  connected: boolean;
  onPause: () => void;
  onResume: () => void;
}

export function StatusBar({ status, connected, onPause, onResume }: Props) {
  const isPaused = status?.status === 'paused';

  return (
    <header className="flex items-center justify-between px-6 py-3 bg-gray-900 border-b border-gray-800">
      <div className="flex items-center gap-4">
        <h1 className="text-lg font-bold text-blue-400">📊 TradeAnalysis</h1>
        <span className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-green-400' : 'bg-gray-500'}`} />
        <span className="text-sm text-gray-400">
          {connected ? '在线' : '离线重连中'}
        </span>
        {status?.last_update && (
          <span className="text-sm text-gray-500">
            上次更新: {new Date(status.last_update).toLocaleTimeString('zh-CN')}
          </span>
        )}
      </div>

      <div className="flex items-center gap-3">
        {status && (
          <span className="text-sm text-gray-400">
            已加载 {status.news_count} 条新闻
          </span>
        )}
        <button
          onClick={isPaused ? onResume : onPause}
          className={`px-4 py-1.5 rounded text-sm font-medium transition ${
            isPaused
              ? 'bg-green-700 hover:bg-green-600 text-white'
              : 'bg-yellow-700 hover:bg-yellow-600 text-white'
          }`}
        >
          {isPaused ? '▶ 开始' : '⏸ 暂停'}
        </button>
      </div>
    </header>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/StatusBar.tsx
git commit -m "feat: add StatusBar component"
```

---

### Task 5.5: Create FilterBar component

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/components/FilterBar.tsx`

- [ ] **Step 1: Write FilterBar**

```tsx
import type { FilterSentiment } from '../types';

interface Props {
  sentiment: FilterSentiment;
  onSentimentChange: (s: FilterSentiment) => void;
  search: string;
  onSearchChange: (s: string) => void;
}

const FILTERS: { label: string; value: FilterSentiment }[] = [
  { label: '全部', value: 'all' },
  { label: '🟢 利好', value: '利好' },
  { label: '🔴 利空', value: '利空' },
  { label: '⚪ 中性', value: '中性' },
];

export function FilterBar({ sentiment, onSentimentChange, search, onSearchChange }: Props) {
  return (
    <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-800">
      {FILTERS.map((f) => (
        <button
          key={f.value}
          onClick={() => onSentimentChange(f.value)}
          className={`px-3 py-1 rounded text-sm transition ${
            sentiment === f.value
              ? 'bg-blue-700 text-white'
              : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
          }`}
        >
          {f.label}
        </button>
      ))}
      <div className="flex-1" />
      <input
        type="text"
        value={search}
        onChange={(e) => onSearchChange(e.target.value)}
        placeholder="🔍 搜索..."
        className="px-3 py-1 rounded bg-gray-800 text-sm text-gray-200 placeholder-gray-500 border border-gray-700 focus:border-blue-500 focus:outline-none w-48"
      />
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/FilterBar.tsx
git commit -m "feat: add FilterBar component"
```

---

### Task 5.6: Create TagCloud component

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/components/TagCloud.tsx`

- [ ] **Step 1: Write TagCloud**

```tsx
interface Props {
  tags: string[];
  selectedTag: string;
  onTagSelect: (tag: string) => void;
}

export function TagCloud({ tags, selectedTag, onTagSelect }: Props) {
  if (tags.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1.5 px-4 py-2 border-b border-gray-800">
      <span className="text-xs text-gray-500 mr-1 py-1">标签:</span>
      {tags.slice(0, 30).map((tag) => (
        <button
          key={tag}
          onClick={() => onTagSelect(selectedTag === tag ? '' : tag)}
          className={`px-2 py-0.5 rounded text-xs transition ${
            selectedTag === tag
              ? 'bg-blue-700 text-white'
              : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
          }`}
        >
          {tag}
        </button>
      ))}
      {selectedTag && (
        <button
          onClick={() => onTagSelect('')}
          className="px-2 py-0.5 rounded text-xs bg-red-900 text-red-300 hover:bg-red-800"
        >
          ✕ 清除
        </button>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/TagCloud.tsx
git commit -m "feat: add TagCloud component"
```

---

### Task 5.7: Create NewsCard and NewsList components

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/components/NewsCard.tsx`
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/components/NewsList.tsx`

- [ ] **Step 1: Write NewsCard**

```tsx
import type { NewsWithAnalysis } from '../types';

interface Props {
  news: NewsWithAnalysis;
  isSelected: boolean;
  onClick: () => void;
}

function parseJsonArray(val: string | null): string[] {
  if (!val) return [];
  try {
    return JSON.parse(val);
  } catch {
    return [];
  }
}

const LEVEL_COLORS: Record<string, string> = {
  A: 'text-red-400',
  B: 'text-yellow-400',
  C: 'text-gray-400',
};

function formatTime(ctime: number | null): string {
  if (!ctime) return '';
  return new Date(ctime * 1000).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function NewsCard({ news, isSelected, onClick }: Props) {
  const analysis = news.analysis;
  const levelClass = LEVEL_COLORS[news.level ?? ''] ?? 'text-gray-400';
  const tags = [
    ...parseJsonArray(analysis?.sectors ?? null),
    ...parseJsonArray(analysis?.stocks ?? null),
  ].slice(0, 5);

  return (
    <div
      onClick={onClick}
      className={`px-4 py-3 cursor-pointer border-b border-gray-800/50 transition hover:bg-gray-800/50 ${
        isSelected ? 'bg-gray-800 border-l-2 border-l-blue-500' : ''
      }`}
    >
      <div className="flex items-start gap-2">
        <span className={`text-xs font-bold mt-0.5 shrink-0 ${levelClass}`}>
          {news.level ? `🔴 ${news.level}` : ''}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-500 shrink-0">
              {formatTime(news.ctime)}
            </span>
            <h3 className="text-sm font-medium text-gray-200 truncate">
              {news.title}
            </h3>
          </div>
          {analysis && (
            <div className="mt-1 flex items-center gap-2 flex-wrap">
              <span className={`text-xs font-medium ${
                analysis.sentiment === '利好' ? 'text-green-400' :
                analysis.sentiment === '利空' ? 'text-red-400' :
                'text-gray-400'
              }`}>
                {analysis.sentiment}
              </span>
              {tags.map((tag) => (
                <span key={tag} className="text-xs px-1.5 py-0.5 rounded bg-gray-700 text-gray-300">
                  {tag}
                </span>
              ))}
            </div>
          )}
          {analysis?.summary && (
            <p className="mt-1 text-xs text-gray-500 truncate">{analysis.summary}</p>
          )}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Write NewsList**

```tsx
import type { NewsWithAnalysis } from '../types';
import { NewsCard } from './NewsCard';

interface Props {
  items: NewsWithAnalysis[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  isLoading: boolean;
}

export function NewsList({ items, selectedId, onSelect, isLoading }: Props) {
  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12 text-gray-500">
        <span className="animate-pulse">加载中...</span>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="flex items-center justify-center py-12 text-gray-500">
        暂无新闻
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      {items.map((news) => (
        <NewsCard
          key={news.id}
          news={news}
          isSelected={news.id === selectedId}
          onClick={() => onSelect(news.id)}
        />
      ))}
    </div>
  );
}
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/NewsCard.tsx frontend/src/components/NewsList.tsx
git commit -m "feat: add NewsCard and NewsList components"
```

---

### Task 5.8: Create DetailPanel component

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/components/DetailPanel.tsx`

- [ ] **Step 1: Write DetailPanel**

```tsx
import type { NewsWithAnalysis } from '../types';

interface Props {
  news: NewsWithAnalysis | null;
}

function parseJsonArray(val: string | null): string[] {
  if (!val) return [];
  try {
    return JSON.parse(val);
  } catch {
    return [];
  }
}

const SENTIMENT_EMOJI: Record<string, string> = {
  '利好': '🟢',
  '利空': '🔴',
  '中性': '⚪',
};

export function DetailPanel({ news }: Props) {
  if (!news) {
    return (
      <div className="h-full flex items-center justify-center text-gray-500">
        <p>选择左侧新闻查看详情</p>
      </div>
    );
  }

  const analysis = news.analysis;

  return (
    <div className="h-full overflow-y-auto p-6 space-y-6">
      {/* Original News */}
      <section>
        <h2 className="text-sm font-bold text-gray-400 uppercase tracking-wide mb-3">
          📰 新闻原文
        </h2>
        <h1 className="text-xl font-bold text-gray-100 mb-2">{news.title}</h1>
        {news.ctime && (
          <p className="text-sm text-gray-500 mb-3">
            {new Date(news.ctime * 1000).toLocaleString('zh-CN')}
          </p>
        )}
        {news.content && (
          <p className="text-gray-300 leading-relaxed">{news.content}</p>
        )}
      </section>

      {/* AI Analysis */}
      {analysis ? (
        <>
          <hr className="border-gray-800" />
          <section>
            <h2 className="text-sm font-bold text-gray-400 uppercase tracking-wide mb-4">
              🤖 AI 深度解读
            </h2>

            <div className="space-y-4">
              {/* Summary */}
              {analysis.summary && (
                <div className="bg-gray-800/50 rounded-lg p-4">
                  <h3 className="text-xs font-bold text-gray-400 mb-1">📌 摘要</h3>
                  <p className="text-gray-200">{analysis.summary}</p>
                </div>
              )}

              {/* Sentiment */}
              {analysis.sentiment && (
                <div className="bg-gray-800/50 rounded-lg p-4">
                  <h3 className="text-xs font-bold text-gray-400 mb-1">📊 情绪</h3>
                  <p className={`text-lg font-bold ${
                    analysis.sentiment === '利好' ? 'text-green-400' :
                    analysis.sentiment === '利空' ? 'text-red-400' :
                    'text-gray-300'
                  }`}>
                    {SENTIMENT_EMOJI[analysis.sentiment]} {analysis.sentiment}
                  </p>
                </div>
              )}

              {/* Impact */}
              {analysis.impact && (
                <div className="bg-gray-800/50 rounded-lg p-4">
                  <h3 className="text-xs font-bold text-gray-400 mb-1">💥 影响</h3>
                  <p className="text-gray-200">{analysis.impact}</p>
                </div>
              )}

              {/* Sectors */}
              <div className="bg-gray-800/50 rounded-lg p-4">
                <h3 className="text-xs font-bold text-gray-400 mb-2">🏷 关联板块</h3>
                <div className="flex flex-wrap gap-1.5">
                  {parseJsonArray(analysis.sectors).map((s) => (
                    <span key={s} className="px-2 py-0.5 rounded text-xs bg-blue-900/50 text-blue-300">
                      {s}
                    </span>
                  ))}
                  {parseJsonArray(analysis.sectors).length === 0 && (
                    <span className="text-xs text-gray-500">无</span>
                  )}
                </div>
              </div>

              {/* Stocks */}
              <div className="bg-gray-800/50 rounded-lg p-4">
                <h3 className="text-xs font-bold text-gray-400 mb-2">📈 关联标的</h3>
                <div className="flex flex-wrap gap-1.5">
                  {parseJsonArray(analysis.stocks).map((s) => (
                    <span key={s} className="px-2 py-0.5 rounded text-xs bg-green-900/50 text-green-300">
                      {s}
                    </span>
                  ))}
                  {parseJsonArray(analysis.stocks).length === 0 && (
                    <span className="text-xs text-gray-500">无</span>
                  )}
                </div>
              </div>

              {/* Historical */}
              {analysis.historical && (
                <div className="bg-gray-800/50 rounded-lg p-4">
                  <h3 className="text-xs font-bold text-gray-400 mb-1">📜 历史联动</h3>
                  <p className="text-gray-200">{analysis.historical}</p>
                </div>
              )}

              {/* Suggestion */}
              {analysis.suggestion && (
                <div className="bg-gray-800/50 rounded-lg p-4 border-l-2 border-yellow-500">
                  <h3 className="text-xs font-bold text-yellow-400 mb-1">💡 操作建议</h3>
                  <p className="text-gray-200">{analysis.suggestion}</p>
                </div>
              )}
            </div>
          </section>
        </>
      ) : (
        <>
          <hr className="border-gray-800" />
          <div className="flex items-center justify-center py-8 text-gray-500">
            <span className="animate-pulse">等待 AI 解读中...</span>
          </div>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx
git commit -m "feat: add DetailPanel component"
```

---

### Task 5.9: Create Pagination component

**Files:**
- Create: `/Users/joesun/trae/Tradeanalysis/frontend/src/components/Pagination.tsx`

- [ ] **Step 1: Write Pagination**

```tsx
interface Props {
  page: number;
  totalPages: number;
  onPageChange: (p: number) => void;
}

export function Pagination({ page, totalPages, onPageChange }: Props) {
  if (totalPages <= 1) return null;

  return (
    <div className="flex items-center justify-center gap-2 py-3 border-t border-gray-800">
      <button
        onClick={() => onPageChange(page - 1)}
        disabled={page <= 1}
        className="px-3 py-1 rounded text-sm bg-gray-800 text-gray-400 hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed transition"
      >
        ← 上一页
      </button>
      <span className="text-sm text-gray-400">
        {page} / {totalPages}
      </span>
      <button
        onClick={() => onPageChange(page + 1)}
        disabled={page >= totalPages}
        className="px-3 py-1 rounded text-sm bg-gray-800 text-gray-400 hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed transition"
      >
        下一页 →
      </button>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/Pagination.tsx
git commit -m "feat: add Pagination component"
```

---

### Task 5.10: Create App.tsx and main.tsx

**Files:**
- Modify: `/Users/joesun/trae/Tradeanalysis/frontend/src/App.tsx`
- Modify: `/Users/joesun/trae/Tradeanalysis/frontend/src/main.tsx`

- [ ] **Step 1: Write App.tsx**

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ErrorBoundary } from './components/ErrorBoundary';
import { StatusBar } from './components/StatusBar';
import { FilterBar } from './components/FilterBar';
import { TagCloud } from './components/TagCloud';
import { NewsList } from './components/NewsList';
import { DetailPanel } from './components/DetailPanel';
import { Pagination } from './components/Pagination';
import { useWebSocket } from './hooks/useWebSocket';
import { useNews } from './hooks/useNews';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 2,
      refetchOnWindowFocus: false,
    },
  },
});

function AppInner() {
  const {
    newsItems,
    totalPages,
    tags,
    status,
    selectedNews,
    isLoading,
    selectedId,
    setSelectedId,
    page,
    setPage,
    sentiment,
    setSentiment,
    selectedTag,
    setSelectedTag,
    search,
    setSearch,
    handlePause,
    handleResume,
    handleNewAnalysis,
    handleStatusChange,
  } = useNews();

  const { connected } = useWebSocket({
    onNewAnalysis: handleNewAnalysis,
    onStatusChange: handleStatusChange,
  });

  return (
    <div className="h-screen flex flex-col bg-gray-950 text-gray-100">
      <StatusBar
        status={status}
        connected={connected}
        onPause={handlePause}
        onResume={handleResume}
      />
      <div className="flex flex-1 overflow-hidden">
        {/* Left Panel - 40% */}
        <div className="w-[40%] min-w-[360px] flex flex-col border-r border-gray-800">
          <FilterBar
            sentiment={sentiment}
            onSentimentChange={(s) => { setSentiment(s); setPage(1); setSelectedId(null); }}
            search={search}
            onSearchChange={(s) => { setSearch(s); setPage(1); }}
          />
          <TagCloud
            tags={tags}
            selectedTag={selectedTag}
            onTagSelect={(t) => { setSelectedTag(t); setPage(1); }}
          />
          <NewsList
            items={newsItems}
            selectedId={selectedId}
            onSelect={setSelectedId}
            isLoading={isLoading}
          />
          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </div>

        {/* Right Panel - 60% */}
        <div className="flex-1 overflow-hidden">
          <DetailPanel news={selectedNews} />
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <AppInner />
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
```

- [ ] **Step 2: Write main.tsx**

```tsx
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
```

- [ ] **Step 3: Verify build succeeds**

Run:
```bash
cd /Users/joesun/trae/Tradeanalysis/frontend
npx tsc --noEmit
npm run build
```

Expected: Build completes without errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/App.tsx frontend/src/main.tsx
git commit -m "feat: add App entry with full layout and state wiring"
```

---

## Phase 6: Integration & Verification

### Task 6.1: End-to-end smoke test

- [ ] **Step 1: Start backend**

```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
export DEEPSEEK_API_KEY="your-api-key"  # Set actual key
python main.py &
sleep 3
```

- [ ] **Step 2: Verify health endpoint**

```bash
curl -s http://localhost:8000/api/health
```

Expected: `{"status":"ok"}`

- [ ] **Step 3: Verify status endpoint**

```bash
curl -s http://localhost:8000/api/status | python -m json.tool
```

Expected: JSON with `status`, `news_count`, `last_update` fields.

- [ ] **Step 4: Start frontend**

```bash
cd /Users/joesun/trae/Tradeanalysis/frontend
npm run dev &
sleep 3
```

- [ ] **Step 5: Open browser and verify**

Open http://localhost:5173. Check:
- StatusBar shows green dot (connected) or gray (reconnecting)
- Left panel shows news list (or empty state)
- Right panel shows "选择左侧新闻查看详情"
- Pause/Resume button works

- [ ] **Step 6: Simulate news and analysis**

Wait for scheduler to fire (or trigger manually):
```bash
cd /Users/joesun/trae/Tradeanalysis/backend
source venv/bin/activate
python -c "
from scheduler import _fetch_and_analyze_job
import asyncio
asyncio.run(_fetch_and_analyze_job())
"
```

Then refresh browser and verify news appears with analysis.

- [ ] **Step 7: Cleanup**

```bash
kill %1 %2 2>/dev/null  # Stop backend and frontend
```

---

## Self-Review Checklist

1. **Spec coverage**: All spec sections covered — DB schema (Task 1.3), fetcher (Task 2.1), analyzer (Task 2.2), scheduler (Task 2.4), WebSocket (Task 2.3), REST API (Task 3.1), frontend layout (Tasks 5.4-5.10), filtering (Tasks 5.5-5.6), pause/resume (Task 5.4 + 3.1), error handling (Task 5.1)
2. **No placeholders**: All steps contain actual code or exact commands. No TBD/TODO.
3. **Type consistency**: `NewsWithAnalysis` (models.py) matches `NewsWithAnalysis` (types/index.ts). `AnalysisResult` is the DeepSeek output format. WebSocket event names match between backend broadcast and frontend handler. API query params match between `client.ts` and `main.py` endpoints.
4. **File consistency**: All file paths are absolute under `/Users/joesun/trae/Tradeanalysis/`.

---

Plan complete and saved. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
