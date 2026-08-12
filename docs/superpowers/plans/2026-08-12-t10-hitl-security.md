# T10 · HITL + 安全闸门 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the human-in-the-loop write path promised by T10: root-only `propose_remediation`, HITL orchestration on top of the existing `HitlProposal` CRUD, `notify` / stub `device_control` executors, RBAC permission seeds (`cmdb:*` / `monitor:*` / `agent:hitl_approve`), and an HTTP approve/reject API — without real device control or WebSocket UI.

**Architecture:** T06 already shipped `HitlProposal` + CRUD transitions (`PENDING → APPROVED|REJECTED`, `APPROVED → EXECUTED`). T10 adds an orchestration layer in `app/agent/hitl.py` that owns payload schema validation (L2), risk policy (L3: notify optionally auto-approve; device_control always HITL), executor dispatch (L4 stub), safe agent-facing summaries (no sensitive payload echo), and an injectable event publisher hook for T11 WebSocket. The tool returns `control="pending_approval"` so `run_loop` stops the turn. Child roles stay read-only: `propose_remediation` must never enter `roles.py` allowlists.

**Tech Stack:** Python 3.14.3, FastAPI, SQLAlchemy 2 async, Pydantic 2, existing RBAC (`require_permission`), pytest + pytest-asyncio, uv. No new Python dependency.

**Spec:** [docs/AGENT_ARCHITECTURE.md](../../AGENT_ARCHITECTURE.md) §4.2 (write tools), §6 (HITL state machine), §9 (L0–L6), §11 A6/A7, §13 T10 row. Also [docs/guide.md](../../guide.md) §5.2–5.3.

## Global Constraints

- Python `>=3.14,<3.15` only. Run every Python/test command from `backend/` with `uv run`.
- Work directly on `master`; no branch/PR. Chinese commit messages (title + blank line + bullets); never `Co-Authored-By`.
- TDD: failing test → observe failure → minimal production code → focused tests green → commit.
- No real LLM, embedding, Docker PostgreSQL, or real device-control channel.
- CRUD methods only `flush()`. Route handlers and HITL orchestration entry points that own a request/lifecycle open sessions and `commit()` once after business + audit succeed (same rule as knowledge upload).
- Reuse existing `hitl_proposal_crud.decide` / `mark_executed`; do not fork a second state machine. Orchestration may wrap them, not replace them.
- `asset_id` stays a soft reference inside `action_payload` (assumption A7). Validate existence via `cmdb_asset_crud.get` at propose time; do not add a DB FK.
- `device_control` executor in T10 is `NotImplementedExecutor` only: APPROVED proposals must **not** be marked EXECUTED when the stub fails (architecture §6).
- Sensitive `action_payload` fields must not be returned to the Agent tool result or to any “agent-safe” summary DTO. Approver-facing API responses may include the full payload.
- Child Agent allowlists remain the seven read-only tools. Add an explicit regression that `propose_remediation` is absent from every role in `ROLE_CATALOG`.
- `knowledge:read` / `knowledge:upload` / `knowledge:manage` already exist in `init_db.py` and the knowledge API — do not rename them; only ensure seeds stay idempotent and mirror them into frontend `PERMISSIONS`.
- Full validation at end: `uv run pytest -v`, `uv run mypy app`, `uv run ruff check .`, `uv run alembic heads` (expect existing head `d6a1b4c9f235`; T10 should not need a schema migration unless absolutely required — prefer no migration).

## File Map

