# 会话审批三档 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 HITL 自动批准从全站 `HITL_NOTIFY_AUTO_APPROVE` 改成当前会话的 `ask` / `assist` / `full` 三档，默认请求审批，并删干净旧开关。

**Architecture:** `AgentSession.approval_mode` 是唯一真相。`propose_action` 按规格判定表决定自动批准还是 PENDING。`PATCH /api/v1/agent/sessions/{id}` 改档。系统配置不再出现 notify 自动审批。设备连接复用不在本期。

**Tech Stack:** Python 3.14.3、FastAPI、SQLAlchemy 2 async、Alembic、Pydantic v2、React 19、TypeScript、pytest、Vitest。`uv run` / `pnpm`。

**Spec:** `docs/superpowers/specs/2026-08-13-session-approval-modes-design.md`

## Global Constraints

- 只在 `master` 工作，不建分支；每个任务验证通过后按项目规范提交一次，禁止 `Co-Authored-By`，未要求不要 push。
- 后端在 `backend/` 下用 `uv run`；前端在 `frontend/` 下用 `pnpm`。不新增依赖。
- 不编辑真实 `backend/.env`，只改 `.env.example`。
- 不新建 HITL 表。需要 Alembic：**只给 `agent_sessions` 加 `approval_mode` 列**。
- 权限仍用 `agent:use`，不新增权限码，不要求 `agent:hitl_approve` 才能改档。
- Python 文件头注释、中文 Google 风格文档字符串；禁止 `from __future__ import annotations`。
- 图标只从 `@/lib/icons` 引入。表单用 `FieldGroup` + `Field`。`SelectItem` 必须包在 `SelectGroup` 里。
- 本期不做设备 SSH 连接复用、不做独立 OTP 弹窗。

## File Structure

| 文件 | 职责 |
| :--- | :--- |
| `backend/app/models/agent_session.py` | 列 `approval_mode` |
| `backend/alembic/versions/2026_08_13_1800-b9e2d4c1a856_session_approval_mode.py` | 加列，默认 `ask` |
| `backend/app/schemas/agent_session.py` | 响应带档位；`AgentSessionApprovalUpdate` |
| `backend/app/api/v1/agent_sessions.py` | `PATCH /sessions/{id}` |
| `backend/app/agent/hitl.py` | `should_auto_approve`；读会话档位 |
| `backend/app/agent/hitl_tools.py` + `tool_dispatch.py` | 命令清单文案跟档位走 |
| `backend/app/services/system_config.py` 等 | 删除 `HITL_NOTIFY_AUTO_APPROVE` |
| `backend/init_db.py` | 不再种子该键；幂等 DELETE |
| `frontend/src/pages/OpsAssistantPage.tsx` + `ChatInput.tsx` | 切档、警告 Dialog、侧栏显示 |

---

### Task 1: 会话列、迁移与 PATCH 改档

**Files:**
- Modify: `backend/app/models/agent_session.py`
- Create: `backend/alembic/versions/2026_08_13_1800-b9e2d4c1a856_session_approval_mode.py`
- Modify: `backend/app/schemas/agent_session.py`
- Modify: `backend/app/api/v1/agent_sessions.py`
- Modify: `backend/tests/test_agent_sessions_api.py`

**Interfaces:**
- `AgentSession.approval_mode: str` 默认 `"ask"`
- `PATCH /api/v1/agent/sessions/{session_id}` body `{ "approval_mode": "ask"|"assist"|"full" }`
- 权限 `agent:use`；非所有者 404；相同档位不写审计

- [ ] **Step 1: 写失败测试**

在 `test_create_and_list_sessions` 里断言：

- 创建结果 `created["approval_mode"] == "ask"`
- 列表每条 `item["approval_mode"]` 存在且为 `"ask"`（新建会话）

在 `test_get_session_detail_and_non_owner_404` 里断言详情 `approval_mode == "ask"`。

追加：

