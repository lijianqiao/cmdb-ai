# 后端 — fastapi-admin

[English](./README.md) · [根目录说明](../README_zh.md)

面向 RBAC 管理后台的异步 FastAPI 服务：认证、用户/角色/权限、审计与仪表盘；以及运维 Agent（会话 REST、WebSocket、HITL 审批、知识库）。

架构总览见 [docs/AGENT_ARCHITECTURE.md](../docs/AGENT_ARCHITECTURE.md)。

## 技术栈

- Python **3.14**、FastAPI、Uvicorn（含 WebSocket）
- SQLAlchemy **2**（异步）+ **PostgreSQL**（psycopg）+ Alembic
- JWT access token + 持久化 refresh 会话族
- Argon2id（pwdlib），兼容校验/迁移旧 bcrypt 哈希
- 大模型调用经 `app.core.llm`（OpenAI 兼容 HTTP）；工具与 HITL 在 `app/agent/`
- 工具：**uv**、ruff、mypy、pytest

## 目录结构

```text
backend/
├── app/
│   ├── api/v1/          # 路由（含 agent_sessions / agent_ws / hitl / knowledge）
│   ├── agent/           # Agent 内核：loop、chat_turn、ws_hub、HITL、工具与编排
│   ├── core/            # 配置、数据库、安全、依赖注入、llm
│   ├── crud/            # 数据访问
│   ├── models/          # ORM 模型
│   ├── schemas/         # Pydantic Schema（含 agent_ws / agent_session）
│   ├── services/        # 会话、清理、限流、知识入库等
│   └── main.py          # ASGI 应用工厂
├── alembic/             # 迁移
├── tests/
├── init_db.py           # 超管 + 权限种子
├── main.py              # Windows 友好的启动入口
└── .env.example
```

## 环境准备

```bash
cd backend
cp .env.example .env
uv sync
```

本地开发最少配置：

```ini
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/fastapi_admin
ENVIRONMENT=development
BACKEND_CORS_ORIGINS=http://localhost:5173,http://localhost:3000
ALLOWED_HOSTS=localhost,127.0.0.1
```

运维助手对话还需配置聊天模型（会真实调用，产生费用/额度）：

```ini
LLM_CHAT_BASE_URL=...
LLM_CHAT_API_KEY=...
LLM_CHAT_MODEL=...
```

知识库上传/语义检索还需 embedding 相关变量（见 `.env.example`）。

首次初始化建议配置：

```ini
INIT_SUPERUSER_USERNAME=admin
INIT_SUPERUSER_EMAIL=admin@example.com
INIT_SUPERUSER_PASSWORD=你的强密码
```

```bash
uv run alembic upgrade head
uv run python init_db.py
```

`init_db.py` 会幂等写入与 `require_permission` / 前端 `PERMISSIONS` 对齐的 **24** 条系统权限，**不**创建角色或分配关系。若已有超级管理员则跳过创建。

## 启动

建议使用项目入口（Windows 上使用 SelectorEventLoop，兼容异步 psycopg）：

```bash
uv run python main.py
```

- 接口前缀：`http://localhost:8000/api/v1`
- OpenAPI：`http://localhost:8000/docs`

常用环境变量：

| 变量 | 作用 |
| --- | --- |
| `LOG_LEVEL` | 根日志级别（默认 `info`） |
| `SQL_ECHO` | 仅排查 SQL 时设为 `true` |
| `REGISTRATION_ENABLED` | 是否开放自助注册（默认关闭） |
| `LLM_CHAT_*` / `LLM_EMBEDDING_*` | 聊天与向量模型 |
| `AGENT_*` | 子 Agent 并发/深度/成本上限（见 `.env.example`） |

## 主要 API 模块（`/api/v1`）

| 前缀 | 说明 |
| --- | --- |
| `/auth` `/users` `/roles` `/permissions` `/me` `/dashboard` `/audit-logs` | RBAC 后台 |
| `/knowledge` | 知识库分类与文档上传（`.md`/`.txt`） |
| `/hitl` | HITL 提案查询与 `decide`（需 `agent:hitl_approve`） |
| `/agent` | 会话 CRUD + `POST .../messages` 触发一轮 `run_chat_turn` |
| `/ws/agent/{session_id}` | 运维助手 WebSocket（query `access_token`；只推安全摘要，不含敏感 `action_payload`） |

会话与 WS 均校验归属：只能操作**自己的** `AgentSession`。发消息会跑 Agent loop，单测必须 mock `chat`，手工联调前请确认模型配置与费用。

## 权限码（种子）

| 模块 | 权限码 |
| --- | --- |
| 用户 | `user:read` `user:create` `user:update` `user:delete` `user:assign` `user:reset_password` |
| 角色 | `role:read` `role:create` `role:update` `role:delete` `role:assign` |
| 权限 | `permission:read` `permission:create` `permission:update` `permission:delete` |
| 审计 | `audit:read` |
| 知识库 | `knowledge:read` `knowledge:upload` `knowledge:manage` |
| CMDB | `cmdb:read` `cmdb:manage` |
| 监控 | `monitor:read` `monitor:manage` |
| Agent | `agent:hitl_approve` |

运维助手 Chat 页面本身对登录用户开放；上传入口要 `knowledge:upload`（列分类还要 `knowledge:read`）；HITL 批准/拒绝要 `agent:hitl_approve`。

## 质量检查

```bash
uv run ruff check .
uv run mypy app
uv run pytest
```

与运维助手相关的契约测可重点看：`tests/test_agent_ws_*.py`、`test_agent_sessions_api.py`、`test_chat_turn.py`、`test_hitl_api.py`、`test_ops_assistant_integration.py`。

## 更多

- 架构与 WS 契约：[docs/AGENT_ARCHITECTURE.md](../docs/AGENT_ARCHITECTURE.md)
- 生产环境、数据库角色与代理配置：[docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md)
