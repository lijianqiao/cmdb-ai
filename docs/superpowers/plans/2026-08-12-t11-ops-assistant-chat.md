# T11 · 前端 Chat 页面（OpsAssistant）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付单一运维助手 Chat 页面：会话 REST + `/api/v1/ws/agent/{session_id}` 实时通道、流式消息 UI、内嵌 `HitlApprovalCard`、权限门控的 `KnowledgeUploadDialog`，并接上现有 `run_loop` / HITL publisher / 知识库上传 API。

**Architecture:** T06–T10 已有内核（`run_loop`、`AgentSession`/`AgentMessage` CRUD、root dispatcher、`HitlEventPublisher` Protocol、HITL HTTP、知识库上传）。T11 补齐**缺失的传输层**（会话 HTTP + WebSocket hub + 把 `NoopHitlEventPublisher` 换成可推送的 WS publisher）和**前端页面**。前端不引入独立 AI Elements 包：用现有 shadcn/base-luma 组件（Card / ScrollArea / Badge / Button / InputGroup / Field / Dialog / Empty / Skeleton / Spinner / Alert / sonner）按当前后台风格拼装。一条 WS 连接承载 `assistant_delta` / `tool_call` / `hitl_*` / `monitor_alert` / `error`；审批卡片对有 `agent:hitl_approve` 的用户再 HTTP 拉完整 `action_payload`（WS 只推安全摘要，与 T10 一致）。

**Tech Stack:** FastAPI WebSocket、现有 JWT（`decode_token`）、React 19 + React Router 7、axios `api`、zustand auth store、shadcn base-luma + hugeicons、sonner、可选 vitest（仅测纯函数）。Python 侧继续 `uv run`；前端用 `npm`。

**Spec:** [docs/AGENT_ARCHITECTURE.md](../../AGENT_ARCHITECTURE.md) §8（WS 契约与页面组件）、§13 T11 行、§11 A5（断线重连在本计划落地）。依赖 T06 数据模型 + T10 HITL API + T07 知识库上传。

**UI / shadcn 约束（强制）：**

- 风格：`base-luma` + `mist` + hugeicons；图标一律经 `@/lib/icons` 导出，禁止直接散落 `@hugeicons/core-free-icons` 新用法而不走适配层。
- 布局间距用 `flex` + `gap-*`，不用 `space-y-*` / `space-x-*`。
- 表单用 `FieldGroup` + `Field` + `data-invalid` / `aria-invalid`（对齐 `UserFormDialog`）。
- 反馈用 `toast`（sonner）、空态用 `Empty`、加载用 `Skeleton`/`Spinner`、状态用 `Badge`/`Alert`。
- `className` 只做布局；颜色/字体走语义 token（`bg-background`、`text-muted-foreground`、`bg-card` 等）。
- 复用公共能力：`api`、`useAuth`、`usePermission`、`ProtectedRoute`、`PageHeader`、`cn`、`PERMISSIONS`/`ROUTES`、`ConfirmDialog`（危险确认时）。

---

## Global Constraints

- 工作直接在 `master`；中文 commit（UTF-8 文件 + `git commit -F`）；禁止 `Co-Authored-By`。
- 后端：`cd backend && uv run …`；不随意 `pip install`；新依赖用 `uv add`（本计划默认**不**加新 Python 包，FastAPI 已含 WebSocket）。
- 前端：新依赖用 `npm install`（若加 vitest）；shadcn 组件优先用已安装列表，缺什么再 `npx shadcn@latest add <name>`（先 `docs` 再装）。
- TDD：后端 pytest 先红后绿；前端对 WS 消息解析/reducer 用 vitest（Task 5 引入），页面以 `npm run typecheck` + 手工验收为主。
- **不做**真实 LLM token 流改造：`llm.chat` / `run_loop` 仍是整段返回。WS 的 `assistant_delta` 按「每次模型回复落库后推送一段（可再切成 UI 伪流式 chunk）」实现；契约类型名保留，便于日后换真流式。
- HITL WS payload **禁止**含原始 `action_payload` 敏感字段；审批人详情走既有 `GET /hitl/proposals/{id}`。
- 会话隔离：只能读写/连接**自己的** `AgentSession`（`user_id == current_user.id`）。
- Chat 页面对登录用户开放（与仪表盘同级，无新权限码）；上传入口 `knowledge:upload`；HITL 操作按钮 `agent:hitl_approve`。
- 明确 A5 重连：指数退避（1s → 2s → 4s … 上限 30s），最多连续重试可配置（默认不封顶但 UI 显示「重连中」）；恢复后不自动重放进行中的 turn（用户可再发或刷新历史）。
- 全量验收：后端 `uv run pytest -v` + mypy + ruff + alembic heads；前端 `npm run typecheck`（+ vitest）。
- 不新建 Alembic migration（表已齐）。

