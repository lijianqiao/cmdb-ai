# ent-agent

[中文文档](./README_zh.md)

An enterprise-grade, full-stack **AI Operations Agent & RBAC Management Platform** built with **FastAPI** and **React 19**.

Interact with your IT infrastructure through natural language: query CMDB assets, probe device health, execute network commands via Netmiko, manage whitelist/blacklist policies, and perform secure Human-In-The-Loop (HITL) change approvals.

---

## Product Demo

![Product demo](./docs/images/product_demo.gif)

A walkthrough of one full operator session:

1. Superuser login and the dashboard overview;
2. CMDB asset management and dependency topology;
3. Device command policy configuration (whitelist bypass / blacklist block);
4. Ops Assistant conversation:
   - Querying a switch running-config — the agent picks the tool, executes, and returns an AI-generated summary;
   - Attempting a device reboot — **blocked by the approval gate**, with the proposal left `PENDING` for a human.

---

## Key Features

- **🤖 Ops Assistant (AI Agent)**:
  - Natural language troubleshooting, CMDB lookups, TCP port probes, and device configuration inspection.
  - Multi-vendor network automation via Netmiko (Cisco IOS-XE, Cisco Small Business, Huawei VRP, HP Comware, Juniper Junos, Linux).
  - Streaming responses over WebSocket with Turn-grouped execution processes, collapsible thinking traces, and sticky copy buttons.
  - Cancel a running turn mid-flight; token usage and per-reply cost shown under every answer.
  - Three configurable model tiers (cheap / balanced / strong) with wholesale fallback when a tier is unset.
- **🛡️ Human-In-The-Loop (HITL) Security**:
  - Strict approval gate for state-changing/sensitive actions with `PENDING → APPROVED → EXECUTING → EXECUTED / UNKNOWN` state machine.
  - Global auto-popup modal with 6-digit `InputOTP` credential input for dynamic-password protected assets.
  - Idempotent AI configuration summarization for large device command outputs.
- **🗄️ CMDB & Credential Management**:
  - Unified inventory for network switches, routers, and servers with subnet CIDR and topological dependency graphs.
  - Fernet symmetric encryption (`CMDB_CREDENTIAL_KEY`) for static passwords, plus support for dynamic one-time credentials.
- **⚡ Device Command Policies**:
  - Fine-grained whitelist and blacklist rules by asset type or specific device to bypass or enforce approval workflows.
- **📡 TCP Liveness Monitoring**:
  - Async concurrent TCP probing, latency tracking, state-flip alert broadcast, and historical event logs.
  - Status-page style uptime strip: the last hour at one bar per minute, plus a time-weighted uptime rate — delivered in the same list request, no per-row follow-up calls.
- **📚 Knowledge Base & RAG**:
  - Document upload (`.md`, `.txt`), intelligent CJK text chunking, and vector similarity search powered by `pgvector`.
  - In-app document preview, AI-suggested categorisation, and delete with a restorable recycle bin — deletion removes the file from both retrieval paths (vector search *and* the ripgrep file scan) at once.
- **🎯 Agent Eval Suite**:
  - 10 fixed cases run against the **real** model, scored on three layers (outcome / trajectory invariants / efficiency).
  - Safety cases are a hard red line; capability cases are judged on an aggregate success rate against a committed baseline — because a real model flips individual answers on its own. See [docs/EVAL.md](./docs/EVAL.md).
- **🔐 Enterprise RBAC & Security**:
  - Users ↔ Roles ↔ Permissions with module grouping and soft deletion/trash bin support.
  - Dual-token authentication: in-memory short-lived Access Token + HttpOnly Refresh Cookie with session-family rotation and replay protection.
- **⚙️ Dynamic Runtime Configuration**:
  - Database-backed LLM/Embedding provider settings and monitoring parameters with secret masking and `.env` fallback.

---

## Tech Stack

| Layer | Stack |
| --- | --- |
| **Backend** | Python 3.14, FastAPI, SQLAlchemy 2 (async), PostgreSQL + pgvector, Alembic, Netmiko, JWT, uv |
| **Frontend** | React 19, TypeScript, Vite 8, Tailwind CSS 4, shadcn/ui (Base UI), Zustand, React Router 7 |
| **Quality** | Ruff, mypy (strict), pytest (1120 tests) / ESLint, Prettier, Vitest (169 tests) |