| File | Responsibility |
| :--- | :--- |
| `backend/init_db.py` | Idempotent seed for `cmdb:*`, `monitor:*`, `agent:hitl_approve` (keep existing `knowledge:*`). |
| `frontend/src/lib/constants.ts` | Mirror new permission codes (and missing `knowledge:*`) for UI gates. |
| `backend/app/core/config.py` | `HITL_NOTIFY_AUTO_APPROVE` (default `false`). |
| `backend/app/agent/executors.py` | `ExecutionResult`, `DeviceControlExecutor` Protocol, `NotImplementedExecutor`, `NotifyExecutor`. |
| `backend/app/agent/hitl.py` | Propose / decide / resume orchestration, safe summaries, event publisher Protocol + noop. |
| `backend/app/crud/hitl_proposal.py` | Add list helpers (`list_for_session`, optional status filter); keep existing transitions. |
| `backend/app/agent/hitl_tools.py` | `propose_remediation` tool returning `pending_approval`. |
| `backend/app/agent/tool_dispatch.py` | Optional root-tool registration path that can include `propose_remediation` without polluting child allowlists. |
| `backend/app/agent/roles.py` + tests | Regression: no write tool in any child role. |
| `backend/app/schemas/hitl.py` | Request/response DTOs (approver full vs agent-safe). |
| `backend/app/api/v1/hitl.py` + `app/api/router.py` | List + decide endpoints gated by `agent:hitl_approve`. |
| `backend/tests/test_agent_hitl_*.py` / `test_hitl_api.py` | Unit + API + policy tests. |

## Explicitly Out of Scope

- WebSocket `/ws/agent/{session_id}`, `hitl_pending` / `hitl_resolved` framing, and `HitlApprovalCard` (T11). T10 only defines a publisher Protocol + noop default.
- Real device-control channel / vendor SDK.
- CMDB/monitor management HTTP APIs (create/edit assets or targets). T10 only seeds permission codes for future use.
- Wiring a live root chat loop over WebSocket. T10 exposes the tool + a root dispatcher factory; T11 connects transport.
- Changing spawn/orchestration workflows to auto-call `propose_remediation` (T09 remains advisory/read-only).
- New Alembic migration unless absolutely required (current `hitl_proposals` table is enough).

---

### Task 1: Permission seeds, frontend constants, and HITL config

**Files:**
- Modify: `backend/init_db.py`
- Modify: `frontend/src/lib/constants.ts`
- Modify: `backend/app/core/config.py`
- Create: `backend/tests/test_hitl_permission_seeds.py`

**Interfaces:**
- Consumes: existing `SEED_PERMISSIONS` tuple and idempotent `seed_permissions()`.
- Produces: additional permission rows + `HITL_NOTIFY_AUTO_APPROVE: bool = False`.

Exact permission codes to add (names in Chinese, modules as below):

| code | module | name |
| :--- | :--- | :--- |
| `cmdb:read` | CMDB | 查看 CMDB 资产 |
| `cmdb:manage` | CMDB | 管理 CMDB 资产 |
| `monitor:read` | 监控 | 查看监控目标与状态 |
| `monitor:manage` | 监控 | 管理监控目标 |
| `agent:hitl_approve` | Agent | 审批 HITL 提案 |

Keep existing `knowledge:read` / `knowledge:upload` / `knowledge:manage` unchanged.

- [ ] **Step 1: Write failing seed contract test**

```python
"""HITL / ops permission seed contract."""

from init_db import SEED_PERMISSIONS

REQUIRED = {
    "knowledge:read",
    "knowledge:upload",
    "knowledge:manage",
    "cmdb:read",
    "cmdb:manage",
    "monitor:read",
    "monitor:manage",
    "agent:hitl_approve",
}

def test_seed_permissions_include_t10_codes() -> None:
    codes = {item["code"] for item in SEED_PERMISSIONS}
    assert REQUIRED <= codes
    assert len(codes) == len(SEED_PERMISSIONS)  # no duplicate codes
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_hitl_permission_seeds.py -v
```

Expected: FAIL because `cmdb:*` / `monitor:*` / `agent:hitl_approve` are missing.

- [ ] **Step 3: Extend seeds + config + frontend constants**

Append the five new entries to `SEED_PERMISSIONS`. Add to `Settings`:

```python
HITL_NOTIFY_AUTO_APPROVE: bool = False
```