## File Map

| File | Responsibility |
| :--- | :--- |
| `backend/app/agent/ws_hub.py` | 进程内 session→WebSocket 连接表；`broadcast(session_id, envelope)`；实现 `HitlEventPublisher`。 |
| `backend/app/api/v1/agent_ws.py` | `WS /ws/agent/{session_id}`：默认 query `access_token` 鉴权（可选首帧 auth），归属校验，注册/注销连接。 |
| `backend/app/schemas/agent_ws.py` | 判别式消息 envelope Pydantic 模型（server→client / client→server）。 |
| `backend/app/schemas/agent_session.py` | 会话 REST DTO。 |
| `backend/app/api/v1/agent_sessions.py` | `POST/GET` 会话、`GET …/messages`、`POST …/messages`（发用户消息并触发 turn）。 |
| `backend/app/agent/chat_turn.py` | 编排：append user → 包装 `chat_fn`/`dispatch_tool` 推 WS → `run_loop`；根 `system_prompt` 常量。 |
| `backend/app/agent/loop.py` | **可选最小改动**：仅当包装不够时，增加可选 `on_event` 回调；默认不改语义。优先不改此文件。 |
| `backend/app/api/v1/hitl.py` | decide/resume 注入 `WsHitlEventPublisher`（替换默认 noop）。 |
| `backend/app/api/router.py` | include sessions + ws 路由。 |
| `backend/app/main.py` | 如需把 WS 挂到 app（若走 api_router 则不必改）。 |
| `frontend/vite.config.ts` | dev proxy：`/api` + `/api/v1/ws` → backend（支持 WS upgrade）。 |
| `frontend/src/lib/constants.ts` | `ROUTES.OPS_ASSISTANT`。 |
| `frontend/src/types/agent.ts` | 会话/消息/WS envelope 类型。 |
| `frontend/src/lib/agent-api.ts` | 会话 REST 封装（走 `api`）。 |
| `frontend/src/lib/agent-ws.ts` | URL 构造、envelope 解析、reconnect 策略纯函数。 |
| `frontend/src/hooks/use-agent-ws.ts` | WS 生命周期 hook。 |
| `frontend/src/hooks/use-ops-chat.ts` | 页面状态：历史加载、发送、合并 WS 事件。 |
| `frontend/src/components/ops-assistant/*` | `ChatMessageList`、`ChatInput`、`HitlApprovalCard`、`KnowledgeUploadDialog`、`MonitorAlertBanner`。 |
| `frontend/src/pages/OpsAssistantPage.tsx` | 页面组装。 |
| `frontend/src/App.tsx` / `Sidebar.tsx` / `icons.tsx` | 路由与导航入口。 |
| `backend/tests/test_agent_ws*.py` / `test_agent_sessions_api.py` / `test_chat_turn.py` | 后端契约与归属测试。 |
| `frontend/src/lib/agent-ws.test.ts` | 解析/重连退避单测（vitest）。 |

## Explicitly Out of Scope

- 改造 `llm.chat` 为真正的 token streaming。
- 子 Agent 可视化树 / spawn 调试 UI。
- CMDB/监控管理 CRUD 页面（仅消费 `monitor_alert` 事件展示）。
- 多标签多会话并排复杂 UX（本计划：左侧简易会话列表 + 主聊天区即可）。
- 修改 T10 状态机语义；只接线 publisher。
- 新权限码、新 migration、强制引入第三方「AI chat」组件库。

---

### Task 1: WebSocket 消息契约 + 连接 Hub

**Files:**
- Create: `backend/app/schemas/agent_ws.py`
- Create: `backend/app/agent/ws_hub.py`
- Create: `backend/tests/test_agent_ws_hub.py`

**Interfaces:**

