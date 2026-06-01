# CLAUDE.md

## 项目概述
Tradeanalysis — [简要描述项目目的，一句话说明这个应用做什么]

## 技术栈

### 后端 (`backend/`)
- 语言 / 运行时：[例如 Python 3.12, Node.js 22, Go 1.22]
- 框架：[例如 FastAPI, Express, Gin]
- 数据库：[例如 PostgreSQL, MongoDB, SQLite]
- 其他关键依赖：[例如 Redis, Kafka, Celery]

### 前端 (`frontend/`)
- 框架：[例如 React 19, Vue 3, Next.js 14]
- 构建工具：[例如 Vite, Turbopack]
- UI 库：[例如 Tailwind CSS, Ant Design, shadcn/ui]
- 状态管理：[例如 Zustand, Pinia, Redux]

## 常用命令

```bash
# === 开发环境启动 ===
# 后端
cd backend && [启动命令]

# 前端
cd frontend && [启动命令]

# === 测试 ===
# 后端测试
cd backend && [测试命令]

# 前端测试
cd frontend && [测试命令]

# === 代码检查 / 格式化 ===
[lint 命令]
[format 命令]

# === 构建 / 部署 ===
[构建命令]
```

## 项目结构

```
Tradeanalysis/
├── backend/           # 后端服务
│   ├── [待补充]
├── frontend/          # 前端应用
│   ├── [待补充]
├── docs/              # 文档
└── CLAUDE.md          # 本文件
```

## 架构概览

[描述整体架构：微服务 / 单体、前后端分离、API 风格（REST / GraphQL / gRPC）等]

### 核心模块

1. **[模块名]** — [职责]
2. **[模块名]** — [职责]
3. **[模块名]** — [职责]

## API 设计约定

- [API 路径命名规范]
- [请求/响应格式约定]
- [错误码约定]

## 数据库设计约定

- [表命名规范]
- [字段类型约定]
- [索引策略]

## 编码规范

- 语言：[中文 / English]
- 缩进：[2 spaces / 4 spaces / tabs]
- 命名规范：文件 — [kebab-case / snake_case]，函数 — [camelCase / snake_case]
- Git 提交信息格式：[约定式提交 / 自定义格式]
- 分支策略：[GitFlow / Trunk-based / Feature branch]

## 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `[ENV_NAME]` | [说明] | [默认值] |

## 工作流程

修改代码必须遵循以下流程，不可跳步：

1. **分析原因** — 先解释问题根因，不做任何修改
2. **制定方案** — 提出修改方案，等待用户审核
3. **用户同意** — 用户明确说"好"/"可以"/"同意"后，才进入下一步
4. **制定计划** — 写实施计划到 plan file
5. **用户审核** — 用户审批计划
6. **落地实施** — 按计划修改代码

**禁止行为：** 在用户同意方案前直接改代码。先问、后改。

## 注意事项 / 约定

- DuckDB ≥1.0，不支持 AUTOINCREMENT，用 INTEGER PRIMARY KEY 默认自增
- Python 版本 ≥3.9，不使用 `list[str] | None` 语法，用 `Optional[list[str]]`
- dwd_weekly_quote 表没有 is_suspended 列，周线查询不要加此过滤条件