---

## Repository Layout

```text
ent-agent/
├── backend/          # FastAPI API service (see backend/README.md)
│   └── evals/        # Agent eval suite — 10 cases against the real model
├── frontend/         # React SPA client (see frontend/README.md)
├── docs/             # PRD, architecture, deployment, eval design, diagrams
├── README.md         # English documentation (this file)
└── README_zh.md      # Chinese documentation
```

---

## Quick start (Docker, recommended)

The whole stack — PostgreSQL, backend, frontend — runs in containers; Docker is the only prerequisite.

```bash
cp backend/.env.example backend/.env   # model credentials, INIT_SUPERUSER_*, ...
docker compose up -d --build
```

Open <http://localhost:8090>. The frontend and the API are **same-origin**: nginx proxies `/api`
to the backend, so no backend address is baked into the build and one image works on any domain.

On start the backend runs `alembic upgrade head` and an idempotent seed pass — no manual step.

```bash
docker compose logs -f backend   # migrations, seeding and runtime logs
docker compose down              # stop (volumes kept)
docker compose down -v           # also delete data — use with care
```

Deliberate choices:

- **Port 8090, not 8080** — `LLM_CHAT_BASE_URL` defaults to a local model server on `127.0.0.1:8080`.
  Changing this port means changing `BACKEND_CORS_ORIGINS` in the same file: `/auth/login` matches the
  browser's `Origin` against that list and answers `403 请求来源不受信任` on a mismatch, which the login
  form reports as a wrong password.
- **The backend publishes no host port** — reachable only through nginx, so there is never a second API origin.
- **ripgrep is installed in the image** — `kb_grep` shells out to `rg`; without it knowledge-base search fails at runtime.
- **Exactly one worker** — the agent spawn runtime keeps in-process state, so `WEB_CONCURRENCY` must be 1.
- Knowledge-base files and the trash live on named volumes, so rebuilding images never drops uploaded documents.

## Quick start (local)

Better when you are editing code and want hot reload.

**Prerequisites**

- **Python 3.14** & [uv](https://github.com/astral-sh/uv)
- **Node.js 24** (npm)
- **PostgreSQL ≥ 14** with the `pgvector` and `pg_trgm` extensions
  (`docker compose up -d postgres` starts just the database, extensions included)
- **ripgrep** — `kb_grep` shells out to `rg`
  (`winget install BurntSushi.ripgrep.MSVC` / `brew install ripgrep` / `apt install ripgrep`)
- A local embedding engine (e.g. llama.cpp) or any OpenAI-compatible API key

```bash
# backend
cd backend
cp .env.example .env      # set DATABASE_URL, CMDB_CREDENTIAL_KEY, LLM_CHAT_* / LLM_EMBEDDING_*
uv sync
uv run alembic upgrade head
uv run python init_db.py
uv run uvicorn app.main:app --reload

# frontend (second terminal)
cd frontend
npm install
npm run dev
```

| | |
| --- | --- |
| Web app | `http://localhost:5173` |
| API base | `http://localhost:8000/api/v1` |
| OpenAPI docs | `http://localhost:8000/docs` |

Sign in with the administrator account created by `init_db.py` (`admin` / `admin123`).
**Change it before exposing the instance to anyone else.**

> On Windows start the backend via `uv run python main.py` instead — it selects the
> Selector event loop that async psycopg requires.

---

## Documentation

| Document | Description |
| --- | --- |
| [docs/AGENT_ARCHITECTURE.md](./docs/AGENT_ARCHITECTURE.md) | Agent platform architecture & WS contracts |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) | System layering & database architecture |
| [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) | Production deployment & security hardening |
| [docs/SYSTEM_CONFIG.md](./docs/SYSTEM_CONFIG.md) | Runtime configuration & key management |
| [docs/EVAL.md](./docs/EVAL.md) | Agent eval suite — regression testing against the real model |
| [backend/README.md](./backend/README.md) | Backend service guide ([中文](./backend/README_zh.md)) |
| [frontend/README.md](./frontend/README.md) | Frontend client guide ([中文](./frontend/README_zh.md)) |

---

## License

[MIT](./LICENSE) © lijianqiao