```python
# schemas/agent_ws.py
from typing import Any, Literal
from pydantic import BaseModel, Field

AgentWsEventType = Literal[
    "assistant_delta",
    "tool_call",
    "hitl_pending",
    "hitl_resolved",
    "hitl_execution_failed",
    "monitor_alert",
    "error",
    "turn_done",
]

class AgentWsServerMessage(BaseModel):
    type: AgentWsEventType
    payload: dict[str, Any] = Field(default_factory=dict)

class AgentWsClientAuth(BaseModel):
    type: Literal["auth"]
    access_token: str

# Hub
class AgentWsHub:
    async def connect(self, session_id: int, websocket: WebSocket) -> None: ...
    async def disconnect(self, session_id: int, websocket: WebSocket) -> None: ...
    async def broadcast(self, session_id: int, message: AgentWsServerMessage) -> None: ...

class WsHitlEventPublisher:
    """HitlEventPublisher：把 hitl_* 事件映射为 AgentWsServerMessage 并 broadcast。"""
    async def publish(self, *, session_id: int, event_type: str, payload: Mapping[str, object]) -> None: ...

hub = AgentWsHub()  # 模块单例，供 API / chat_turn / HITL 注入
```

安全摘要字段白名单（与 T10 `ProposalSafeSummary` 对齐）：`proposal_id` / `action_type` / `status` / `reason` / `asset_id`。`hitl_pending` / `hitl_resolved` / `hitl_execution_failed` 的 payload 不得出现原始动作载荷键（如 `message`/`command`/`password`）。

- [x] **Step 1: 写失败测试** — hub 广播只到达同 `session_id` 的连接；publisher 映射 `hitl_pending`；payload 不含敏感键。

- [x] **Step 2: 跑测确认失败**

```bash
cd backend
uv run pytest tests/test_agent_ws_hub.py -v
```

Expected: FAIL（模块不存在）

- [x] **Step 3: 最小实现** hub + schema + `WsHitlEventPublisher`

- [x] **Step 4: 绿**

```bash
uv run pytest tests/test_agent_ws_hub.py -v
```

Expected: PASS

- [x] **Step 5: Commit**（UTF-8 `-F`）

```text
新增 Agent WebSocket Hub 与 HITL 事件发布器

- 进程内按 session_id 管理连接并广播判别式 JSON
- WsHitlEventPublisher 实现 T10 Protocol，只推安全摘要
- 为后续会话 API / Chat 页提供单一实时通道
```

---

### Task 2: WebSocket 路由鉴权（归属 + token）

**Files:**
- Create: `backend/app/api/v1/agent_ws.py`
- Modify: `backend/app/api/router.py`
- Create: `backend/tests/test_agent_ws_api.py`

**鉴权约定（默认 + 可选）：**

1. **默认（必须实现并作为前端路径）**：连接 URL `?access_token=<jwt>`（浏览器 `new WebSocket(url)` 最直接）。
2. **可选兼容（架构 §8「首帧校验」）**：若 query 无 token，则等待首帧 JSON `{"type":"auth","access_token":"..."}`（例如 5s 超时未认证则关闭）。单测至少覆盖默认路径；首帧路径有一条测试即可。

校验步骤：`decode_token` → 加载用户 → `agent_session_crud.get` → `session.user_id == user.id` 且 `status == "active"`；失败码用 WS close code（如 4401/4403/4404）并写中文 reason。

路由挂载：`api_router.include_router(..., prefix="/ws")`，最终路径 **`/api/v1/ws/agent/{session_id}`**（与架构一致；注意 `settings.API_V1_PREFIX`）。

- [x] **Step 1: 失败测试** — 无 token / 错 token / 非所有者 / 成功连接后 hub 可收到 broadcast。

用 `httpx.AsyncClient` + Starlette/FastAPI `TestClient` websocket 或 pytest-anyio 已有模式；参考项目现有 API 测试 fixture。

- [x] **Step 2: 实现路由并挂到 `router.py`**

- [x] **Step 3: 绿 + Commit**

```text
新增 Agent WebSocket 鉴权路由

- 复用 decode_token 与会话归属校验
- 连接注册到 AgentWsHub，断线自动注销
```

---

### Task 3: 会话 REST API（创建 / 列表 / 历史）

