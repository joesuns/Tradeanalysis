# 财联社新闻采集 + AI 解读 + Web 展示 设计文档

> 日期: 2026-05-30 | 状态: 已确认

## 1. 概述

### 1.1 目标

构建一个个人使用的 Web 应用，自动采集财联社电报新闻，通过 DeepSeek V4 大模型进行深度解读，并在网页端实时展示。

### 1.2 用户场景

- 打开浏览器即可查看最新财联社新闻和 AI 解读
- 系统自动定时拉取新闻并逐条分析，无需手动操作
- 可通过情绪、标签（板块/标的）筛选新闻
- 支持暂停/恢复抓取流程（如市场休市时暂停以节约 API 费用）

---

## 2. 架构总览

采用 **FastAPI 单体后端 + React 前端** 架构。

```
┌─────────────────────────────────────┐
│          单一 Python 进程            │
│  ┌──────────┐  ┌──────────────────┐ │
│  │ APScheduler│  │   FastAPI       │ │
│  │ 定时抓新闻 │  │ REST + WebSocket │ │
│  └──────────┘  └──────────────────┘ │
│  ┌──────────┐  ┌──────────────────┐ │
│  │ AKShare  │  │   SQLite         │ │
│  │ DeepSeek │  │                  │ │
│  └──────────┘  └──────────────────┘ │
└─────────────────────────────────────┘
         ↕ WebSocket + REST
┌─────────────────┐
│   React 前端     │
│   Vite + Tailwind│
└─────────────────┘
```

---

## 3. 项目结构

```
Tradeanalysis/
├── backend/
│   ├── main.py              # FastAPI 入口，启动调度器
│   ├── config.py            # 配置（API Key、模型、抓取间隔等）
│   ├── database.py          # SQLite 连接和表初始化
│   ├── models.py            # Pydantic 数据模型
│   ├── scheduler.py         # APScheduler 定时任务，含暂停/恢复
│   ├── fetcher.py           # AKShare 新闻抓取
│   ├── analyzer.py          # DeepSeek API 解读
│   └── websocket_manager.py # WebSocket 连接管理 + 广播
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── Layout.tsx          # 左右分屏容器
│   │   │   ├── NewsList.tsx        # 左侧新闻列表
│   │   │   ├── NewsCard.tsx        # 列表中的新闻条目
│   │   │   ├── DetailPanel.tsx     # 右侧：原文 + AI 解读
│   │   │   ├── FilterBar.tsx       # 筛选栏 + 标签云
│   │   │   ├── TagCloud.tsx        # 板块/标的标签选择
│   │   │   ├── StatusBar.tsx       # 状态指示 + 暂停/开始按钮
│   │   │   └── Pagination.tsx      # 分页控件
│   │   ├── hooks/
│   │   │   ├── useWebSocket.ts     # WebSocket 连接 hook
│   │   │   └── useNews.ts          # 新闻数据获取 hook
│   │   ├── types/
│   │   │   └── index.ts           # TypeScript 类型定义
│   │   └── api/
│   │       └── client.ts          # REST API 调用封装
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── tailwind.config.js
├── docs/
│   └── superpowers/
│       └── specs/
│           └── 2026-05-30-cls-news-analysis-design.md
└── requirements.txt
```

---

## 4. 后端设计

### 4.1 数据库表结构（SQLite）

```sql
-- 新闻原始数据
CREATE TABLE news (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id  TEXT UNIQUE,          -- 财联社文章ID，去重用
    title       TEXT NOT NULL,
    content     TEXT,
    ctime       INTEGER,              -- 发布时间戳
    level       TEXT,                  -- 重要性 (A/B/C)
    fetched_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- AI 解读结果
CREATE TABLE analysis (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id     INTEGER UNIQUE REFERENCES news(id),
    summary     TEXT,                  -- 摘要
    sentiment   TEXT,                  -- 利好 / 利空 / 中性
    impact      TEXT,                  -- 市场影响判断
    sectors     TEXT,                  -- 关联板块 (JSON 数组字符串)
    stocks      TEXT,                  -- 关联标的 (JSON 数组字符串)
    historical  TEXT,                  -- 历史事件联动
    suggestion  TEXT,                  -- 操作建议
    analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 系统状态 (单行记录，持久化暂停状态)
CREATE TABLE system_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- 初始化: INSERT INTO system_state VALUES ('paused', 'false');
```