```python
async def test_patch_approval_mode_owner_and_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    create_resp = await client.post(
        "/api/v1/agent/sessions",
        json={"title": "改档"},
        headers=auth_headers,
    )
    session_id = create_resp.json()["data"]["id"]

    patched = await client.patch(
        f"/api/v1/agent/sessions/{session_id}",
        json={"approval_mode": "assist"},
        headers=auth_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["approval_mode"] == "assist"

    from sqlalchemy import func, select
    from app.models.audit_log import AuditLog

    count = await db_session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "update_session_approval_mode"
        )
    )
    assert count == 1

    same = await client.patch(
        f"/api/v1/agent/sessions/{session_id}",
        json={"approval_mode": "assist"},
        headers=auth_headers,
    )
    assert same.status_code == 200
    count_after = await db_session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "update_session_approval_mode"
        )
    )
    assert count_after == 1


async def test_patch_approval_mode_rejects_invalid_and_non_owner(
    client: AsyncClient,
    db_session: AsyncSession,
    test_role: Role,
    auth_headers: Headers,
    login_user,
) -> None:
    create_resp = await client.post(
        "/api/v1/agent/sessions",
        json={"title": "他人"},
        headers=auth_headers,
    )
    session_id = create_resp.json()["data"]["id"]

    bad = await client.patch(
        f"/api/v1/agent/sessions/{session_id}",
        json={"approval_mode": "bypass"},
        headers=auth_headers,
    )
    assert bad.status_code == 422

    other = await _other_user(db_session, test_role)
    other_headers = await login_user(other.username, "testpassword123")
    forbidden = await client.patch(
        f"/api/v1/agent/sessions/{session_id}",
        json={"approval_mode": "full"},
        headers=other_headers,
    )
    assert forbidden.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
uv run pytest tests/test_agent_sessions_api.py::test_create_and_list_sessions tests/test_agent_sessions_api.py::test_patch_approval_mode_owner_and_audit tests/test_agent_sessions_api.py::test_patch_approval_mode_rejects_invalid_and_non_owner -q
```

Expected: FAIL（没有 `approval_mode` / 没有 PATCH）

- [ ] **Step 3: 实现模型、迁移、schema、路由**

`agent_session.py` 增加：

```python
approval_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="ask")
```

迁移（`down_revision = "a2f6c8d91e37"`，与现有加列迁移同样的 destructive downgrade 保护）：

```python
def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column(
            "approval_mode",
            sa.String(length=20),
            nullable=False,
            server_default="ask",
        ),
    )
```

schema：

```python
from typing import Literal

type ApprovalMode = Literal["ask", "assist", "full"]

class AgentSessionApprovalUpdate(ApiModel):
    approval_mode: ApprovalMode

class AgentSessionResponse(ApiModel):
    # 现有字段保持不变，增加：
    approval_mode: ApprovalMode
```

`AgentSessionCreate` **不要**加 `approval_mode`。

路由：在 DELETE 旁增加 PATCH，复用 `_owned_session_or_404`。用 `agent_session_crud.update`。仅当旧值 ≠ 新值时 `log_audit(..., action="update_session_approval_mode", target=f"agent_session:{id}", detail=f"{old}→{new}")`。然后 `commit` + `refresh`。

创建会话不必显式写入 `approval_mode`（ORM default + DB server_default）。

- [ ] **Step 4: 跑测试**

```bash
uv run pytest tests/test_agent_sessions_api.py -q
```

Expected: PASS。本地 Postgres 再执行 `uv run alembic upgrade head`（会改真实库结构，实施时做；SQLite 测试库走 create_all 时模型已含新列）。

- [ ] **Step 5: Commit**

```text
会话增加审批模式列并支持 PATCH 改档

- agent_sessions.approval_mode 默认 ask，创建接口不接收该字段
- 所有者可改为 ask/assist/full；相同档位不写审计
```

---

### Task 2: `propose_action` 按会话档位自动批准