Update `frontend/src/lib/constants.ts` `PERMISSIONS` with:

```ts
  KNOWLEDGE_READ: "knowledge:read",
  KNOWLEDGE_UPLOAD: "knowledge:upload",
  KNOWLEDGE_MANAGE: "knowledge:manage",
  CMDB_READ: "cmdb:read",
  CMDB_MANAGE: "cmdb:manage",
  MONITOR_READ: "monitor:read",
  MONITOR_MANAGE: "monitor:manage",
  AGENT_HITL_APPROVE: "agent:hitl_approve",
```

- [ ] **Step 4: GREEN + commit**

```bash
uv run pytest tests/test_hitl_permission_seeds.py -v
uv run ruff check init_db.py app/core/config.py tests/test_hitl_permission_seeds.py
```

```bash
git add backend/init_db.py backend/app/core/config.py backend/tests/test_hitl_permission_seeds.py frontend/src/lib/constants.ts
git commit -m "新增 HITL 与运维权限种子及自动批准配置

- 幂等种子 cmdb/monitor/agent:hitl_approve，保留既有 knowledge:* 码
- 增加 HITL_NOTIFY_AUTO_APPROVE（默认 false）供 notify 低风险策略使用
- 前端 PERMISSIONS 同步知识库与 HITL 权限码，避免 UI 门控漂移"
```

---

### Task 2: ExecutionResult and executors (notify + device_control stub)

**Files:**
- Create: `backend/app/agent/executors.py`
- Create: `backend/tests/test_agent_executors.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True, slots=True)
class ExecutionResult:
    ok: bool
    message: str
    detail: dict[str, object] = field(default_factory=dict)

class DeviceControlExecutor(Protocol):
    async def execute(self, payload: Mapping[str, object]) -> ExecutionResult: ...

class NotImplementedExecutor:
    async def execute(self, payload: Mapping[str, object]) -> ExecutionResult:
        return ExecutionResult(ok=False, message="device_control 执行器尚未接入")

class NotifyExecutor:
    async def execute(
        self,
        db: AsyncSession,
        *,
        proposal_id: int,
        payload: Mapping[str, object],
        actor_user_id: int | None,
    ) -> ExecutionResult:
        # writes audit_logs via log_audit; does not invent a second audit table
        ...
```

- [ ] **Step 1: Write executor tests**

Cover: stub always returns `ok=False`; notify writes an audit row with action `hitl_notify_executed` and returns `ok=True`; notify rejects blank message.

- [ ] **Step 2: RED → implement → GREEN → commit**

```bash
uv run pytest tests/test_agent_executors.py -v
```

```bash
git add backend/app/agent/executors.py backend/tests/test_agent_executors.py
git commit -m "新增 HITL 执行器接口与 notify/stub 实现

- DeviceControlExecutor + NotImplementedExecutor 保证 stub 无法伪造成功
- NotifyExecutor 复用 audit_logs 记录通知执行结果
- 为后续 hitl.resume 提供统一 ExecutionResult 契约"
```

---

### Task 3: Payload schemas and HITL orchestration (`hitl.py`)

**Files:**
- Create: `backend/app/agent/hitl.py`
- Create: `backend/tests/test_agent_hitl.py`
- Modify: `backend/app/crud/hitl_proposal.py` (list helpers only)

**Interfaces:**
- Consumes: `hitl_proposal_crud`, `cmdb_asset_crud.get`, executors, `settings.HITL_NOTIFY_AUTO_APPROVE`.
- Produces:

```python
ActionType = Literal["notify", "device_control"]

class NotifyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=2000)
    # asset_id is NOT on the typed payload model — it comes from the tool's
    # top-level asset_id argument and is merged into the stored JSON.

class DeviceControlPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: Literal["reboot", "shutdown", "port_disable", "port_enable"]
    # optional device-specific fields may be added later; keep extra=forbid

class HitlEventPublisher(Protocol):
    async def publish(
        self, *, session_id: int, event_type: str, payload: Mapping[str, object]
    ) -> None: ...

class NoopHitlEventPublisher:
    async def publish(self, *, session_id: int, event_type: str, payload: Mapping[str, object]) -> None:
        return None

class HitlProposalRejectedError(ValueError):
    """Raised before insert when validation / CMDB lookup rejects a proposal."""

class HitlResumeError(ValueError):
    """Raised when resume is requested on a non-APPROVED proposal (except EXECUTED)."""

@dataclass(frozen=True, slots=True)
class ProposalSafeSummary:
    proposal_id: int
    action_type: ActionType
    status: str
    reason: str
    asset_id: int | None
    # NEVER include raw secrets / full device credentials

async def propose_action(
    db: AsyncSession,
    *,
    session_id: int,
    proposed_by_agent_id: str | None,
    action_type: ActionType,
    asset_id: int,
    payload: Mapping[str, object],
    reason: str,
    actor_user_id: int,
    publisher: HitlEventPublisher | None = None,
) -> ProposalSafeSummary: ...

async def decide_proposal(
    db: AsyncSession,
    *,
    proposal_id: int,
    approve: bool,
    reviewed_by_user_id: int,
    publisher: HitlEventPublisher | None = None,
) -> ProposalSafeSummary: ...

async def resume_proposal(
    db: AsyncSession,
    *,
    proposal_id: int,
    actor_user_id: int | None = None,
    publisher: HitlEventPublisher | None = None,
) -> ProposalSafeSummary: ...
```

**Top-level `asset_id` merge rule (mandatory, prevents Task 3/4 schema fights):**

1. Architecture tool args are `asset_id`, `action_type`, `payload`, `reason`.
2. Typed payload models (`NotifyPayload` / `DeviceControlPayload`) do **not** include `asset_id`.
3. Before validation, if `payload` contains `asset_id` and it differs from the tool top-level `asset_id`, raise `HitlProposalRejectedError`. If it matches (or is absent), **pop/strip** `payload["asset_id"]` before `extra=forbid` validation so a redundant matching key does not become a cryptic Pydantic extra-field error.
4. After validating the typed payload, build `stored_payload = {**validated, "asset_id": asset_id, "proposal_reason": reason}` for persistence and CMDB lookup via `stored_payload["asset_id"]`.

Hard rules to encode in tests:

1. Unknown `action_type` / extra payload keys / numeric strings under `strict=True` → reject before insert.
2. Missing CMDB asset → reject; no row created.
3. `device_control` never auto-approves even if `HITL_NOTIFY_AUTO_APPROVE=True`.
4. `notify` + auto-approve true → create PENDING, immediately decide approve with `actor_user_id`, then resume to EXECUTED in one orchestration call (still append-only transitions). `actor_user_id` is required (`int`); callers must pass the session owner (from `AgentSession.user_id`) or the authenticated API user — never invent a synthetic id.
5. `device_control` approve + stub fail → status stays `APPROVED`, `executed_at` remains null.
6. Resume on `EXECUTED` is idempotent no-op returning the same safe summary; resume on `PENDING`/`REJECTED` raises `HitlResumeError`; must never execute twice.
7. Agent-safe summary never contains payload keys beyond `asset_id` / `action_type` / `reason` / status fields.
8. Publisher events: `hitl_pending` once on create; `hitl_resolved` once when the proposal becomes terminal for the human decision path — i.e. on reject after decide, on EXECUTED after successful resume, and on auto-approve notify after resume. Do **not** also publish `hitl_resolved` on the intermediate APPROVED state when resume will follow in the same call. Stub-failed device_control stays APPROVED and publishes a single `hitl_execution_failed` event (not `hitl_resolved`) so T11 can show “approved but not executed”.

CRUD additions:

```python
async def list_for_session(
    self, db: AsyncSession, session_id: int, *, status: str | None = None
) -> list[HitlProposal]: ...
```