### 4.2 模块职责

| 模块 | 职责 | 关键行为 |
|------|------|----------|
| `config.py` | 集中管理配置 | DeepSeek API Key、模型名、抓取间隔(默认300秒)、数据库路径 |
| `database.py` | SQLite 连接管理 | 启动时自动建表，提供 `get_db()` 依赖注入 |
| `fetcher.py` | 调用 AKShare 拉取新闻 | 使用 `stock_info_global_cls()`，按 `article_id` 去重入库 |
| `analyzer.py` | 调用 DeepSeek V4 解读 | 逐条分析未解读新闻，解析 JSON 输出，写入 analysis 表 |
| `scheduler.py` | 定时触发抓取→分析流程 | 每 N 分钟执行，支持暂停/恢复，检查 `system_state` |
| `websocket_manager.py` | WebSocket 连接管理 | 维护活跃连接，分析完成时广播 `new_analysis` 事件 |
| `main.py` | FastAPI 应用入口 | 挂载路由、启动调度器、处理 lifespan |

### 4.3 数据流

```
┌─────────┐   定时触发   ┌─────────┐  新新闻  ┌──────────┐
│Scheduler│ ──────────→ │ Fetcher │ ──────→ │ Analyzer │
└─────────┘             └─────────┘         └──────────┘
                              │                    │
                              ↓                    ↓
                         ┌─────────┐         ┌──────────┐
                         │ SQLite  │         │WebSocket │
                         │  存储    │         │ 广播前端  │
                         └─────────┘         └──────────┘
```

1. APScheduler 每 N 分钟触发任务
2. Fetcher 拉取最新新闻，去重后写入 `news` 表
3. Analyzer 查询无 analysis 记录的 news，逐条调用 DeepSeek
4. 每完成一条分析，写入 `analysis` 表 + WebSocket 广播
5. 前端收到事件后将新卡片插入列表顶部

### 4.4 DeepSeek API 调用

- 使用 OpenAI 兼容 SDK（`openai` Python 包），base_url 指向 DeepSeek
- System Prompt 固定为结构化分析指令
- 要求返回 JSON 格式，包含 7 个字段
- 添加重试机制：单条失败 3 次则跳过，记录日志

### 4.5 暂停/恢复机制

```
POST /api/pause   → 设置 system_state.paused = 'true'，等当前任务完成后不再触发
POST /api/resume  → 设置 system_state.paused = 'false'，恢复调度
GET  /api/status  → 返回 { status, last_update, news_count }
```

暂停时行为：
- 当前正在执行的 fetcher + analyzer 跑完（避免脏数据）
- 后续调度周期跳过
- 状态持久化到 SQLite，重启后保持

### 4.6 REST API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/news` | 分页新闻列表，支持 `?tag=&sentiment=&level=&search=&page=&page_size=` |
| GET | `/api/news/{id}` | 单条新闻 + 解读详情 |
| GET | `/api/tags` | 热门标签列表（从 sectors + stocks 聚合） |
| GET | `/api/status` | 系统状态（running/paused/analyzing + 统计） |
| POST | `/api/pause` | 暂停抓取 |
| POST | `/api/resume` | 恢复抓取 |
| WS | `/ws` | WebSocket 连接，推送 `new_analysis` 和 `status_change` |

---

## 5. 前端设计

### 5.1 技术栈

| 层 | 选型 |
|------|------|
| 框架 | React 18 + TypeScript |
| 构建 | Vite |
| 样式 | Tailwind CSS |
| 数据获取 | TanStack React Query |
| WebSocket | 原生 WebSocket（自定义 hook） |

### 5.2 页面布局（左右分屏）

