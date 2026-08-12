# Backend — fastapi-admin

[中文文档](./README_zh.md) · [Root README](../README.md)

Async FastAPI service for the RBAC admin console (auth, users/roles/permissions, audit, dashboard) plus the ops Agent stack (session REST, WebSocket, HITL approvals, knowledge base).

See [docs/AGENT_ARCHITECTURE.md](../docs/AGENT_ARCHITECTURE.md) for the architecture overview.

## Stack

- Python **3.14**, FastAPI, Uvicorn (including WebSocket)
- SQLAlchemy **2** (async) + **PostgreSQL** (psycopg) + Alembic
- JWT access tokens + persistent refresh session families
- Argon2id (pwdlib) with bcrypt verify/migrate for legacy hashes
- LLM calls go through `app.core.llm` (OpenAI-compatible HTTP); tools/HITL live under `app/agent/`
- Tooling: **uv**, ruff, mypy, pytest

## Project layout

```text
backend/
├── app/
│   ├── api/v1/          # Routes (agent_sessions / agent_ws / hitl / knowledge)
│   ├── agent/           # Agent core: loop, chat_turn, ws_hub, HITL, tools
│   ├── core/            # Config, DB, security, deps, llm
│   ├── crud/            # Data access
│   ├── models/          # SQLAlchemy models
│   ├── schemas/         # Pydantic models (incl. agent_ws / agent_session)
│   ├── services/        # Auth sessions, cleanup, rate limits, knowledge ingest
│   └── main.py          # ASGI app factory
├── alembic/             # Migrations
├── tests/
├── init_db.py           # Superuser + permission seed
├── main.py              # Windows-safe uvicorn entry
└── .env.example
```

## Setup

```bash
cd backend
cp .env.example .env
uv sync
```

Minimum `.env` for local development:

```ini
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/fastapi_admin
ENVIRONMENT=development
BACKEND_CORS_ORIGINS=http://localhost:5173,http://localhost:3000
ALLOWED_HOSTS=localhost,127.0.0.1
```

Ops Assistant chat also needs a chat model (real calls → cost/quota):

```ini
LLM_CHAT_BASE_URL=...
LLM_CHAT_API_KEY=...
LLM_CHAT_MODEL=...
```

Knowledge upload / semantic search need embedding settings (see `.env.example`). Optional: `HITL_NOTIFY_AUTO_APPROVE=true` auto-approves `notify` HITL actions (default `false`).

First-time bootstrap (optional but recommended):

```ini
INIT_SUPERUSER_USERNAME=admin
INIT_SUPERUSER_EMAIL=admin@example.com
INIT_SUPERUSER_PASSWORD=your-strong-password
```

```bash
uv run alembic upgrade head
uv run python init_db.py
```

`init_db.py` idempotently seeds **24** system permissions aligned with `require_permission` / the frontend `PERMISSIONS` constants. It does **not** create roles or assignments. Superuser creation is skipped if one already exists.

## Run

Prefer the project entry (SelectorEventLoop on Windows for async psycopg):

```bash
uv run python main.py
```

- API base: `http://localhost:8000/api/v1`
- OpenAPI: `http://localhost:8000/docs`

Useful flags in `.env`:

| Variable | Purpose |
| --- | --- |
| `LOG_LEVEL` | Root log level (`info` default) |
| `SQL_ECHO` | Set `true` only while debugging SQL |
| `REGISTRATION_ENABLED` | Public self-registration (off by default) |
| `LLM_CHAT_*` / `LLM_EMBEDDING_*` | Chat and embedding models |
| `HITL_NOTIFY_AUTO_APPROVE` | Auto-approve `notify` HITL actions |
| `AGENT_*` | Child-agent concurrency/depth/cost caps (see `.env.example`) |

## Main API modules (`/api/v1`)

| Prefix | Purpose |
| --- | --- |
| `/auth` `/users` `/roles` `/permissions` `/me` `/dashboard` `/audit-logs` | RBAC admin |
| `/knowledge` | Categories + document upload (`.md`/`.txt`) |
| `/hitl` | HITL proposal list/get/`decide` (`agent:hitl_approve`) |
| `/agent` | Session CRUD + `POST .../messages` → `run_chat_turn` |
| `/ws/agent/{session_id}` | Ops Assistant WebSocket (`access_token` query; safe HITL summary only — no raw `action_payload`) |

Sessions and WS enforce ownership: only the session owner may read/write/connect. Sending a message runs the Agent loop — tests must mock `chat`; confirm model config/cost before live debugging.

## Permission codes (seeded)

| Module | Codes |
| --- | --- |
| Users | `user:read` `user:create` `user:update` `user:delete` `user:assign` `user:reset_password` |
| Roles | `role:read` `role:create` `role:update` `role:delete` `role:assign` |
| Permissions | `permission:read` `permission:create` `permission:update` `permission:delete` |
| Audit | `audit:read` |
| Knowledge | `knowledge:read` `knowledge:upload` `knowledge:manage` |
| CMDB | `cmdb:read` `cmdb:manage` |
| Monitor | `monitor:read` `monitor:manage` |
| Agent | `agent:hitl_approve` |

The Ops Assistant Chat page is available to any logged-in user. Upload UI needs `knowledge:upload` (listing categories also needs `knowledge:read`). HITL approve/reject needs `agent:hitl_approve`.

## Quality

```bash
uv run ruff check .
uv run mypy app
uv run pytest
```

Ops-assistant-focused tests: `tests/test_agent_ws_*.py`, `test_agent_sessions_api.py`, `test_chat_turn.py`, `test_hitl_api.py`, `test_ops_assistant_integration.py`.

## More

- Architecture & WS contract: [docs/AGENT_ARCHITECTURE.md](../docs/AGENT_ARCHITECTURE.md)
- Production hardening, DB roles, and proxy settings: [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md)