Persist the tool-level `reason` into stored payload as `proposal_reason` during propose (avoids a schema migration while keeping approver UI informative).

- [ ] **Step 1: Write failing orchestration tests** (schema + policy + stub failure + publisher calls).

- [ ] **Step 2: RED**

```bash
uv run pytest tests/test_agent_hitl.py -v
```

Expected: import/collection failure or missing symbols.

- [ ] **Step 3: Implement schemas, list helper, orchestration**

`propose_action` algorithm:

1. Require `actor_user_id: int` (session owner or authenticated approver context).
2. Apply the top-level `asset_id` merge rule; validate action_type + typed payload model.
3. `asset = await cmdb_asset_crud.get(db, asset_id)`; if None → raise `HitlProposalRejectedError`.
4. `hitl_proposal_crud.create(...)` with `stored_payload`.
5. Publish `hitl_pending` with safe summary.
6. If `action_type == "notify"` and `settings.HITL_NOTIFY_AUTO_APPROVE`: call `decide_proposal(..., reviewed_by_user_id=actor_user_id)` then `resume_proposal(...)` (publish rules above).
7. Return safe summary (status may already be EXECUTED for auto-notify).

`decide_proposal`: wrap `hitl_proposal_crud.decide`, audit `hitl_approved` / `hitl_rejected`. Publish `hitl_resolved` only on reject here; on approve leave publishing to `resume_proposal` / HTTP combine path so T11 does not see double resolved events. Do **not** auto-resume inside decide — HTTP may call both.

`resume_proposal`:

1. Load proposal; if `EXECUTED` → return safe summary (idempotent). If not `APPROVED` → raise `HitlResumeError`.
2. Dispatch executor by `action_type`.
3. If `result.ok`: `mark_executed` + audit + publish `hitl_resolved`.
4. If not ok: leave `APPROVED`, audit failure, publish `hitl_execution_failed`, return safe summary still APPROVED.

- [ ] **Step 4: GREEN + commit**

```bash
uv run pytest tests/test_agent_hitl.py tests/test_agent_crud_hitl.py -v
uv run mypy app/agent/hitl.py app/agent/executors.py app/crud/hitl_proposal.py
```

```bash
git add backend/app/agent/hitl.py backend/app/crud/hitl_proposal.py backend/tests/test_agent_hitl.py
git commit -m "实现 HITL 编排层与提案安全摘要

- 对 notify/device_control 做严格 payload 校验，并提出前校验 CMDB 资产存在
- notify 可配置自动批准；device_control 强制人工审批且 stub 失败不伪装 EXECUTED
- 事件发布钩子默认 noop，供 T11 WebSocket 接入"
```

---

### Task 4: `propose_remediation` tool + root-only dispatch wiring

**Files:**
- Create: `backend/app/agent/hitl_tools.py`
- Modify: `backend/app/agent/tool_dispatch.py`
- Create: `backend/tests/test_agent_hitl_tools.py`
- Modify: `backend/tests/test_agent_roles.py` (regression)

**Interfaces:**

```python
async def propose_remediation(
    db: AsyncSession,
    *,
    session_id: int,
    actor_user_id: int,
    proposed_by_agent_id: str | None,
    asset_id: int,
    action_type: str,
    payload: dict[str, object],
    reason: str,
    publisher: HitlEventPublisher | None = None,
) -> ToolResult:
    ...
```

Return shapes:

- success path: `ToolResult(control="pending_approval", content="<safe Chinese summary with proposal_id>")` (if notify auto-approve already EXECUTED, still use `pending_approval` only when human action remains; if already EXECUTED, return `control="ok"` with a short “已自动批准并执行通知” summary — cover both in tests)
- validation / missing asset / asset_id mismatch: `control="rejected"` or `clarification` with actionable Chinese message
- unexpected errors: `control="failed"` with exception type only