**Files:**
- Modify: `backend/app/agent/hitl.py`
- Modify: `backend/tests/test_agent_hitl.py`
- Modify: `backend/tests/test_hitl_api.py`
- Modify: `backend/tests/test_hitl_integration.py`
- Modify: `backend/tests/test_device_command_execution_integration.py`
- Modify: `backend/tests/test_device_control_execution.py`（若存在且依赖白名单自动执行）

**Interfaces:**
- `should_auto_approve(*, approval_mode: str, action_type: ActionType, policy_decision: str | None, credential_type: str) -> bool`
- `propose_action` 不再调用 `get_effective_operations_config` 做自动批准

判定（黑名单已在建提案前拒绝，不会进这个函数）：

```python
def should_auto_approve(
    *,
    approval_mode: str,
    action_type: ActionType,
    policy_decision: str | None,
    credential_type: str,
) -> bool:
    if action_type == "notify":
        return approval_mode in ("assist", "full")
    if action_type in ("device_query", "device_control"):
        if credential_type == "dynamic":
            return False
        if policy_decision == "whitelist":
            return approval_mode in ("assist", "full")
        if policy_decision is None:
            return approval_mode == "full"
        return False
    return False
```

`propose_action` 在创建提案前：

```python
session = await agent_session_crud.get(db, session_id)
if session is None:
    raise HitlProposalRejectedError("会话不存在")
approval_mode = session.approval_mode
```

把原来的

```python
operations = await get_effective_operations_config(db)
if (action_type == "notify" and operations.hitl_notify_auto_approve) or (
    action_type in ("device_query", "device_control")
    and policy_decision == "whitelist"
    and asset.credential_type != "dynamic"
):
```

换成 `should_auto_approve(...)`。`notify` 的 `policy_decision` 用 `None`，`credential_type` 用 `asset.credential_type`（notify 也有 asset）。删除本函数对 `get_effective_operations_config` 的导入（若文件内无其它引用）。

- [ ] **Step 1: 写失败测试（默认 ask 下白名单不再自动执行）**

在 `test_agent_hitl.py` 用现有 `_make_context` + 给资产配 vendor/凭据/白名单的方式（抄该文件里已有白名单用例的资产准备）。核心断言：

1. 默认会话（ask）+ notify → `PENDING`
2. 把该会话 `approval_mode` 改成 `assist` 后再 notify → `EXECUTED`
3. ask + 白名单 + 静态凭据的 `device_query` → `PENDING`（今天会 EXECUTED，这是本任务的红灯）
4. assist + 白名单 + 静态 → `EXECUTED`
5. full + 未分类 + 静态 → `EXECUTED`
6. assist + 未分类 + 静态 → `PENDING`
7. full + 白名单 + `credential_type=dynamic` → `PENDING`
8. 三档 + 黑名单 → `HitlProposalRejectedError`，提案数为 0
9. `propose_action(..., session_id=999999, ...)`（不存在的会话）→ `HitlProposalRejectedError("会话不存在")`，提案数为 0

给会话改档：

```python
session = await agent_session_crud.get(db_session, session_id)
assert session is not None
session.approval_mode = "assist"
await db_session.flush()
```

**删掉/改写** `test_database_setting_can_auto_approve_notify_when_env_is_false`（全站键将删除，不要再测 DB 覆盖 env）。

`test_notify_auto_approve_uses_actor_and_executes_once`：去掉 `monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", True)`，改为把测试会话设为 `assist`。

全库搜索 `HITL_NOTIFY_AUTO_APPROVE`：

