# Frontend — ent-agent

[中文文档](./README_zh.md) · [Root README](../README.md)

Modern **React 19 Single Page Application (SPA)** client for the **ent-agent** platform. Built with **TypeScript**, **Vite 8**, **Tailwind CSS 4**, and **shadcn/ui (Base UI)**.

---

## Architecture Overview

See [docs/AGENT_ARCHITECTURE.md](../docs/AGENT_ARCHITECTURE.md) for full architectural contracts.

```text
frontend/
├── src/
│   ├── components/
│   │   ├── auth/             # ProtectedRoute permission & authentication guard
│   │   ├── cmdb/             # CMDB asset dialogs, vendor selectors & forms
│   │   ├── common/           # DataTable (TanStack Table), ConfirmDialog, Pagination, ErrorBoundary
│   │   ├── device-command-policies/ # Whitelist/blacklist command policy form dialogs
│   │   ├── layout/           # AppLayout, Sidebar navigation, Header with theme toggle, PageHeader
│   │   ├── monitor/          # Monitor target form dialogs and schemas
│   │   ├── ops-assistant/    # [Core] AI Chat components, Turn grouped timeline, HITL approval dialog/cards
│   │   ├── permissions/      # Permission management form dialogs
│   │   ├── roles/            # Role management and permission assignment dialogs
│   │   ├── system-config/    # LLM/Embedding model & operations configuration cards
│   │   ├── ui/               # 32 atomic shadcn/ui (Base UI) primitive components
│   │   └── users/            # User CRUD, role assignment, and password reset dialogs
│   ├── hooks/                # Custom hooks (useOpsChat, useAgentWs, useAuth, usePermission, usePaginatedQuery)
│   ├── lib/                  # Axios HTTP client, WebSocket helpers, API modules, constants, Hugeicons
│   ├── pages/                # Route view components (100% lazy-loaded with React.lazy)
│   ├── store/                # Zustand in-memory authentication state store
│   ├── types/                # Strict TypeScript contract definitions (Agent, CMDB, Auth, Monitor, etc.)
│   ├── App.tsx               # App entry point, session bootstrap, and route configuration
│   ├── index.css             # Tailwind CSS 4 theme variables and global styles
│   └── main.tsx              # React DOM mounting with ThemeProvider and Sonner Toaster
├── vite.config.ts            # Vite 8 config with development `/api` & WebSocket proxy
├── package.json              # Project dependencies and script declarations
└── tsconfig.json             # Strict TypeScript configuration
```

---

## Ops Assistant UI Architecture (`components/ops-assistant/`)

The Ops Assistant chat experience is engineered for multi-turn AI reasoning, tool execution visibility, and seamless Human-In-The-Loop safety workflows:

1. **Turn-Grouped Message Timeline (`ChatMessageList.tsx`)**:
   - Converts flat WebSocket message streams into logical question-and-answer `Turn`s.
   - Each turn consists of: (1) Collapsible user question bubble, (2) Collapsible intermediate execution trace, and (3) Assistant final Markdown answer.
   - User questions and assistant answers feature **sticky floating copy buttons** (`CopyButton.tsx`) that follow the viewport during long text scrolling.
2. **Intermediate Execution Traces (`ExecutionProcessCollapsible.tsx`)**:
   - Aggregates internal model thoughts, tool call badges, sub-agent statuses, and HITL approval states.
   - Expands automatically while generating or awaiting approval; collapses cleanly once the final answer is rendered.
3. **HITL Global Approval Modal (`HitlApprovalDialog.tsx`)**:
   - Automatically pops up centered on the screen when a `PENDING` proposal is received.
   - Integrates the official **Shadcn `InputOTP`** 6-digit credential input for dynamic-password protected assets.
   - Instantly dismisses upon decision submission without blocking background streaming.
4. **HITL Timeline Card (`HitlApprovalCard.tsx`)**:
   - Embedded record within the timeline history.
   - Supports viewing full device configuration outputs in an expandable drawer with one-click **AI Summary Recovery**.
5. **Sub-Agent Lifecycle Cards (`ChildAgentStatusCard.tsx`)**:
   - Displays real-time progress, role indicators, and completion receipts for dynamically spawned child agents.
6. **Streaming Rich Text (`ChatMarkdown.tsx`)**:
   - Custom GFM Markdown renderer with optimized styling for code blocks, tables, lists, and quotes.

---

## State Management & Networking

- **`use-ops-chat.ts`**: Core conversation state machine. Loads initial state via REST snapshot before establishing the WebSocket connection to eliminate race conditions. Uses a pure Reducer to merge streaming events.
- **`use-agent-ws.ts`**: Manages WebSocket connection lifecycle, JWT query parameter authentication, and exponential backoff auto-reconnection (1s to 30s).
- **`use-auth.ts` & `store/auth.ts`**: In-memory token storage. On page reload, `bootstrap()` transparently exchanges the HttpOnly refresh cookie for a fresh access token without user interruption.
- **`use-permission.ts`**: Granular RBAC permission checks (`hasPermission`, `hasAnyPermission`, `hasAllPermissions`). Superusers automatically bypass client-side restrictions.
- **`src/lib/api.ts`**: Central Axios client with response interceptor queueing 401 unauthorized requests and performing seamless token refresh.

---

## Available Pages & Routing

All routes are split into lightweight dynamic chunks via `React.lazy()`:

| Route Path | View Component | Description | Required Permission |
| --- | --- | --- | --- |
| `/login` | `LoginPage` | Authentication login page | Public |
| `/` | `DashboardPage` | Metrics dashboard and quick overview | Authenticated |
| `/ops-assistant` | `OpsAssistantPage` | AI operations chat assistant | Authenticated (`agent:use`) |
| `/cmdb` | `CmdbAssetsPage` | CMDB asset table & dependency topology | `cmdb:read` |
| `/cmdb/trash` | `CmdbAssetsTrashPage` | Soft-deleted asset recycling bin | `cmdb:manage` |
| `/device-command-policies` | `DeviceCommandPoliciesPage` | Whitelist/blacklist execution rules | `device_command_policy:read` |
| `/monitor-targets` | `MonitorTargetsPage` | TCP probe targets and live latency | `monitor:read` |
| `/monitor-logs` | `MonitorLogsPage` | Health probe event history logs | `monitor_log:read` |
| `/users` | `UsersPage` | User accounts and role assignments | `user:read` |
| `/roles` | `RolesPage` | Roles and permission bindings | `role:read` |
| `/permissions` | `PermissionsPage` | System permission catalog tree | `permission:read` |
| `/system-config` | `SystemConfigPage` | LLM, prices, and operations settings | `system_config:manage` |
| `/audit` | `AuditLogsPage` | Administrative audit trail logs | `audit:read` |
| `/profile` | `ProfilePage` | Personal user profile & password update | Authenticated |

---

## Getting Started

### Installation

```bash
cd frontend
cp .env.example .env

# Install dependencies
npm install  # or: pnpm install
```

### Development Server

```bash
npm run dev
```

Application runs at `http://localhost:5173`. The Vite development server automatically proxies `/api` and WebSocket connections to the backend at `http://127.0.0.1:8000`.

---

## Scripts & Quality Standards

```bash
# Start local Vite development server
npm run dev

# Run full Vitest unit test suite (134+ tests)
npm test

# Type-check TypeScript codebase
npm run typecheck

# Code style and ESLint validation
npm run lint

# Production build (chunk size optimized < 500KB)
npm run build

# Preview production build locally
npm run preview
```

---

## License

[MIT](../LICENSE) © lijianqiao