Dispatch changes:

- Keep child `build_tool_dispatcher(db, allowlist)` unchanged for the seven read-only tools (match the existing function signature in `tool_dispatch.py`).
- Add `build_root_tool_dispatcher(db, *, session_id: int, actor_user_id: int, proposed_by_agent_id: str | None = None, publisher=None)` that includes the seven read-only tools **plus** `propose_remediation`.
- `actor_user_id` is mandatory for the root factory. Preferred source: authenticated chat user id from T11; for unit tests pass the fixture user id explicitly. Optionally resolve from `AgentSession.user_id` when the caller only has `session_id`, but the factory signature must still accept `actor_user_id` so auto-approve never lacks a reviewer id.
- Do **not** add `propose_remediation` to `ToolName` in `roles.py`.

Role regression:

```python
def test_no_role_allows_propose_remediation() -> None:
    for role in list_roles():
        assert "propose_remediation" not in role.tools_allowlist
```

- [ ] **Step 1: Failing tool + role tests**

- [ ] **Step 2: Implement tool + root dispatcher factory**

JSON Schema for the model (root tools only) must fix `action_type` enum to `notify|device_control` and require `reason`, `asset_id`, and `payload` object.

- [ ] **Step 3: GREEN + commit**

```bash
uv run pytest tests/test_agent_hitl_tools.py tests/test_agent_roles.py tests/test_agent_tool_dispatch.py -v
```

```bash
git add backend/app/agent/hitl_tools.py backend/app/agent/tool_dispatch.py backend/tests/test_agent_hitl_tools.py backend/tests/test_agent_roles.py
git commit -m "新增 root-only propose_remediation 工具

- 工具返回 pending_approval，敏感 payload 不回传模型
- root dispatcher 可挂载写工具；子角色白名单保持只读七工具
- 回归断言任何 child role 都不能调用 propose_remediation"
```

---

### Task 5: HTTP API for listing and deciding proposals

**Files:**
- Create: `backend/app/schemas/hitl.py`
- Create: `backend/app/api/v1/hitl.py`
- Modify: `backend/app/api/router.py`
- Create: `backend/tests/test_hitl_api.py`

**Endpoints:**

| Method | Path | Permission | Behavior |
| :--- | :--- | :--- | :--- |
| `GET` | `/api/v1/hitl/proposals` | `agent:hitl_approve` | Query `session_id` (required), optional `status`. Returns **approver** DTOs including full `action_payload`. |
| `GET` | `/api/v1/hitl/proposals/{proposal_id}` | `agent:hitl_approve` | Single proposal; 404 if missing. |
| `POST` | `/api/v1/hitl/proposals/{proposal_id}/decide` | `agent:hitl_approve` | Body `{ "approve": bool }`. On approve: `decide_proposal(..., reviewed_by_user_id=current_user.id)` then `resume_proposal`. On reject: decide only. Audit + commit once. |

Response envelope: existing `success_response` / `ResponseEnvelope`.

Decide body schema:

```python
class HitlDecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
```

HTTP error mapping:

- missing proposal → 404
- `InvalidHitlTransitionError` / `HitlResumeError` → 409
- `HitlProposalRejectedError` → 400
- user lacks permission → 403 (via `require_permission`)

Tests (follow `tests/test_knowledge_api.py` pattern: grant permission to `test_user` role in fixture):

1. Without permission → 403.
2. Create proposal via orchestration helper, list returns it with payload.
3. Approve notify → EXECUTED + audit rows.
4. Approve device_control → stays APPROVED when stub fails; second decide → 409.
5. Reject → REJECTED; resume not called.

- [ ] **Step 1: Write API tests (RED)**

- [ ] **Step 2: Implement schemas + routes + router include**

```python
api_router.include_router(hitl_router, prefix="/hitl", tags=["HITL 审批"])
```

- [ ] **Step 3: GREEN + commit**

