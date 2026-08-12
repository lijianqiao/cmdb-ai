# Frontend — fastapi-admin

[中文文档](./README_zh.md) · [Root README](../README.md)

React SPA for the RBAC admin console: login, dashboard, users/roles/permissions, audit logs, profile, plus the **Ops Assistant** chat (sessions, WebSocket events, HITL approval cards, knowledge upload).

![Dashboard](../docs/images/dashboard.png)

Architecture & WS contract: [docs/AGENT_ARCHITECTURE.md](../docs/AGENT_ARCHITECTURE.md).

## Stack

- React **19** + TypeScript + Vite **8**
- Tailwind CSS **4** + shadcn/ui (**Base UI** / `@base-ui/react`)
- Hugeicons (via `@/lib/icons`), TanStack Table, React Hook Form + Zod
- Zustand auth store, React Router **7**, Axios
- Vitest (pure helpers: WS envelope / chat reducer)
- Session restore via refresh cookie on app bootstrap

## Project layout

```text
frontend/
├── src/
│   ├── components/
│   │   ├── layout/           # Sidebar, PageHeader
│   │   ├── ops-assistant/    # Chat UI, HITL card, knowledge upload, monitor banner
│   │   └── ui/               # shadcn primitives
│   ├── hooks/                # useAuth, usePermission, useAgentWs, useOpsChat
│   ├── lib/                  # api, agent-api, agent-ws, hitl-api, knowledge-api, constants, icons
│   ├── pages/                # includes OpsAssistantPage (/ops-assistant)
│   ├── store/                # Zustand
│   └── types/
├── vite.config.ts            # Dev proxy /api (incl. WS) → backend
├── .env.example
└── package.json
```

## Setup

```bash
cd frontend
cp .env.example .env
pnpm install   # or: npm install
```

`.env`:

```ini
VITE_API_BASE_URL=http://localhost:8000/api/v1
```

Ensure the backend is running and CORS includes `http://localhost:5173`.

In dev, `vite.config.ts` proxies `/api` (including WebSocket upgrades) to `http://127.0.0.1:8000`, so the browser can use same-origin WS paths.

## Scripts

```bash
pnpm run dev         # http://localhost:5173
pnpm run build       # tsc -b && vite build
pnpm run typecheck   # tsc -b --force
pnpm run test        # vitest run
pnpm run lint
pnpm run preview
```

## Ops Assistant (`/ops-assistant`)

- Sidebar entry: visible to any logged-in user (no extra permission code)
- Session REST: `/agent/sessions`; send via `POST .../messages` (~300s timeout for a full Agent turn)
- WebSocket: `/api/v1/ws/agent/{session_id}?access_token=...` carries `assistant_delta` / `tool_call` / `hitl_*` / `monitor_alert` / `error`; exponential reconnect (cap 30s); does not auto-replay an in-flight turn after reconnect
- **HITL**: WS carries a safe summary only; with `agent:hitl_approve`, HTTP fetches the full payload and calls `/hitl/proposals/{id}/decide`
- **Knowledge upload**: button gated by `knowledge:upload`; category list needs `knowledge:read` (upload-only users get a clear toast/hint, not a blank dialog)

Implementation: `pages/OpsAssistantPage.tsx`, `hooks/use-ops-chat.ts`, `hooks/use-agent-ws.ts`, `components/ops-assistant/*`.

## Auth & permissions (UI)

- Access token is kept in memory; refresh token lives in an HttpOnly cookie
- On startup, `bootstrap()` refreshes once (single-flight) then loads `/me`
- Menu items and action buttons use `PERMISSIONS` / `ROUTES` from `src/lib/constants.ts`
- Superusers bypass permission checks in the UI; API still enforces rules server-side

Permission codes match the backend seed (including `knowledge:*`, `cmdb:*`, `monitor:*`, `agent:hitl_approve`).

## Screenshots

See the [root README](../README.md#screenshots) gallery (`docs/images/`).

## Notes

- Prefer Base UI patterns from the installed shadcn components (`render=` instead of Radix `asChild` where applicable)
- Forms should use the registry `field` components so validation messages surface correctly
- Icons go through `@/lib/icons` — do not import `@hugeicons/...` ad hoc in pages
- Use semantic color/typography tokens; prefer flex + gap layouts over decorative card stacks