**Files:**
- Create: `backend/app/schemas/agent_session.py`
- Create: `backend/app/api/v1/agent_sessions.py`
- Modify: `backend/app/api/router.py`
- Modify: `backend/app/crud/agent_message.py`（若缺 `list_for_session` 则补；已有则复用）
- Create: `backend/tests/test_agent_sessions_api.py`

**Endpoints（均需登录 `get_current_user`）：**

| Method | Path | 行为 |
| :--- | :--- | :--- |
| `POST` | `/agent/sessions` | body 可选 `{ "title": str }`；创建 `user_id=current_user.id` |
| `GET` | `/agent/sessions` | 当前用户会话列表（复用 `list_for_user`） |
| `GET` | `/agent/sessions/{id}` | 详情；非所有者 404 |
| `GET` | `/agent/sessions/{id}/messages` | 按 id 升序历史；**优先复用**已有 `agent_message_crud.list_for_agent(db, session_id, agent_id=None)`（根 transcript）。返回 `role/content/tool_calls/created_at` 等已落库字段；tool 行 content 应为工具层已写的安全文本（T10 已保证 HITL 不回显密钥），API 不再二次清洗除非发现泄漏 |

响应包络复用 `success_response` / `ResponseEnvelope`。

- [x] **Step 1–4: TDD 实现**

- [x] **Step 5: Commit**

```text
新增 Agent 会话 REST API

- 登录用户可创建/列出自己的会话并拉取消息历史
- 非所有者访问统一 404，避免会话枚举
```

---

### Task 4: Chat turn 编排（发消息 → run_loop → WS 推送）

**Files:**
- Create: `backend/app/agent/chat_turn.py`
- Modify: `backend/app/api/v1/agent_sessions.py`（`POST /{id}/messages`）
- Modify: `backend/app/api/v1/hitl.py`（decide/resume 传入 `WsHitlEventPublisher()`）
- Create: `backend/tests/test_chat_turn.py`

**`POST /agent/sessions/{id}/messages` body:**

```json
{ "content": "用户问题" }
```

**算法：**

1. 校验会话归属。
2. `append_user_message(db, session_id, content)`。
3. 构造 `publisher = WsHitlEventPublisher()`；`base_dispatch = build_root_tool_dispatcher(db, session_id=..., actor_user_id=current_user.id, publisher=publisher)`；`tools = root_tool_schemas()`。
4. **中途 WS 推送（强制做法，避免卡在「run_loop 无钩子」）：**
   - **优先（不改 `loop.py`）**：在 `chat_turn.py` 包装传入 `run_loop` 的依赖：
     - `chat_fn` 包装：在真实/注入的 `chat_fn` 返回后，若 `result.tool_calls` 非空，对每个 call `broadcast(tool_call)`（payload 仅 `id`/`name`，**省略 arguments**）；若无 tool_calls 且有 `content`，`broadcast(assistant_delta)`（可再按 40–80 字切伪流式 chunk，最后一片 `done: true`）。
     - `dispatch_tool` 包装：调用 `base_dispatch` 前后不回传敏感内容；若 `control == "pending_approval"`，HITL 事件仍由 dispatcher 内 publisher 发出（不要在包装层重复解析 payload）。
   - **仅当包装无法覆盖**（例如必须在 `append_assistant_message` 之后、同一事务可见性有要求）时：给 `run_loop` 增加**可选** `on_event: Callable | None = None`，默认 `None` 保持旧行为；并更新 `loop.py` 单测。File Map 已允许这一最小改动。
5. 调用 `run_loop(..., chat_fn=wrapped_chat, dispatch_tool=wrapped_dispatch, system_prompt=ROOT_OPS_SYSTEM_PROMPT)`。
   - `ROOT_OPS_SYSTEM_PROMPT`：在 `chat_turn.py`（或 `roles.py` 根常量）写一段中文根指令（运维助手身份、工具契约、HITL 等待审批时停止杜撰执行结果）；架构 §8「根指令每轮从代码注入」。
   - `model_key`：读 settings 已有默认 Agent/chat 模型键；若无专用项则用现有 `MODELS` 默认键并在报告写明。
6. loop 正常或 early_exit 返回后：再 `broadcast(turn_done)`（`reason` / `control`）。异常：`broadcast(error)`（中文 message，无堆栈）后仍尽量 commit 已写入的用户消息。
7. `await db.commit()` 一次（与知识库/HITL 相同事务习惯）。
8. HITL HTTP decide：把 `publisher=WsHitlEventPublisher()` 传入 `decide_proposal` / `resume_proposal`。