- 只是为了让 notify **不要**自动过而 `monkeypatch False` 的测试：删掉这行（默认 ask 已经不会自动过 notify）。
- **期望白名单当场执行** 的设备命令测试：必须给会话设 `assist` 或 `full`，否则默认 ask 会变 PENDING。`test_device_command_execution_integration.py` 属于这类。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_agent_hitl.py -q
```

Expected: FAIL（ask 下白名单仍 EXECUTED，或新断言失败）

- [ ] **Step 3: 实现 `should_auto_approve` 并改分支**

- [ ] **Step 4: 跑测试**

```bash
uv run pytest tests/test_agent_hitl.py tests/test_hitl_api.py tests/test_hitl_integration.py tests/test_device_command_execution_integration.py -q
```

Expected: PASS。若还有 `test_device_control_execution*.py` 失败，按同样规则改会话档位后再跑。

- [ ] **Step 5: Commit**

```text
HITL 自动批准改为读取会话审批模式

- ask 下 notify 与白名单都进入待审批；assist/full 按规格判定表执行
- 动态凭据与黑名单行为不变；不再读取 HITL_NOTIFY_AUTO_APPROVE
```

---

### Task 3: 命令清单文案跟档位走

**Files:**
- Modify: `backend/app/agent/hitl_tools.py`
- Modify: `backend/app/agent/tool_dispatch.py`
- Modify: `backend/tests/test_agent_hitl_tools.py`

**Interfaces:**
- `list_device_commands_for_asset(db, *, session_id: int, asset_id: int)`

文案（规格 §3.2）：

| 策略 | ask | assist | full |
| :--- | :--- | :--- | :--- |
| 黑名单 | 禁止执行 | 禁止执行 | 禁止执行 |
| 白名单 | 白名单（当前为请求审批，需人工批准） | 白名单（可自动执行） | 白名单（可自动执行） |
| 未分类 | 未分类（需人工审批） | 未分类（需人工审批） | 未分类（完全访问，可自动执行） |

黑名单行今天是 `黑名单（禁止执行）`，保持该完整字符串，三档相同。

动态凭据提示句三档都保留。

会话不存在：`control="rejected"`，中文「会话不存在」。

- [ ] **Step 1: 改现有测试并加 ask 断言**

真实测试名是 `test_list_device_commands_reports_policy_and_credential_state`，今天断言 `"白名单（自动执行）"`。该测试的 `_make_session_and_asset` 默认 ask，应改为断言 **不含** `"可自动执行"`，且含 `"白名单（当前为请求审批，需人工批准）"`。调用改为传入 `session_id`。

同一文件里另外两个直接调用也必须加 `session_id`（缺资产时随便传一个已有会话 ID 即可）：

- `test_list_device_commands_rejects_missing_asset`
- `test_list_device_commands_rejects_asset_without_vendor`

`test_root_dispatcher_routes_list_device_commands` 的 fake 捕获应变为含 `session_id`：`dispatch = build_root_tool_dispatcher(..., session_id=1, ...)` 后 `captured == {"asset_id": 9, "session_id": 1}`。

追加：同一白名单命令，会话改成 `assist` 后再列一次，内容含 `"白名单（可自动执行）"`。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_agent_hitl_tools.py -q
```

Expected: FAIL（缺 `session_id` 或旧文案）

- [ ] **Step 3: 改工具签名与 dispatch**

`tool_dispatch.py`：

```python
return await list_device_commands_for_asset(
    db, session_id=session_id, asset_id=list_args.asset_id
)
```

- [ ] **Step 4: 跑测试**

```bash
uv run pytest tests/test_agent_hitl_tools.py tests/test_agent_hitl.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```text
设备命令清单按会话审批模式显示是否自动执行