```
┌──────────────────────────────────────────────────────────────────────┐
│  📊 TradeAnalysis          🟢 在线  | 上次更新: 3秒前  [⏸ 暂停]    │
├────────────────────────────────┬─────────────────────────────────────┤
│  筛选: [全部][利好][利空][中性] │                                     │
│  标签: [银行][新能源][AI][白酒] │      📰 新闻原文                    │
│  🔍 搜索...                    │  "央行决定于2026年6月15日下调金融..."│
│                                │                                     │
│  ┌──────────────────────────┐  │  ─────────────────────────────────  │
│  │ 🔴 央行下调准备金率 14:32│ ←│  🤖 AI 深度解读                    │
│  │ 🟢 利好 | 银行 券商 地产 │选中│                                     │
│  │──────────────────────────│  │  📌 摘要: ...                       │
│  │ 🟡 特斯拉FSD获批  14:28  │  │  📊 情绪: 🟢 利好                   │
│  │ 🟢 利好 | 自动驾驶 AI    │  │  💥 影响: ...                       │
│  │──────────────────────────│  │  🏷 关联板块: ...                   │
│  │ ⚪ 跨境电商新政策  14:25  │  │  📈 关联标的: ...                   │
│  │ 🟢 利好 | 跨境电商 物流  │  │  📜 历史联动: ...                   │
│  └──────────────────────────┘  │  💡 建议: ...                       │
├────────────────────────────────┴─────────────────────────────────────┤
│                         < 上一页  1/15  下一页 >                      │
└──────────────────────────────────────────────────────────────────────┘
```

### 5.3 组件树

```
App
├── StatusBar          # 在线状态 + 上次更新时间 + 暂停/开始按钮
├── Layout (flex row)
│   ├── LeftPanel (40%)
│   │   ├── FilterBar  # 情绪筛选 + 搜索框
│   │   ├── TagCloud   # 板块/标的标签，点击筛选
│   │   ├── NewsList   # 新闻卡片列表，虚拟滚动
│   │   │   └── NewsCard[]  # 单条：标题 + 时间 + 情绪 + 标签
│   │   └── Pagination
│   └── RightPanel (60%)
│       └── DetailPanel
│           ├── NewsOriginal     # 原始新闻正文
│           └── AnalysisView     # 7 个解读维度展示
```

### 5.4 筛选逻辑

- **情绪筛选**：单选（全部/利好/利空/中性）
- **标签筛选**：从 `sectors` + `stocks` 拍平聚合，多选，点击切换
- **重要性筛选**：A/B/C 级别
- **关键词搜索**：模糊匹配标题和内容
- 所有筛选条件组合后通过 API 查询参数发送，后端 SQL 做 WHERE 过滤

### 5.5 WebSocket 事件

```
Server → Client:
  new_analysis    → { news_id, title, content, ...analysis_fields }
  status_change   → { status: "running" | "paused" | "analyzing" }
  fetcher_tick    → { message: "fetching...", news_count: 3, analysis_count: 3 }

Client → Server:
  ping            → 心跳，每 30 秒一次
```

### 5.6 状态管理与数据流

- **TanStack React Query** 管理 REST 数据（新闻列表、标签、系统状态）
- **WebSocket hook** 监听实时推送，收到 `new_analysis` 时 invalidate 新闻列表缓存
- 暂停状态：通过 WebSocket 和 API 双重同步，保证状态一致

---

## 6. 配置设计

```python
# backend/config.py
import os

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"  # DeepSeek V4

FETCH_INTERVAL_SECONDS = 300  # 5 分钟
DATABASE_PATH = "tradeanalysis.db"
PAGE_SIZE = 20
```

所有敏感信息通过环境变量注入，不硬编码。

---

## 7. 错误处理

| 场景 | 处理方式 |
|------|----------|
| AKShare 抓取失败 | 记录日志，跳过本次调度，下个周期重试 |
| DeepSeek API 超时/限流 | 单条重试最多 3 次（指数退避），失败跳过 |
| WebSocket 断连 | 前端自动重连（指数退避，最大 30 秒间隔） |
| SQLite 锁冲突 | WAL 模式，写入串行化 |
| 数据库文件损坏 | 启动时检测，自动从备份恢复或重建 |

---

## 8. 验证方式

1. **后端单元测试**：pytest 测试 fetcher、analyzer、scheduler 核心逻辑
2. **API 集成测试**：httpx 测试所有 REST 端点
3. **前端组件测试**：Vitest + React Testing Library 测试关键组件
4. **端到端验证**：
   - 启动后端 `python main.py`
   - 启动前端 `npm run dev`
   - 验证新闻自动拉取、AI 解读生成、WebSocket 推送、筛选/暂停功能