**测试策略：** mock `chat_fn`（先返回带 tool_calls，再返回最终文本）/ dispatcher，断言 WS hub 收到 `tool_call` → `assistant_delta` → `turn_done` 顺序；`pending_approval` 时有 `hitl_pending` 且无敏感载荷。

- [x] **Step 1–4: TDD**

- [x] **Step 5: Commit**

```text
打通会话发消息与 WebSocket 推送编排

- POST messages 复用 run_loop 与 root dispatcher
- HITL 审批路径注入 WsHitlEventPublisher
- assistant_delta/tool_call/turn_done/error 事件可测
```

---

### Task 5: 前端类型、API 封装、Vite WS 代理、vitest

**Files:**
- Modify: `frontend/vite.config.ts`
- Modify: `frontend/src/lib/constants.ts`（`ROUTES.OPS_ASSISTANT = "/ops-assistant"`）
- Create: `frontend/src/types/agent.ts`
- Create: `frontend/src/lib/agent-api.ts`
- Create: `frontend/src/lib/agent-ws.ts`
- Create: `frontend/src/lib/agent-ws.test.ts`
- Modify: `frontend/package.json`（devDependency `vitest`；script `"test": "vitest run"`）
- Modify: `frontend/src/lib/api.ts`（导出 `getAccessToken()`；**不要**改全局默认 30s timeout——只在会话发消息请求上覆盖）

**Vite proxy 示例：**

```ts
server: {
  proxy: {
    "/api": {
      target: "http://127.0.0.1:8000",
      changeOrigin: true,
      ws: true,
    },
  },
},
```

（若后端端口不同，与现有 `.env` / 文档一致。）

**`agent-ws.ts` 纯函数：**

- `buildAgentWsUrl(sessionId, accessToken)`
- `parseAgentWsMessage(raw: string): AgentWsServerMessage | null`
- `nextReconnectDelay(attempt: number): number`（指数退避，封顶 30_000）

- [x] **Step 1: 安装 vitest 并写失败单测**

```bash
cd frontend
npm install -D vitest
npm run test -- src/lib/agent-ws.test.ts
```

- [x] **Step 2: 实现纯函数与 API 封装**（`agent-api` 只用 `@/lib/api`，不要新建 axios 实例）

**发消息超时（必须）：** 全局 `api` 默认 `timeout: 30000` 对整轮 Agent turn 不够。`POST /agent/sessions/{id}/messages` 必须在该次请求上覆盖更长超时，例如：

```ts
api.post(`/agent/sessions/${id}/messages`, body, { timeout: 300_000 })
```

（5 分钟或 `0` 表示无超时，择一并在注释说明。）否则 WS 已推完，HTTP 仍因 30s 失败，手工验收会误判。

- [x] **Step 3: typecheck + vitest 绿**

```bash
npm run typecheck
npm run test
```

- [x] **Step 4: Commit**

```text
新增运维助手前端 API/WS 基础与开发代理

- Vite 代理开启 WebSocket upgrade
- 复用 axios api 封装会话 REST
- vitest 覆盖 WS 解析与重连退避
```

---

### Task 6: `useAgentWs` + `useOpsChat` hooks

**Files:**
- Create: `frontend/src/hooks/use-agent-ws.ts`
- Create: `frontend/src/hooks/use-ops-chat.ts`

**行为：**

- `useAgentWs({ sessionId, enabled })`：从 `useAuthStore` / `getAccessToken` 取 token（与 `api.ts` 同源；若 token 仅在模块变量，导出 `getAccessToken()` 只读函数——**优先扩展现有 `api.ts` 的 getter，不复制 token 状态**）。
- 连接、心跳（可选：浏览器层靠 reconnect；不必自造 ping 除非后端要求）。
- onmessage → 回调；断线按 `nextReconnectDelay` 重连。
- `useOpsChat`：加载历史 → 本地 messages 状态 → 发送（POST messages）→ 合并 `assistant_delta` / `tool_call` / `hitl_*` / `error` / `turn_done`；发送中禁用输入。

- [x] **Step 1: 实现 hooks（可对 reducer 抽纯函数并测）**