- list_device_commands 读取 session_id 的 approval_mode
- ask 下不再对白名单声称自动执行
```

---

### Task 4: 删除全站 `HITL_NOTIFY_AUTO_APPROVE`

**Files:**
- Modify: `backend/app/core/config.py`（删字段）
- Modify: `backend/.env.example`
- Modify: `backend/app/services/system_config.py`
- Modify: `backend/app/schemas/system_config.py`
- Modify: `backend/app/api/v1/system_config.py`
- Modify: `backend/init_db.py`
- Modify: `backend/tests/test_system_config_seeds.py`
- Modify: `backend/tests/test_system_config_api.py`
- Modify: `backend/tests/test_system_config_service.py`
- Modify: `frontend/src/types/system-config.ts`
- Modify: `frontend/src/components/system-config/systemConfigFormSchemas.ts`
- Modify: `frontend/src/components/system-config/systemConfigFormSchemas.test.ts`
- Modify: `frontend/src/components/system-config/OperationsConfigCard.tsx`
- Modify: `frontend/src/components/system-config/OperationsConfigCard.test.tsx`
- 全库再搜一次 `HITL_NOTIFY_AUTO_APPROVE` / `hitl_notify_auto_approve`，清掉剩余引用（含 `backend/README.md`、`backend/README_zh.md`）

**不要**改 `backend/.env`。

- [ ] **Step 1: 改种子测试为四项运行键，并断言会删旧键**

把 `test_seed_system_configs_creates_only_five_operational_keys` 改名为 `...four...`，`assert == 4`，且 `"HITL_NOTIFY_AUTO_APPROVE" not in OPERATIONS_CONFIG_KEYS`。

`test_seed_system_configs_preserves_existing_values`：预置 1 个键后，期望新插入从 `4` 改为 `3`（四项里已有一项）。

追加：先手动插入 `HITL_NOTIFY_AUTO_APPROVE` 行，跑 `seed_system_configs()`，断言该键不在库里。

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_system_config_seeds.py -q
```

Expected: FAIL（仍是 5 个键或旧键还在）

- [ ] **Step 3: 删键并幂等 DELETE**

`OPERATIONS_CONFIG_KEYS` 去掉 `KEY_HITL_NOTIFY_AUTO_APPROVE`。`EffectiveOperationsConfig` 去掉该字段。`save_operations_config` / `build_system_config_response` / PUT 审计映射同步删。

`init_db._system_config_seed_values()` 只留四项。`seed_system_configs` 在 `create_missing` **之前**：

```python
from sqlalchemy import delete
from app.models.system_config import SystemConfig

await db.execute(
    delete(SystemConfig).where(SystemConfig.key == "HITL_NOTIFY_AUTO_APPROVE")
)
```

前端 `OperationsConfigCard` 删除 Switch、`autoApprove` 警告 Alert、`hitl_notify_auto_approve` 字段。卡片描述改为只谈巡检/对账/日志保留，不再提 HITL 自动批准。Zod schema 与 `_operations_payload` / `validOperationsForm` 同步。

- [ ] **Step 4: 验证**

```bash
uv run pytest tests/test_system_config_seeds.py tests/test_system_config_api.py tests/test_system_config_service.py tests/test_agent_hitl.py -q
cd ../frontend
pnpm exec vitest run src/components/system-config/systemConfigFormSchemas.test.ts src/components/system-config/OperationsConfigCard.test.tsx
```

Expected: PASS。全库搜索这两个名字应只剩本计划/规格文档的历史叙述（代码与 README 中不应再作为配置项出现）。

- [ ] **Step 5: Commit**

```text
删除全站 notify 自动审批配置

- 运行参数种子改为四项，init_db 幂等删除旧键 HITL_NOTIFY_AUTO_APPROVE
- 系统设置表单与 .env.example 同步去掉该开关
```

---

### Task 5: 聊天页切档、警告弹窗、侧栏显示

**Files:**
- Modify: `frontend/src/types/agent.ts`
- Modify: `frontend/src/lib/agent-api.ts`
- Modify: `frontend/src/components/ops-assistant/ChatInput.tsx`
- Modify: `frontend/src/pages/OpsAssistantPage.tsx`
- Modify: `frontend/src/pages/AuditLogsPage.tsx`（`ACTION_LABELS`）
- Test（可选但推荐）: `frontend/src/components/ops-assistant/ChatInput.test.tsx` 若项目对该组件尚无测试，至少保证 `pnpm exec tsc -b --pretty false --force` 通过

