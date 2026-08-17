# Backend — ent-agent

[中文文档](./README_zh.md) · [Root README](../README.md)

Async **FastAPI** backend service for the **ent-agent** platform. It provides enterprise RBAC, infrastructure asset management, TCP liveness monitoring, Netmiko network automation, and a multi-turn LLM Operations Agent runtime with Human-In-The-Loop (HITL) safety gates.

---

## Architecture Overview

See [docs/AGENT_ARCHITECTURE.md](../docs/AGENT_ARCHITECTURE.md) and [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) for full architectural specifications.

```text
backend/
├── app/
│   ├── api/v1/              # HTTP and WebSocket routers (auth, agent, cmdb, monitor, hitl, ...)
│   ├── agent/               # Agent runtime: loop, chat_turn, ws_hub, HITL, tools, Netmiko executors
│   ├── core/                # App config, async DB session, security (Argon2id/JWT), Fernet encryption, LLM client
│   ├── crud/                # Data access layer (SQLAlchemy 2 async queries)
│   ├── models/              # Declarative ORM models (PostgreSQL + pgvector)
│   ├── schemas/             # Pydantic schemas for request/response validation & WS events
│   ├── services/            # Business logic: auth session rotation, TCP monitor sweep, CMDB diff, knowledge ingest
│   ├── utils/               # Audit logger and helper utilities
│   └── main.py              # FastAPI ASGI factory, middlewares, exception handlers & Lifespan
├── alembic/                 # Database schema migrations
├── knowledge/               # Local file storage for uploaded knowledge documents
├── tests/                   # 955+ automated pytest test cases
├── init_db.py               # Superuser & 24 system permissions seeder
├── main.py                  # Windows-safe uvicorn startup entry (SelectorEventLoop)
├── pyproject.toml           # Project dependencies managed via uv
└── .env.example             # Configuration template
```

---

## Key Modules & Systems

### 1. Agent Runtime (`app/agent/`)
- **Core Loop (`loop.py`)**: General-purpose tool execution loop with step (`max_steps`) and cost (`max_cost_usd`) hard budgets.
- **Chat Turn (`chat_turn.py`)**: Root ops agent conversation orchestrator. Injects the system prompt, streams SSE tokens from the LLM, dispatches tools, and broadcasts live events over WebSocket.
- **Context Compaction (`compaction.py`)**: Automatically summarizes older conversation windows using a lightweight background LLM call when token thresholds are reached.
- **HITL Security Gate (`hitl_gate.py` & `hitl_execution.py`)**: Intercepts state-changing and sensitive commands (`notify`, `device_control`, `query_device_command`). Enforces the `PENDING → APPROVED → EXECUTING → EXECUTED / UNKNOWN` lifecycle.
- **Netmiko Device Executor (`executors.py`)**: Handles SSH/CLI automation across Cisco, Huawei, H3C, Juniper, and Linux switches and servers.
- **Sub-Agent Spawn System (`spawn.py` & `orchestration.py`)**: Manages bounded concurrent sub-agents for specialized roles (`classifier`, `investigator`, `reviewer`).
- **WebSocket Broadcast Hub (`ws_hub.py`)**: Filters sensitive action payloads and pushes safe event streams to connected clients.

### 2. Infrastructure & Monitoring
- **CMDB Asset & Dependency Management (`app/crud/cmdb_asset*.py`)**: Stores IT assets, IP addresses, vendor platform mappings, and directed dependency graphs. Static credentials are encrypted with `CMDB_CREDENTIAL_KEY` (Fernet).
- **Device Command Policy Engine (`app/crud/device_command_policy.py`)**: Evaluates whitelist/blacklist rules by asset type or asset ID.
- **TCP Liveness Prober (`app/services/monitor_sweep.py`)**: High-concurrency async TCP sweep checking port availability and latency. Broadcasts real-time state-flip alerts.
- **Knowledge Base RAG (`app/services/knowledge_ingestion.py`)**: Parses markdown/text documents, generates CJK-friendly chunks, and queries vector embeddings with PostgreSQL `pgvector`.

### 3. Authentication & Security (`app/core/` & `app/services/auth.py`)
- **Dual-Token Scheme**: Short-lived in-memory Access Tokens (15–30 min) + HttpOnly SameSite=Strict Refresh Cookies (7 days).
- **Session Family Rotation**: Detects and revokes replayed refresh tokens immediately.
- **Password Hashing**: Modern Argon2id via `pwdlib`, with automatic transparent migration from legacy Bcrypt hashes.
- **Origin Validation**: Strict CORS and CSRF origin verification on state-changing endpoints.

---

## API Routes Summary (`/api/v1`)

| Prefix | Method / Scope | Purpose | Required Permission |
| --- | --- | --- | --- |
| `/auth` | POST | Login, refresh token rotation, logout, registration | Public / Authenticated |
| `/me` | GET, PATCH, PUT | User profile, personal password change, permission list | Authenticated |
| `/users` | CRUD, trash, reset-pwd | User accounts and role assignments | `user:*` |
| `/roles` | CRUD, trash, assign | Role management and permission binding | `role:*` |
| `/permissions` | GET, CRUD | Permission tree and custom permission definitions | `permission:*` |
| `/cmdb` | CRUD, dependencies | Asset inventory, topology graph, credential preview | `cmdb:*` |
| `/device-command-policies` | CRUD, trash | Whitelist/blacklist command execution policies | `device_command_policy:*` |
| `/monitor` | GET, CRUD | Target health status, probe configuration, history logs | `monitor:*` |
| `/knowledge` | GET, POST | Category creation and document vector upload | `knowledge:*` |
| `/hitl` | GET, POST | Pending proposals, approve/reject decisions, retry | `agent:hitl_approve` |
| `/agent` | CRUD, POST | Chat session management, snapshot restore, message turns | `agent:use` |
| `/ws/agent/{session_id}` | WS | Real-time token streaming and status event broadcasts | `agent:use` |
| `/system-config` | GET, PUT | Dynamic LLM model, price rates, and monitoring settings | `system_config:*` |
| `/audit-logs` | GET | Comprehensive administrative and execution audit trail | `audit:read` |

---

## Development & Setup

### Requirements
- Python **3.14**
- [uv](https://github.com/astral-sh/uv)
- PostgreSQL **≥ 14** with `pgvector` and `pg_trgm`

### Installation & Run

```bash
cd backend
cp .env.example .env

# Install dependencies into uv virtualenv
uv sync

# Run database migrations
uv run alembic upgrade head

# Seed superuser & system permissions
uv run python init_db.py

# Start application server
uv run python main.py
```

- API Base: `http://localhost:8000/api/v1`
- Swagger UI: `http://localhost:8000/docs`

---

## Quality Assurance & Testing

The backend adheres to a strict zero-warning policy:

```bash
# Code formatting and linting
uv run ruff check .

# Strict static type checking (120+ files)
uv run mypy app

# Run complete test suite (955+ tests)
uv run pytest
```

---

## License

[MIT](../LICENSE) © lijianqiao