- [x] **Step 2: `npm run typecheck`**

- [x] **Step 3: Commit**

```text
新增运维助手 WebSocket 与会话状态 hooks

- 断线指数退避重连（落实架构 A5）
- 历史 REST 与实时事件合并为单一消息列表
```

---

### Task 7: Chat UI 组件 + OpsAssistantPage + 导航

**Files:**
- Create: `frontend/src/components/ops-assistant/ChatMessageList.tsx`
- Create: `frontend/src/components/ops-assistant/ChatInput.tsx`
- Create: `frontend/src/components/ops-assistant/MonitorAlertBanner.tsx`
- Create: `frontend/src/pages/OpsAssistantPage.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/layout/Sidebar.tsx`
- Modify: `frontend/src/lib/icons.tsx`（增加聊天相关图标，经 `makeIcon`）

**UI 结构（一页一职责）：**

```text
PageHeader「运维助手」+ actions（新建会话、上传知识库入口）
┌─────────────┬──────────────────────────────────────┐
│ 会话列表     │  ChatMessageList (ScrollArea)         │
│ (Button/    │  - user / assistant bubbles            │
│  Empty)     │  - tool_call → Badge 行                │
│             │  - HitlApprovalCard 插槽               │
│             │  MonitorAlertBanner (Alert)            │
│             │  ChatInput (InputGroup + Textarea)    │
└─────────────┴──────────────────────────────────────┘
```

样式要点：

- 消息区 `bg-background`，气泡用 `bg-card` / `bg-muted`，不要紫渐变或装饰卡片堆叠。
- `ChatInput`：`InputGroup` + 发送 `Button`（`data-icon`）；Enter 发送、Shift+Enter 换行。
- 空会话：`Empty` 组件。
- 侧栏新增「运维助手」项（无 permission，登录可见），放在仪表盘附近。

- [x] **Step 1: 实现组件与页面、接线路由**

- [x] **Step 2: `npm run typecheck`**

- [x] **Step 3: Commit**

```text
新增运维助手 Chat 页面与导航入口

- 复用 PageHeader/ScrollArea/InputGroup/Empty 对齐现有后台风格
- 侧栏与路由挂载 /ops-assistant
```

---

### Task 8: HitlApprovalCard（权限门控 + 现有 HITL API）

**Files:**
- Create: `frontend/src/components/ops-assistant/HitlApprovalCard.tsx`
- Create: `frontend/src/lib/hitl-api.ts`（`list/get/decide` 封装，走 `api`）

**行为：**

1. 消息流出现 `hitl_pending`（安全摘要）时渲染 Card。
2. `usePermission().hasPermission(PERMISSIONS.AGENT_HITL_APPROVE)` 为真时：`GET /hitl/proposals/{id}` 拉完整 payload，展示 action_type / asset_id / reason / payload 字段；提供「批准」「拒绝」按钮（拒绝可用 `ConfirmDialog` 或 `AlertDialog`）。
3. 无权限：只显示「等待审批」摘要，不展示敏感 payload，无按钮。
4. 调用 `POST /hitl/proposals/{id}/decide`；成功后依赖 WS `hitl_resolved` / `hitl_execution_failed` 更新卡片状态；失败 `toast.error`。
5. `device_control` stub 失败保持「已批准但未执行」文案（与 T10 一致）。

- [x] **Step 1–3: 实现 + typecheck + Commit**

```text
新增 HITL 审批卡片并接入现有审批 API

- WS 只消费安全摘要；完整载荷经权限门控 HTTP 拉取
- 批准/拒绝复用 /hitl/proposals/{id}/decide
```

---

### Task 9: KnowledgeUploadDialog

**Files:**
- Create: `frontend/src/components/ops-assistant/KnowledgeUploadDialog.tsx`
- Create: `frontend/src/lib/knowledge-api.ts`（`listCategories`、`uploadDocument` multipart）

**行为：**