**Interfaces:**
- `export type ApprovalMode = "ask" | "assist" | "full"`
- `AgentSession` 增加 `approval_mode: ApprovalMode`（不要只加类型别名而漏掉会话对象字段）
- `patchAgentSession(id, { approval_mode })`
- `ChatInput` 新增 props：`approvalMode: ApprovalMode | null`、`onApprovalModeSelect: (mode: ApprovalMode) => void`（**不**在 ChatInput 里发 PATCH）
- 中文：`ask` 请求审批；`assist` 帮我审批；`full` 完全访问

- [ ] **Step 1: 实现类型、API、UI**

`Select` 的 `value` 始终是当前会话已保存的档位。用户选 `full` 且当前不是 `full`：`OpsAssistantPage` 打开 Dialog，**不要**先改本地 `approval_mode`。确认后再 `patchAgentSession`；成功后用返回体更新 `sessions` 数组里对应项（侧栏 + 选择器一起变）。失败 toast，保持旧档。选 `ask`/`assist` 直接 PATCH。无选中会话时选择器 `disabled`。

Dialog：`DialogTitle` = 「确认开启完全访问」。正文按规格。

侧栏每条会话在时间旁或下一行显示中文档位。

`AuditLogsPage`：`update_session_approval_mode: "变更会话审批模式"`。

输入框左侧放 Select：用 `InputGroupAddon align="inline-start"`，`SelectItem` 包在 `SelectGroup`，并给 `Select` 传 `items`（与项目其它 Select 一样，否则受控值不显示文案）。

- [ ] **Step 2: 验证**

```bash
cd frontend
pnpm exec tsc -b --pretty false --force
```

Expected: 无错误。若写了 ChatInput 测试则一并 `pnpm exec vitest run src/components/ops-assistant/ChatInput.test.tsx`。

- [ ] **Step 3: Commit**

```text
运维助手支持按会话切换审批模式

- 输入框旁选择请求审批/帮我审批/完全访问，完全访问需确认
- 侧栏显示当前档位；操作日志补上变更会话审批模式
```

---

### Task 6: 架构与系统配置文档

**Files:**
- Modify: `docs/AGENT_ARCHITECTURE.md`（第 6 节 HITL、第 8 节前端、第 9 节 L3）
- Modify: `docs/SYSTEM_CONFIG.md`（删 `HITL_NOTIFY_AUTO_APPROVE` 行）

L3 改为：审批模式在 `AgentSession.approval_mode`（默认 `ask`）；黑名单不可绕过；动态凭据始终要人输入本次密码；`full` 仅额外放开未分类非动态命令。

第 8 节给 `ChatInput` 补一句档位选择器。不要写连接复用。

- [ ] **Step 1: 改文档**

- [ ] **Step 2: Commit**

```text
文档改为会话级审批三档

- AGENT_ARCHITECTURE L3 不再写全站 notify 自动批准
- SYSTEM_CONFIG 删除已下线的 HITL_NOTIFY_AUTO_APPROVE
```

---

## Spec coverage

| 规格 | 任务 |
| :--- | :--- |
| `approval_mode` 列、默认 ask、迁移 | Task 1 |
| PATCH 改档、审计、非所有者 404 | Task 1 |
| 判定表 / should_auto_approve | Task 2 |
| 命令清单文案 | Task 3 |
| 删除全站开关与旧键 | Task 4 |
| 聊天 UI、警告、侧栏 | Task 5 |
| 架构文档 | Task 6 |
| 不做连接复用 / 独立 OTP | 全任务 |

## 本地验收

```bash
cd backend
uv run alembic upgrade head
uv run python init_db.py
uv run pytest tests/test_agent_sessions_api.py tests/test_agent_hitl.py tests/test_agent_hitl_tools.py tests/test_system_config_seeds.py tests/test_hitl_api.py -q
cd ../frontend
pnpm exec tsc -b --pretty false --force
```

新开会话应为「请求审批」。改成「帮我审批」后 notify / 白名单才自动过。系统设置运行参数里不应再有 notify 开关。