```bash
uv run pytest tests/test_hitl_api.py -v
uv run mypy app/api/v1/hitl.py app/schemas/hitl.py
```

```bash
git add backend/app/schemas/hitl.py backend/app/api/v1/hitl.py backend/app/api/router.py backend/tests/test_hitl_api.py
git commit -m "新增 HITL 提案查询与审批 API

- 以 agent:hitl_approve 门控 list/get/decide
- 批准后触发 resume；device_control stub 失败保持 APPROVED
- 复用统一响应信封与审计日志，不引入第二套权限体系"
```

---

### Task 6: Cross-component acceptance and T10 verification

**Files:**
- Create: `backend/tests/test_hitl_integration.py`
- Modify: `docs/superpowers/plans/2026-08-12-t10-hitl-security.md` (check boxes during execution)

**Scenario A — notify auto-approve off:**

1. Insert a CMDB asset via CRUD.
2. Call `propose_remediation` through `build_root_tool_dispatcher(..., actor_user_id=test_user.id)`.
3. Assert tool `pending_approval`, proposal PENDING, agent-safe content has no raw payload secrets.
4. HTTP decide approve as permitted user → EXECUTED.
5. Assert audit actions present.

**Scenario B — device_control forced HITL:**

1. Enable `HITL_NOTIFY_AUTO_APPROVE=True` in settings monkeypatch.
2. Propose `device_control` → still PENDING (not auto).
3. Decide approve → remains APPROVED after stub failure.
4. Decide again → 409.

**Scenario C — child isolation:**

1. `build_tool_dispatcher` with a child allowlist rejects `propose_remediation`.

- [x] **Step 1: Write integration tests**

- [x] **Step 2: Run focused + full verification**

```bash
uv run pytest tests/test_hitl_integration.py tests/test_agent_hitl.py tests/test_hitl_api.py tests/test_agent_executors.py tests/test_hitl_permission_seeds.py -v
uv run pytest -v
uv run mypy app
uv run ruff check .
uv run alembic heads
```

Expected:

- all tests pass (no live external services)
- mypy/ruff clean
- Alembic still single head `d6a1b4c9f235` (or a new head only if a migration was truly required — prefer none)

- [x] **Step 3: Diff audit against architecture acceptance**

Confirm:

1. No real device side effects possible.
2. Child roles cannot write.
3. Sensitive payload not returned to tool/model summary.
4. Permission seeds cover architecture T10 codes.
5. No Redis/queue/new vector DB introduced.

- [x] **Step 4: Commit**

```bash
git add backend/tests/test_hitl_integration.py docs/superpowers/plans/2026-08-12-t10-hitl-security.md
git commit -m "完成 T10 HITL 跨组件验收

- 覆盖 notify 人工审批、device_control stub 失败保 APPROVED、子角色拒写
- 验证 root dispatcher 与审批 API 共用同一编排状态机
- 全量 pytest/mypy/ruff 与单 Alembic head 通过，无真实外部依赖"
```

---

## After All Tasks

- Use `superpowers:verification-before-completion` and report fresh command output.
- Optional: dispatch `superpowers:requesting-code-review` on the T10 diff; fix Critical/Important with RED/GREEN tests.
- Do not push unless the project owner asks.
- T11 can now depend on: HITL HTTP API, publisher Protocol, root dispatcher factory, and permission codes.

## Implementation Notes for Agents

- Prefer extending files over new abstractions. One orchestration module (`hitl.py`) is enough; do not create a service layer hierarchy.
- Match existing Chinese user-facing strings / English identifiers mix used in `app/agent/`.
- Python file headers must follow project docstring rules when creating new modules.
- Do not use `from __future__ import annotations` in new files (project rule). Existing files that already have it are left alone.
- Superuser created by `init_db.py` still needs roles/permissions assigned in UI for non-superuser tests; API tests should attach `agent:hitl_approve` to `test_user`'s role explicitly like knowledge tests do.