- 入口按钮仅当 `hasPermission(PERMISSIONS.KNOWLEDGE_UPLOAD)` 显示（PageHeader actions）。
- Dialog + `FieldGroup`：分类（Select，先 `GET /knowledge/categories`）、标题 Input、文件 Input。
- **权限注意：** 列分类接口要求 `knowledge:read`。若用户仅有 `upload` 无 `read`，打开 Dialog 时 categories 会 403——UI 需 `toast`/`FieldDescription` 提示「需要知识库查看权限才能选择分类」，不要白屏。
- `FormData`: `category_code` / `title` / `file` → `POST /knowledge/documents`（与后端 `upload_document` 一致；路径以实际 router 为准，实现前再 `rg` 确认）。
- 成功 `toast.success` 并关闭；失败展示中文错误。

- [x] **Step 1–3: 实现 + typecheck + Commit**

```text
新增知识库上传对话框（权限门控）

- 复用 Field/Dialog 模式与 knowledge:upload 权限
- multipart 调用既有上传 API，不新建后端接口
```

---

### Task 10: 跨层验收与文档勾选

**Files:**
- Create: `backend/tests/test_ops_assistant_integration.py`（可选但推荐：创建会话 → mock turn → WS 事件；或 API 级串联）
- Modify: 本计划文件勾选 Tasks
- 手动核对清单写入 commit message body

**Verify：**

```bash
cd backend
uv run pytest tests/test_agent_ws_hub.py tests/test_agent_ws_api.py tests/test_agent_sessions_api.py tests/test_chat_turn.py tests/test_hitl_api.py -v
uv run pytest -v
uv run mypy app
uv run ruff check .
uv run alembic heads

cd ../frontend
npm run test
npm run typecheck
```

**手工验收（实现者在报告中勾选）：**

- [ ] 登录后侧栏可见运维助手
- [ ] 新建会话、发送消息，消息区出现用户气泡与助手增量/最终回复
- [ ] 断网再恢复可见重连；不崩页
- [ ] 无 `agent:hitl_approve` 时 HITL 卡无按钮无敏感字段；有权限可批准/拒绝
- [ ] 无 `knowledge:upload` 无上传按钮；有权限可上传
- [ ] 浏览器网络面板仅一条 WS

- [x] **Step: Commit**

```text
完成 T11 运维助手 Chat 跨层验收

- 会话 REST + WS + Chat UI + HITL 卡 + 知识上传闭环
- 前后端检查与权限门控通过，未引入真流式 LLM 改造
```

---

## Architecture acceptance mapping

| # | 架构要求 | 本计划落点 |
| ---: | :--- | :--- |
| 1 | 单一 Chat 页 `OpsAssistantPage` | Task 7 |
| 2 | WS `/api/v1/ws/agent/{session_id}` + 判别式 JSON | Task 1–2, 4 |
| 3 | `ChatMessageList` / `ChatInput` | Task 7 |
| 4 | `HitlApprovalCard` | Task 8 |
| 5 | `KnowledgeUploadDialog` + `knowledge:upload` | Task 9 |
| 6 | 一条连接承载 chat / HITL / monitor_alert | Task 1, 6–7（**客户端必须能渲染 `monitor_alert`**；T08 `monitor_sweep` 尚未向 hub 推送属已知缺口，T11 不强制接线 sweep，可用单测/`hub.broadcast` 手工注入验证 UI） |
| 7 | A5 重连策略 | Task 5–6 |
| 8 | 敏感 payload 不进 Agent/WS 摘要 | Task 1, 4, 8 |

---

## Notes for implementers

1. **先后端后前端**：无 WS/会话 API 时前端只能全 mock；本计划把缺口算进 T11，不假装 T06 已有 WS。
2. **成本**：`POST messages` 会打真实 LLM（若环境配置了 key）。单测必须 mock `chat_fn`；手工联调前与用户确认。
3. **Commit 编码**：Windows PowerShell 下用 UTF-8 消息文件 + `git commit -F`，避免再次出现 `?` 乱码。
4. **shadcn**：缺组件时 `npx shadcn@latest docs <name>` 再 `add`；已装列表见当前 `components.json` / `info`。
5. **公共函数优先**：token、权限、PageHeader、Dialog/Field、toast、api 实例一律复用；不要平行再写一套。
6. **Turn 内事件**：优先包装 `chat_fn`/`dispatch_tool`；不要复制粘贴一套第二 loop。
7. **HTTP vs WS**：UI 应以 WS 事件更新助手气泡为主；HTTP `POST messages` 的响应只需表示「turn 已受理/结束」（可返回 `LoopOutcome` 摘要）。务必加长该请求 timeout。
