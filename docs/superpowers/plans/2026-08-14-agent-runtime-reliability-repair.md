# Agent 运行时可靠性修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复审查发现的全部 Agent/HITL/会话/Spawn/前端恢复问题，使设备命令具备持久化认领与不确定结果人工处置，并让聊天状态、审批和子 Agent 可以从数据库可靠恢复。

**Architecture:** 数据库是所有可恢复状态的唯一真相来源，WebSocket 只承担实时通知。HITL 外部执行使用独立数据库会话和 `APPROVED -> EXECUTING -> EXECUTED/UNKNOWN` 状态机；根会话通过数据库 turn 租约串行化；前端通过分页快照恢复；Spawn 只向根 Agent 暴露受限的只读编排工具。

**Tech Stack:** Python 3.14.3、FastAPI、SQLAlchemy Async、Alembic、Pydantic v2、pytest、React 19、TypeScript 6、Vite 8、Vitest。

## Global Constraints

- 只在 `master` 分支工作，不创建、切换或合并分支，不创建 PR。
- 每个任务先写失败测试并确认失败，再实现最小代码。
- 所有 Python 命令都在 `backend/` 下使用 `uv run`；不得直接运行系统 Python。
- 不新增依赖；确有必要时先说明原因，并使用 `uv add <包名>` 安装最新兼容版本。
- 所有 Agent/HITL 测试使用假 LLM、假设备执行器和假通知器，不访问真实设备，不产生模型费用。
- 设备凭据不得进入 transcript、WebSocket、Spawn task brief、状态原因或审计详情。
- 部署继续只支持一个 Uvicorn worker；不引入 Redis、Celery 或分布式任务运行时。
- 每个 commit 使用中文标题、空行和详细要点，禁止 `Co-Authored-By`。
- 未经项目所有者确认不得执行 `git push`。

---

## File Map

**新增后端文件：**

- `backend/alembic/versions/2026_08_14_1600-f2b4c6d8e013_agent_runtime_reliability.py`：HITL 执行字段和会话 turn 租约迁移。
- `backend/app/agent/hitl_execution.py`：策略复检、持久化认领、外部执行和 UNKNOWN 恢复。
- `backend/app/agent/spawn_tools.py`：根 Agent 专用 Spawn 工具 schema 与 dispatcher。
- `backend/tests/test_runtime_reliability_migration.py`：新迁移契约。
- `backend/tests/test_agent_hitl_execution.py`：执行认领、策略漂移与 UNKNOWN 测试。
- `backend/tests/test_agent_spawn_tools.py`：根 Spawn 工具接入测试。

**新增前端文件：**

- `frontend/src/hooks/use-ops-chat.test.tsx`：快照竞态、重连和发送后恢复测试。
- `frontend/src/components/ops-assistant/ChildAgentStatusCard.tsx`：只读子任务状态卡片。
- `frontend/src/components/ops-assistant/ChildAgentStatusCard.test.tsx`：子任务状态展示测试。
- `frontend/src/lib/cmdb-credential-api.ts`：从组件移出的凭据查询 API。
- `frontend/src/components/cmdb/cmdbAssetPickerUtils.ts`：从组件移出的资产显示纯函数。

**主要修改区域：**

- 后端：`models/`、`crud/`、`agent/hitl*.py`、`agent/loop.py`、`agent/compaction.py`、`agent/spawn.py`、`agent/ws_hub.py`、`api/v1/hitl.py`、`api/v1/agent_sessions.py`、`main.py`。
- 前端：`types/agent.ts`、`lib/agent-api.ts`、`lib/hitl-api.ts`、`hooks/use-ops-chat.ts`、`hooks/use-agent-ws.ts`、运维助手组件、`App.tsx`。
- 文档：`docs/AGENT_ARCHITECTURE.md`、`docs/DEPLOYMENT.md`、`docs/guide.md` 和相关 Mermaid 文件。

---

### Task 1: 持久化字段与 Alembic 迁移

**Files:**
- Create: `backend/alembic/versions/2026_08_14_1600-f2b4c6d8e013_agent_runtime_reliability.py`
- Create: `backend/tests/test_runtime_reliability_migration.py`
- Modify: `backend/app/models/hitl_proposal.py:16-46`
- Modify: `backend/app/models/agent_session.py:9-31`
- Modify: `backend/tests/test_agent_models.py`

**Interfaces:**
- Produces: `HitlProposal.execution_started_at`, `status_reason`, `resolved_by_user_id`, `resolved_at`。
- Produces: `AgentSession.active_turn_token`, `active_turn_started_at`。
- Migration revision: `f2b4c6d8e013`; down revision: `c1a8e4b7d902`。

- [ ] **Step 1: 写模型字段失败测试**

```python
def test_hitl_execution_recovery_columns_exist() -> None:
    columns = HitlProposal.__table__.columns
    assert columns["execution_started_at"].nullable is True
    assert columns["status_reason"].type.length == 50
    assert columns["resolved_by_user_id"].foreign_keys
    assert columns["resolved_at"].nullable is True


def test_agent_session_turn_lease_columns_exist() -> None:
    columns = AgentSession.__table__.columns
    assert columns["active_turn_token"].type.length == 36
    assert columns["active_turn_started_at"].nullable is True
```

- [ ] **Step 2: 写迁移契约失败测试**

```python
MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "2026_08_14_1600-f2b4c6d8e013_agent_runtime_reliability.py"
)


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_runtime_reliability", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_reliability_migration_follows_current_head() -> None:
    migration = _load_migration(MIGRATION_PATH)
    assert migration.revision == "f2b4c6d8e013"
    assert migration.down_revision == "c1a8e4b7d902"
```

迁移测试还要用 fake batch op 断言四个 HITL 列、两个 session 列和 `fk_hitl_proposals_resolved_by_user_id_users` 外键都被创建；降级未传 `-x allow-destructive=true` 时必须抛 `RuntimeError`。

- [ ] **Step 3: 运行测试并确认失败**

Run:

```powershell
uv run pytest tests/test_agent_models.py tests/test_runtime_reliability_migration.py -q
```

Expected: FAIL，模型尚无新字段且迁移文件不存在。

- [ ] **Step 4: 添加模型字段和迁移**

```python
# app/models/hitl_proposal.py
execution_started_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
status_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
resolved_by_user_id: Mapped[int | None] = mapped_column(
    Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
)
resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

# app/models/agent_session.py
active_turn_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
active_turn_started_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

迁移的 `upgrade()` 使用 `op.batch_alter_table` 添加相同列和外键；`downgrade()` 先调用与最新 compaction 迁移一致的 `_require_destructive_downgrade()`，再按外键、列的逆序删除。

- [ ] **Step 5: 验证迁移、模型和单一 head**

Run:

```powershell
uv run pytest tests/test_agent_models.py tests/test_runtime_reliability_migration.py -q
uv run alembic heads
```

Expected: tests PASS；Alembic 只输出 `f2b4c6d8e013 (head)`。

- [ ] **Step 6: 提交 Task 1**

```powershell
git add backend/alembic/versions/2026_08_14_1600-f2b4c6d8e013_agent_runtime_reliability.py backend/app/models/hitl_proposal.py backend/app/models/agent_session.py backend/tests/test_agent_models.py backend/tests/test_runtime_reliability_migration.py
git commit -m "扩展 Agent 可靠性持久化字段" -m "- 为 HITL 增加执行认领、状态原因和 UNKNOWN 人工处置字段。" -m "- 为根会话增加数据库 turn 租约字段，并提供可验证的 Alembic 升降级迁移。"
```

---

### Task 2: HITL 持久化状态转换与原子认领

**Files:**
- Modify: `backend/app/crud/hitl_proposal.py:1-112`
- Modify: `backend/tests/test_agent_crud_hitl.py`

**Interfaces:**
- Produces: `claim_execution(db, proposal_id) -> HitlProposal`。
- Produces: `reject_for_policy(db, proposal_id) -> HitlProposal`。
- Produces: `mark_unknown(db, proposal_id, reason) -> HitlProposal`。
- Produces: `resolve_unknown(db, proposal_id, resolution, resolved_by_user_id) -> HitlProposal`。
- Produces: `recover_executing(db) -> int`。
- `mark_executed` 改为只允许 `EXECUTING -> EXECUTED`。

- [ ] **Step 1: 写完整状态机失败测试**

```python
async def _approved_proposal(
    db_session: AsyncSession,
    test_user: User,
) -> HitlProposal:
    session_id = await _make_session(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "test"},
    )
    await hitl_proposal_crud.decide(
        db_session,
        proposal.id,
        approve=True,
        reviewed_by_user_id=test_user.id,
    )
    return proposal


async def test_execution_state_machine_requires_claim(
    db_session: AsyncSession, test_user: User
) -> None:
    proposal = await _approved_proposal(db_session, test_user)

    with pytest.raises(InvalidHitlTransitionError):
        await hitl_proposal_crud.mark_executed(db_session, proposal.id)

    executing = await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    assert executing.status == "EXECUTING"
    assert executing.execution_started_at is not None

    executed = await hitl_proposal_crud.mark_executed(db_session, proposal.id)
    assert executed.status == "EXECUTED"
    assert executed.status_reason == "executor_succeeded"
```

另外添加：第二次 claim 被拒绝、`APPROVED -> REJECTED(policy_blacklisted)`、`EXECUTING -> UNKNOWN`、UNKNOWN 两种人工处置、非 UNKNOWN 禁止处置、启动恢复只修改 EXECUTING。

- [ ] **Step 2: 写数据库原子认领竞争测试**

```python
async def test_claim_execution_is_compare_and_swap(
    db_session: AsyncSession, second_db_session: AsyncSession, test_user: User
) -> None:
    proposal = await _approved_proposal(db_session, test_user)
    await db_session.commit()

    first = await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    await db_session.commit()
    assert first.status == "EXECUTING"

    with pytest.raises(InvalidHitlTransitionError) as exc_info:
        await hitl_proposal_crud.claim_execution(second_db_session, proposal.id)
    assert exc_info.value.current == "EXECUTING"
```

若现有 fixture 没有 `second_db_session`，在测试内使用项目 `AsyncSessionLocal()` 创建第二个短会话。

- [ ] **Step 3: 运行测试并确认失败**

Run:

```powershell
uv run pytest tests/test_agent_crud_hitl.py -q
```

Expected: FAIL，新转换函数不存在，旧 `mark_executed` 仍接受 APPROVED。

- [ ] **Step 4: 实现原子转换**

```python
async def claim_execution(self, db: AsyncSession, proposal_id: int) -> HitlProposal:
    now = datetime.now(UTC)
    stmt = (
        update(HitlProposal)
        .where(HitlProposal.id == proposal_id, HitlProposal.status == "APPROVED")
        .values(
            status="EXECUTING",
            execution_started_at=now,
            status_reason=None,
        )
        .returning(HitlProposal)
    )
    claimed = (await db.execute(stmt)).scalar_one_or_none()
    if claimed is None:
        current = await self.get(db, proposal_id)
        if current is None:
            raise ValueError(f"HITL proposal {proposal_id} not found")
        raise InvalidHitlTransitionError(current.status, "EXECUTING")
    await db.flush()
    return claimed
```

`resolve_unknown` 接受严格字面量 `Literal["confirm_executed", "allow_retry"]`；前者写 `EXECUTED/manual_confirmed` 和 `executed_at`，后者写 `APPROVED/retry_authorized` 并清空 `execution_started_at`。所有状态原因都使用固定代码，不保存异常原文。

- [ ] **Step 5: 运行 CRUD 状态机测试**

Run:

```powershell
uv run pytest tests/test_agent_crud_hitl.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交 Task 2**

```powershell
git add backend/app/crud/hitl_proposal.py backend/tests/test_agent_crud_hitl.py
git commit -m "完善 HITL 持久化执行状态机" -m "- 使用带旧状态条件的数据库更新原子认领 APPROVED 提案。" -m "- 增加 EXECUTING、UNKNOWN、策略拒绝和人工恢复转换，禁止不确定结果自动重试。"
```

---

### Task 3: 独立 HITL 执行服务与策略复检

**Files:**
- Create: `backend/app/agent/hitl_execution.py`
- Create: `backend/tests/test_agent_hitl_execution.py`
- Modify: `backend/app/agent/hitl.py:118-590`
- Modify: `backend/app/agent/hitl_gate.py:38-190`
- Modify: `backend/app/agent/hitl_tools.py:1-266`
- Modify: `backend/tests/test_agent_hitl.py`
- Modify: `backend/tests/test_agent_hitl_tools.py`
- Modify: `backend/tests/test_hitl_integration.py`

**Interfaces:**
- Produces `NotifyExecutorProtocol`：

```python
class NotifyExecutorProtocol(Protocol):
    async def execute(
        self,
        db: AsyncSession,
        *,
        proposal_id: int,
        payload: Mapping[str, object],
        actor_user_id: int | None,
    ) -> ExecutionResult:
        raise NotImplementedError


class DeviceExecutorProtocol(Protocol):
    async def execute(
        self,
        db: AsyncSession,
        *,
        asset: CmdbAsset,
        command_name: str,
        dynamic_password: str | None,
        interface_name: str | None = None,
    ) -> ExecutionResult:
        raise NotImplementedError
```

- Produces `execute_approved_proposal(session_factory, proposal_id, actor_user_id, publisher, dynamic_password, notify_executor, device_executor) -> ProposalSafeSummary`。
- Produces `reconcile_executing_proposals(session_factory) -> int`。
- Private helpers are fixed as `_preflight_and_claim(session_factory, proposal_id, dynamic_password, publisher) -> PreparedExecution | ProposalSafeSummary`、`_execute_prepared(db, prepared, actor_user_id, dynamic_password, notify_executor, device_executor) -> ExecutionResult`、`_mark_execution_unknown(session_factory, proposal_id, publisher) -> ProposalSafeSummary` and `_publish_execution_summary(publisher, proposal) -> None`; `PreparedExecution` is a frozen dataclass containing proposal ID、session ID、action type、copied payload、optional detached asset。

- Consumes: Task 2 的原子状态转换。
- `HitlGateHook.before` 对 gated tool 始终返回完整结果并阻止 base dispatcher 再执行一次。

- [ ] **Step 1: 写策略漂移失败测试**

```python
async def _approved_device_proposal(
    db: AsyncSession,
    user: User,
) -> tuple[HitlProposal, int]:
    session = await agent_session_crud.create(
        db,
        {"user_id": user.id, "title": "execution", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "network",
            "hostname": "switch-42",
            "ip_address": "10.0.0.42",
            "business_system": "test",
            "subnet_cidr": "",
            "vendor": "cisco_iosxe",
            "credential_type": "dynamic",
            "credential_username": "admin",
        },
    )
    proposal = await hitl_proposal_crud.create(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="device_query",
        action_payload={
            "asset_id": asset.id,
            "command_name": "show_version",
            "proposal_reason": "verify policy drift",
        },
    )
    await hitl_proposal_crud.decide(
        db, proposal.id, approve=True, reviewed_by_user_id=user.id
    )
    await db.commit()
    return proposal, asset.id


async def _approved_notify_proposal(
    db: AsyncSession,
    user: User,
) -> HitlProposal:
    session = await agent_session_crud.create(
        db,
        {"user_id": user.id, "title": "notify", "status": "active"},
    )
    proposal = await hitl_proposal_crud.create(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "test notification", "proposal_reason": "test"},
    )
    await hitl_proposal_crud.decide(
        db, proposal.id, approve=True, reviewed_by_user_id=user.id
    )
    await db.commit()
    return proposal


class RecordingDeviceExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str | None, str | None]] = []

    async def execute(
        self,
        db: AsyncSession,
        *,
        asset: CmdbAsset,
        command_name: str,
        dynamic_password: str | None,
        interface_name: str | None = None,
    ) -> ExecutionResult:
        self.calls.append(
            (asset.id, command_name, dynamic_password, interface_name)
        )
        return ExecutionResult(ok=True, message="ok")


async def test_blacklist_added_after_approval_blocks_execution(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    proposal, asset_id = await _approved_device_proposal(
        db_session, test_user
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_version",
            "decision": "blacklist",
        },
    )
    await db_session.commit()
    fake_device_executor = RecordingDeviceExecutor()
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    summary = await execute_approved_proposal(
        session_factory=session_factory,
        proposal_id=proposal.id,
        actor_user_id=test_user.id,
        dynamic_password="one-use-password",
        device_executor=fake_device_executor,
    )

    assert summary.status == "REJECTED"
    assert fake_device_executor.calls == []
    db_session.expire_all()
    persisted = await hitl_proposal_crud.get(db_session, proposal.id)
    assert persisted is not None
    assert persisted.status_reason == "policy_blacklisted"
```

再增加“命令定义不存在”“动态凭据未提供”发生在认领前，状态仍为 APPROVED 且执行器未调用。

- [ ] **Step 2: 写执行提交顺序与 UNKNOWN 失败测试**

```python
async def test_executor_observes_committed_executing_state(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    proposal = await _approved_notify_proposal(db_session, test_user)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    observed: list[str] = []

    class FakeExecutor:
        async def execute(self, db, *, proposal_id, payload, actor_user_id):
            async with session_factory() as observer:
                persisted = await observer.get(HitlProposal, proposal_id)
                assert persisted is not None
                observed.append(persisted.status)
            return ExecutionResult(ok=True, message="ok")

    result = await execute_approved_proposal(
        session_factory=session_factory,
        proposal_id=proposal.id,
        actor_user_id=test_user.id,
        notify_executor=FakeExecutor(),
    )
    assert observed == ["EXECUTING"]
    assert result.status == "EXECUTED"
```

再添加执行器抛 `TimeoutError` 的测试，断言最终状态是 UNKNOWN，且再次调用执行服务抛 `HitlResumeError`。

- [ ] **Step 3: 运行新执行服务测试并确认失败**

Run:

```powershell
uv run pytest tests/test_agent_hitl_execution.py -q
```

Expected: FAIL，模块和接口尚不存在。

- [ ] **Step 4: 实现执行服务**

```python
async def execute_approved_proposal(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    proposal_id: int,
    actor_user_id: int | None,
    publisher: HitlEventPublisher | None = None,
    dynamic_password: str | None = None,
    notify_executor: NotifyExecutorProtocol | None = None,
    device_executor: DeviceExecutorProtocol | None = None,
) -> ProposalSafeSummary:
    prepared = await _preflight_and_claim(
        session_factory=session_factory,
        proposal_id=proposal_id,
        dynamic_password=dynamic_password,
        publisher=publisher,
    )
    if isinstance(prepared, ProposalSafeSummary):
        return prepared

    try:
        async with session_factory() as execution_db:
            result = await _execute_prepared(
                execution_db,
                prepared,
                actor_user_id=actor_user_id,
                dynamic_password=dynamic_password,
                notify_executor=notify_executor or NotifyExecutor(),
                device_executor=device_executor or DeviceQueryExecutor(),
            )
            if result.ok:
                await execution_db.commit()
            else:
                await execution_db.rollback()
    except asyncio.CancelledError:
        await _mark_execution_unknown(session_factory, proposal_id, publisher)
        raise
    except Exception:
        return await _mark_execution_unknown(
            session_factory, proposal_id, publisher
        )

    if not result.ok:
        return await _mark_execution_unknown(session_factory, proposal_id, publisher)

    async with session_factory() as finish_db:
        finished = await hitl_proposal_crud.mark_executed(finish_db, proposal_id)
        await finish_db.commit()
    await _publish_execution_summary(publisher, finished)
    return _summary(finished)
```

`_preflight_and_claim` 在同一短事务内完成资产/命令/凭据结构校验、`resolve_policy` 复检、策略拒绝或 claim+commit。`_execute_prepared` 只按 prepared action type 调用上述两个协议之一。只捕获执行开始后的异常并写固定原因 `dispatch_outcome_unknown`；日志可以记录异常类型，但响应、状态原因和事件不得包含异常原文。取消异常完成 UNKNOWN 持久化后必须继续抛出。

- [ ] **Step 5: 改造聊天 HITL 钩子**

`HitlGateHook` 使用注入的 `async_sessionmaker` 在独立短会话中创建并提交提案。PENDING 返回 `pending_approval`；自动批准时调用 `execute_approved_proposal`，然后返回 `block=True` 的 ToolResult。删除“base dispatcher 先执行、after hook 再 attach”的双阶段路径，避免同一命令被两套路径执行。

```python
if summary.status == "PENDING":
    return BeforeToolDecision(block=True, result=_pending_result(summary))

executed = await execute_approved_proposal(
    session_factory=self._session_factory,
    proposal_id=summary.proposal_id,
    actor_user_id=self._actor_user_id,
    publisher=self._publisher,
)
return BeforeToolDecision(block=True, result=_tool_result_from_summary(executed))
```

- [ ] **Step 6: 更新旧测试并运行 HITL 相关测试**

旧的“连接失败后保持 APPROVED”断言改为 UNKNOWN；旧 retry 测试改成“UNKNOWN 直接 retry 被拒绝，人工 allow_retry 后才能执行”。保留动态密码不落库和子 Agent 禁止写工具的断言。

Run:

```powershell
uv run pytest tests/test_agent_hitl_execution.py tests/test_agent_hitl.py tests/test_agent_hitl_tools.py tests/test_hitl_integration.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交 Task 3**

```powershell
git add backend/app/agent/hitl_execution.py backend/app/agent/hitl.py backend/app/agent/hitl_gate.py backend/app/agent/hitl_tools.py backend/tests/test_agent_hitl_execution.py backend/tests/test_agent_hitl.py backend/tests/test_agent_hitl_tools.py backend/tests/test_hitl_integration.py
git commit -m "隔离 HITL 外部执行并复检命令策略" -m "- 在调用设备前提交 EXECUTING，并在策略变黑名单时阻止旧提案执行。" -m "- 将执行后的异常统一持久化为 UNKNOWN，聊天与 HTTP 路径复用同一安全语义。"
```

---

### Task 4: UNKNOWN API、人工处置与启动恢复

**Files:**
- Modify: `backend/app/schemas/hitl.py:1-52`
- Modify: `backend/app/api/v1/hitl.py:39-233`
- Modify: `backend/app/schemas/agent_ws.py:15-33`
- Modify: `backend/app/agent/ws_hub.py:90-170`
- Modify: `backend/app/main.py:62-80`
- Modify: `backend/tests/test_hitl_api.py`
- Modify: `backend/tests/test_agent_recovery.py`
- Modify: `backend/tests/test_agent_ws_hub.py`

**Interfaces:**
- Produces: `HitlUnknownResolutionRequest(resolution: Literal["confirm_executed", "allow_retry"])`。
- Produces: `POST /api/v1/hitl/proposals/{proposal_id}/resolve-unknown`。
- `HitlProposalResponse` 增加 execution/recovery 字段。
- 启动 lifespan 在 Spawn reconcile 前调用 `reconcile_executing_proposals`。

- [ ] **Step 1: 写 UNKNOWN API 失败测试**

```python
@pytest_asyncio.fixture
async def unknown_proposal(
    db_session: AsyncSession,
    test_user: User,
) -> HitlProposal:
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "unknown", "status": "active"},
    )
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "test", "proposal_reason": "test"},
    )
    await hitl_proposal_crud.decide(
        db_session,
        proposal.id,
        approve=True,
        reviewed_by_user_id=test_user.id,
    )
    await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    await hitl_proposal_crud.mark_unknown(
        db_session, proposal.id, reason="dispatch_outcome_unknown"
    )
    await db_session.commit()
    return proposal


async def test_resolve_unknown_requires_permission_and_records_actor(
    client, auth_headers, db_session, unknown_proposal, test_user
) -> None:
    response = await client.post(
        f"/api/v1/hitl/proposals/{unknown_proposal.id}/resolve-unknown",
        json={"resolution": "allow_retry"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "APPROVED"

    persisted = await hitl_proposal_crud.get(db_session, unknown_proposal.id)
    assert persisted.resolved_by_user_id == test_user.id
    assert persisted.status_reason == "retry_authorized"
```

增加非法 resolution 返回 422、非 UNKNOWN 返回 409、无权限返回 403、`confirm_executed` 写 executed_at 和审计动作 `hitl_unknown_confirmed`。

- [ ] **Step 2: 写启动恢复失败测试**

```python
async def test_startup_reconciles_executing_to_unknown(
    db_engine: AsyncEngine,
    test_user: User,
) -> None:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as db:
        session = await agent_session_crud.create(
            db,
            {"user_id": test_user.id, "title": "recovery", "status": "active"},
        )
        proposal = await hitl_proposal_crud.create(
            db,
            session_id=session.id,
            proposed_by_agent_id=None,
            action_type="notify",
            action_payload={"message": "test"},
        )
        await hitl_proposal_crud.decide(
            db, proposal.id, approve=True, reviewed_by_user_id=test_user.id
        )
        await hitl_proposal_crud.claim_execution(db, proposal.id)
        await db.commit()
        proposal_id = proposal.id

    changed = await reconcile_executing_proposals(session_factory)
    assert changed == 1
    async with session_factory() as db:
        persisted = await hitl_proposal_crud.get(db, proposal_id)
        assert persisted is not None
        assert persisted.status == "UNKNOWN"
```

- [ ] **Step 3: 运行 API 与恢复测试并确认失败**

Run:

```powershell
uv run pytest tests/test_hitl_api.py tests/test_agent_recovery.py -q
```

Expected: FAIL，新请求模型、路由和启动恢复调用不存在。

- [ ] **Step 4: 实现请求模型、响应字段和 API**

```python
class HitlUnknownResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Literal["confirm_executed", "allow_retry"]


@router.post(
    "/proposals/{proposal_id}/resolve-unknown",
    response_model=ResponseEnvelope[HitlProposalResponse],
)
async def resolve_unknown_proposal(
    proposal_id: int,
    body: HitlUnknownResolutionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[HitlProposalResponse]:
    publisher = BufferedWsHitlEventPublisher()
    proposal = await hitl_proposal_crud.resolve_unknown(
        db,
        proposal_id,
        resolution=body.resolution,
        resolved_by_user_id=current_user.id,
    )
    await log_audit(
        db,
        user_id=current_user.id,
        action="hitl_unknown_confirmed"
        if body.resolution == "confirm_executed"
        else "hitl_unknown_retry_authorized",
        target=f"hitl_proposal:{proposal_id}",
        detail=body.resolution,
    )
    await db.commit()
    await publisher.publish(
        session_id=proposal.session_id,
        event_type="hitl_resolved",
        payload={
            "proposal_id": proposal.id,
            "action_type": proposal.action_type,
            "status": proposal.status,
            "status_reason": proposal.status_reason,
            "reason": str(proposal.action_payload.get("proposal_reason", "")),
            "asset_id": proposal.action_payload.get("asset_id"),
            "resolved_at": proposal.resolved_at,
        },
    )
    await publisher.flush()
    return success_response(await _to_response(db, proposal))
```

人工 approve API 必须先提交 APPROVED，再调用 Task 3 的执行服务；retry 只接受 APPROVED。更新 WS 安全字段白名单，允许 `status_reason`、`execution_started_at` 和 `resolved_at`，仍禁止 command/password。

- [ ] **Step 5: 接入启动恢复**

```python
await reconcile_executing_proposals(AsyncSessionLocal)
await spawn_manager.reconcile_startup()
```

把这两行放在现有 `lifespan` 创建 receipt GC task 之前；现有 yield、GC cancel、spawn shutdown 顺序保持不变。

恢复过程只广播服务启动后的安全状态，不尝试重新执行。

- [ ] **Step 6: 运行 API、WS 与恢复测试**

Run:

```powershell
uv run pytest tests/test_hitl_api.py tests/test_agent_recovery.py tests/test_agent_ws_hub.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交 Task 4**

```powershell
git add backend/app/schemas/hitl.py backend/app/api/v1/hitl.py backend/app/schemas/agent_ws.py backend/app/agent/ws_hub.py backend/app/main.py backend/tests/test_hitl_api.py backend/tests/test_agent_recovery.py backend/tests/test_agent_ws_hub.py
git commit -m "增加 HITL 不确定结果人工处置" -m "- 提供 UNKNOWN 确认已执行和允许重试接口，并记录管理员与审计动作。" -m "- 服务启动时把遗留 EXECUTING 恢复为 UNKNOWN，禁止崩溃后自动重复命令。"
```

---

### Task 5: 根会话 Turn 租约与完整 transcript 单元

**Files:**
- Modify: `backend/app/crud/agent_session.py:10-52`
- Modify: `backend/app/agent/loop.py:83-190`
- Modify: `backend/app/agent/chat_turn.py:61-181`
- Modify: `backend/app/api/v1/agent_sessions.py:209-245`
- Modify: `backend/app/main.py:62-80`
- Modify: `backend/tests/test_agent_crud_session.py`
- Modify: `backend/tests/test_agent_loop.py`
- Modify: `backend/tests/test_chat_turn.py`
- Modify: `backend/tests/test_agent_sessions_api.py`

**Interfaces:**
- Produces:

`claim_turn(db: AsyncSession, session_id: int, token: str) -> bool`、`release_turn(db: AsyncSession, session_id: int, token: str) -> bool`、`recover_active_turns(db: AsyncSession) -> int`。

- `run_chat_turn` 不再保存用户消息；POST API 在调用它前保存并提交。
- `run_loop` 只在全部工具结果收集完成后写 assistant/tool 消息单元。

- [ ] **Step 1: 写 turn 租约 CRUD 失败测试**

```python
async def test_turn_lease_is_owner_token_guarded(db_session, agent_session) -> None:
    assert await agent_session_crud.claim_turn(db_session, agent_session.id, "token-a")
    assert not await agent_session_crud.claim_turn(db_session, agent_session.id, "token-b")
    assert not await agent_session_crud.release_turn(db_session, agent_session.id, "token-b")
    assert await agent_session_crud.release_turn(db_session, agent_session.id, "token-a")
```

增加 `recover_active_turns` 只清空非空 lease 的测试。

- [ ] **Step 2: 写并发 POST 失败测试**

```python
async def _post_message(
    client: AsyncClient,
    session_id: int,
    headers: dict[str, str],
    content: str,
) -> Response:
    return await client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": content},
        headers=headers,
    )


async def test_same_session_rejects_second_concurrent_turn(
    client, auth_headers, session_id, monkeypatch
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_turn(*args, **kwargs):
        entered.set()
        await release.wait()
        return LoopOutcome(reason="final_answer", final_answer="ok")

    monkeypatch.setattr(agent_sessions_api, "run_chat_turn", slow_turn)
    first = asyncio.create_task(_post_message(client, session_id, auth_headers, "A"))
    await entered.wait()
    second = await _post_message(client, session_id, auth_headers, "B")
    release.set()
    assert second.status_code == 409
    assert (await first).status_code == 200
```

- [ ] **Step 3: 写悬空 tool call 回归测试**

```python
async def test_dispatch_exception_does_not_persist_assistant_tool_call(
    db_session, session_id
) -> None:
    async def exploding_dispatch(name, arguments):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await run_loop(
            db_session,
            session_id=session_id,
            model_key="fake",
            chat_fn=_one_tool_call,
            dispatch_tool=exploding_dispatch,
        )
    await db_session.commit()
    rows = await agent_message_crud.list_for_agent(
        db_session, session_id, agent_id=None
    )
    assert all(row.tool_calls is None for row in rows)
```

- [ ] **Step 4: 运行测试并确认失败**

Run:

```powershell
uv run pytest tests/test_agent_crud_session.py tests/test_agent_loop.py tests/test_agent_sessions_api.py -q
```

Expected: FAIL，租约方法不存在；同会话并发都进入 turn；loop 先写 assistant tool call。

- [ ] **Step 5: 实现租约和完整单元写入**

```python
async def claim_turn(self, db, session_id, token):
    result = await db.execute(
        update(AgentSession)
        .where(
            AgentSession.id == session_id,
            AgentSession.active_turn_token.is_(None),
        )
        .values(
            active_turn_token=token,
            active_turn_started_at=datetime.now(UTC),
        )
    )
    return result.rowcount == 1
```

`run_loop` 先把每个 `(tool_call, ToolResult)` 放入本地列表；若 before/dispatch/after 抛异常，列表不落库。全部完成或补齐 early-exit 的 skipped result 后，按“assistant 一条 + 每个 tool 一条”写入同一事务。

- [ ] **Step 6: 改造 POST 事务流程**

```python
turn_token = str(uuid4())
if not await agent_session_crud.claim_turn(db, session_id, turn_token):
    raise HTTPException(status_code=409, detail="该会话正在处理上一条消息")
await db.commit()

try:
    await append_user_message(db, session_id, body.content)
    await db.commit()
    outcome = await run_chat_turn(
        db,
        session_id=session_id,
        actor_user_id=current_user.id,
    )
    await db.commit()
except Exception:
    await db.rollback()
    raise
finally:
    await db.rollback()
    await agent_session_crud.release_turn(db, session_id, turn_token)
    await db.commit()
```

`run_chat_turn` 删除 `content` 参数和内部 `append_user_message`。启动 lifespan 调用 `recover_active_turns`。

- [ ] **Step 7: 运行会话和 loop 测试**

Run:

```powershell
uv run pytest tests/test_agent_crud_session.py tests/test_agent_loop.py tests/test_chat_turn.py tests/test_agent_sessions_api.py -q
```

Expected: PASS；并发第二条返回 409；异常时用户消息仍在，assistant/tool 不悬空。

- [ ] **Step 8: 提交 Task 5**

```powershell
git add backend/app/crud/agent_session.py backend/app/agent/loop.py backend/app/agent/chat_turn.py backend/app/api/v1/agent_sessions.py backend/app/main.py backend/tests/test_agent_crud_session.py backend/tests/test_agent_loop.py backend/tests/test_chat_turn.py backend/tests/test_agent_sessions_api.py
git commit -m "串行化根会话并保证工具消息完整" -m "- 使用数据库 turn token 拒绝同一会话的并发请求，并在启动时清理遗留租约。" -m "- 工具调度完成后再成组写 assistant 与 tool 消息，异常时不留下悬空调用。"
```

---

### Task 6: 工具调用感知的上下文压缩边界

**Files:**
- Modify: `backend/app/agent/compaction.py:79-190`
- Modify: `backend/app/agent/session.py:29-95`
- Modify: `backend/tests/test_agent_compaction.py`
- Modify: `backend/tests/test_agent_session.py`

**Interfaces:**
- Produces: `_message_units(rows) -> list[tuple[int, int]]`，索引区间使用左闭右开。
- Produces: `_safe_compaction_cut_index(rows, recent_raw_count) -> int`。
- `_messages_to_summarize` 只返回完整消息单元，并只推进到最后一个完整单元末尾。

- [ ] **Step 1: 写 17 行边界回归测试**

```python
def _message(
    message_id: int,
    role: str,
    *,
    content: str = "",
    tool_calls: list[dict[str, str]] | None = None,
    tool_call_id: str | None = None,
) -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id=1,
        agent_id=None,
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
    )


def test_compaction_keeps_assistant_and_tool_result_together() -> None:
    rows = [
        _message(1, "assistant", tool_calls=[{"id": "tc-1", "name": "query", "arguments": "{}"}]),
        _message(2, "tool", tool_call_id="tc-1", content="result"),
        *[_message(i, "user", content=f"m-{i}") for i in range(3, 18)],
    ]

    selected = _messages_to_summarize(rows, compacted_through_message_id=None)
    assert selected == []
```

这个用例固定复现旧逻辑的 `cut_ids=[1]`。再写 18 行版本，断言 assistant 和 tool 两行一起进入摘要。

- [ ] **Step 2: 写多工具与不完整单元测试**

```python
def test_incomplete_multi_tool_group_never_advances_cutoff() -> None:
    rows = [
        _message(
            1,
            "assistant",
            tool_calls=[
                {"id": "a", "name": "one", "arguments": "{}"},
                {"id": "b", "name": "two", "arguments": "{}"},
            ],
        ),
        _message(2, "tool", tool_call_id="a", content="only-one-result"),
        *[_message(i, "user", content="x") for i in range(3, 20)],
    ]
    assert _messages_to_summarize(rows, None) == []
```

另加完整多工具组、已有 summary 后连续压缩、摘要模型失败不改变 `compacted_through_message_id` 的测试。

- [ ] **Step 3: 运行压缩测试并确认失败**

Run:

```powershell
uv run pytest tests/test_agent_compaction.py tests/test_agent_session.py -q
```

Expected: 新边界测试 FAIL，旧代码拆开 assistant/tool。

- [ ] **Step 4: 实现完整消息单元解析**

```python
def _message_units(rows: list[AgentMessage]) -> list[tuple[int, int]]:
    units: list[tuple[int, int]] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        if row.role != "assistant" or not row.tool_calls:
            units.append((index, index + 1))
            index += 1
            continue

        expected = {call["id"] for call in row.tool_calls}
        found: set[str] = set()
        end = index + 1
        while end < len(rows) and rows[end].role == "tool":
            if rows[end].tool_call_id in expected:
                found.add(rows[end].tool_call_id)
            end += 1
        if found != expected:
            break
        units.append((index, end))
        index = end
    return units
```

`_safe_compaction_cut_index` 计算原始目标 `len(rows) - COMPACT_RECENT_RAW_MESSAGES`，只选择 `unit_end <= raw_target` 的最后一个完整单元末尾。已有 compaction cursor 的过滤在安全切点确定后执行。

- [ ] **Step 5: 保留模型历史防御并修复 summary 类型收窄**

`build_model_history` 继续丢弃历史开头的孤立 tool 作为旧数据防御，但新增测试断言正常压缩结果不需要该分支。将摘要内容先收窄：

```python
summary = result.content
if result.finish_reason == "error" or summary is None or not summary.strip():
    return
session.memory_summary = summary.strip()
```

- [ ] **Step 6: 运行压缩、会话和 mypy 局部检查**

Run:

```powershell
uv run pytest tests/test_agent_compaction.py tests/test_agent_session.py -q
uv run mypy app/agent/compaction.py app/agent/session.py
```

Expected: tests PASS；mypy 零错误。

- [ ] **Step 7: 提交 Task 6**

```powershell
git add backend/app/agent/compaction.py backend/app/agent/session.py backend/tests/test_agent_compaction.py backend/tests/test_agent_session.py
git commit -m "按完整工具单元压缩 Agent 上下文" -m "- 压缩切点不再拆开 assistant tool call 与对应结果。" -m "- 不完整多工具调用不会进入摘要或推进压缩游标，保留合法模型消息序列。"
```

---

### Task 7: 后端会话安全快照与 Cursor 分页

**Files:**
- Modify: `backend/app/crud/agent_message.py:42-115`
- Modify: `backend/app/crud/hitl_proposal.py`
- Modify: `backend/app/crud/agent_registry.py:152-260`
- Modify: `backend/app/schemas/agent_session.py:40-80`
- Modify: `backend/app/api/v1/agent_sessions.py:190-245`
- Modify: `backend/tests/test_agent_crud_message.py`
- Modify: `backend/tests/test_agent_sessions_api.py`

**Interfaces:**
- Produces `list_root_before_id(db: AsyncSession, session_id: int, before_id: int | None, limit: int) -> tuple[list[AgentMessage], bool]`。
- Produces `list_non_terminal_for_session(db: AsyncSession, session_id: int) -> list[HitlProposal]`。
- Produces `list_snapshot_for_session(db: AsyncSession, session_id: int, terminal_limit: int = 20) -> list[AgentRegistry]`。

- Produces: `AgentSessionSnapshotResponse`，字段为 `messages`、`proposals`、`children`、`has_more_messages`、`next_before_message_id`。
- Produces: `GET /api/v1/agent/sessions/{session_id}/snapshot`。

- [ ] **Step 1: 写 cursor CRUD 失败测试**

```python
async def test_list_root_before_id_pages_newest_messages_oldest_first(
    db_session, session_id
) -> None:
    for index in range(1, 7):
        await agent_message_crud.append(
            db_session,
            session_id=session_id,
            agent_id=None,
            role="user",
            content=str(index),
        )
    rows, has_more = await agent_message_crud.list_root_before_id(
        db_session, session_id, before_id=None, limit=3
    )
    assert [row.content for row in rows] == ["4", "5", "6"]
    assert has_more is True

    older, older_has_more = await agent_message_crud.list_root_before_id(
        db_session, session_id, before_id=rows[0].id, limit=3
    )
    assert [row.content for row in older] == ["1", "2", "3"]
    assert older_has_more is False
```

- [ ] **Step 2: 写快照 API 安全与分页失败测试**

```python
async def test_snapshot_returns_safe_recoverable_state(
    client, auth_headers, session_with_messages_proposal_and_child
) -> None:
    response = await client.get(
        f"/api/v1/agent/sessions/{session_with_messages_proposal_and_child}/snapshot",
        params={"limit": 2},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["messages"]) == 2
    assert data["has_more_messages"] is True
    assert set(data["proposals"][0]) <= {
        "proposal_id", "action_type", "status", "status_reason",
        "reason", "asset_id", "created_at", "execution_started_at",
        "resolved_at",
    }
    assert "password" not in response.text
    assert "action_payload" not in response.text
```

增加非所有者 404、child transcript 不进入 messages、只返回 PENDING/APPROVED/EXECUTING/UNKNOWN 提案、active children 加最近 20 个 terminal children 的测试。

- [ ] **Step 3: 运行 CRUD/API 测试并确认失败**

Run:

```powershell
uv run pytest tests/test_agent_crud_message.py tests/test_agent_sessions_api.py -q
```

Expected: FAIL，分页 CRUD、快照 DTO 和路由不存在。

- [ ] **Step 4: 实现分页查询**

```python
async def list_root_before_id(
    self,
    db: AsyncSession,
    session_id: int,
    *,
    before_id: int | None,
    limit: int,
) -> tuple[list[AgentMessage], bool]:
    stmt = select(AgentMessage).where(
        AgentMessage.session_id == session_id,
        AgentMessage.agent_id.is_(None),
    )
    if before_id is not None:
        stmt = stmt.where(AgentMessage.id < before_id)
    rows = list(
        (await db.execute(stmt.order_by(AgentMessage.id.desc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    return rows, has_more
```

现有 `GET /messages` 增加 `limit=100`、最大 200，至少保证旧接口不再无上限返回；前端在 Task 8 改用 snapshot。

- [ ] **Step 5: 定义安全 DTO**

```python
class HitlProposalSafeResponse(ApiModel):
    proposal_id: int
    action_type: str
    status: str
    status_reason: str | None
    reason: str
    asset_id: int | None
    created_at: datetime
    execution_started_at: datetime | None
    resolved_at: datetime | None


class ChildAgentSnapshotResponse(ApiModel):
    child_id: str
    role: str
    task_brief: str
    status: str
    result_summary: str | None
    created_at: datetime
    status_changed_at: datetime


class AgentSessionSnapshotResponse(ApiModel):
    messages: list[AgentMessageResponse]
    proposals: list[HitlProposalSafeResponse]
    children: list[ChildAgentSnapshotResponse]
    has_more_messages: bool
    next_before_message_id: int | None
```

DTO 转换只从白名单字段组装，不对 ORM 对象做无选择序列化。

- [ ] **Step 6: 实现 snapshot 路由**

```python
@router.get(
    "/sessions/{session_id}/snapshot",
    response_model=ResponseEnvelope[AgentSessionSnapshotResponse],
)
async def get_session_snapshot(
    session_id: int,
    before_message_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[AgentSessionSnapshotResponse]:
    await _owned_session_or_404(db, session_id, current_user.id)
    messages, has_more = await agent_message_crud.list_root_before_id(
        db, session_id, before_id=before_message_id, limit=limit
    )
    proposals = await hitl_proposal_crud.list_non_terminal_for_session(db, session_id)
    children = await agent_registry_crud.list_snapshot_for_session(db, session_id)
    next_before = messages[0].id if has_more and messages else None
    return success_response(_snapshot_response(messages, proposals, children, has_more, next_before))
```

- [ ] **Step 7: 运行快照、权限和分页测试**

Run:

```powershell
uv run pytest tests/test_agent_crud_message.py tests/test_agent_sessions_api.py tests/test_agent_crud_registry.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交 Task 7**

```powershell
git add backend/app/crud/agent_message.py backend/app/crud/hitl_proposal.py backend/app/crud/agent_registry.py backend/app/schemas/agent_session.py backend/app/api/v1/agent_sessions.py backend/tests/test_agent_crud_message.py backend/tests/test_agent_sessions_api.py backend/tests/test_agent_crud_registry.py
git commit -m "增加可恢复的 Agent 会话安全快照" -m "- 使用 cursor 分页返回根消息，避免长会话一次加载全部审计记录。" -m "- 快照只暴露 HITL 与子 Agent 安全摘要，为刷新和断线恢复提供数据库真相。"
```

---

### Task 8: 前端快照恢复、请求竞态与历史分页

**Files:**
- Modify: `frontend/src/types/agent.ts:1-75`
- Modify: `frontend/src/lib/agent-api.ts:105-150`
- Modify: `frontend/src/hooks/use-ops-chat.ts:34-500`
- Modify: `frontend/src/hooks/use-agent-ws.ts:19-144`
- Modify: `frontend/src/hooks/ops-chat-reducer.test.ts`
- Create: `frontend/src/hooks/use-ops-chat.test.tsx`
- Modify: `frontend/src/components/ops-assistant/ChatMessageList.tsx`
- Modify: `frontend/src/pages/OpsAssistantPage.tsx:85-420`

**Interfaces:**
- Produces: TypeScript `AgentSessionSnapshot`、`HitlProposalSafeSummary`、`ChildAgentSnapshot`。
- Produces:

```typescript
getAgentSessionSnapshot(
  sessionId: number,
  params?: { before_message_id?: number; limit?: number },
  signal?: AbortSignal,
): Promise<AgentSessionSnapshot>
```

- `UseOpsChatResult` 增加 `reloadSnapshot`、`loadOlder`、`hasMore`、`isLoadingOlder`。

- [ ] **Step 1: 写 reducer 快照去重失败测试**

```typescript
function buildSnapshot(
  overrides: Partial<AgentSessionSnapshot> = {},
): AgentSessionSnapshot {
  return Object.assign({
    messages: [],
    proposals: [],
    children: [],
    has_more_messages: false,
    next_before_message_id: null,
  }, overrides)
}

function message(
  overrides: Partial<AgentMessage> & Pick<AgentMessage, "id" | "role" | "content">,
): AgentMessage {
  return Object.assign({
    session_id: 1,
    tool_call_id: null,
    tool_calls: null,
    created_at: "2026-08-14T00:00:00Z",
  }, overrides)
}

function proposal(
  overrides: Partial<HitlProposalSafeSummary> &
    Pick<HitlProposalSafeSummary, "proposal_id" | "status">,
): HitlProposalSafeSummary {
  return Object.assign({
    action_type: "notify",
    status_reason: null,
    reason: "test",
    asset_id: null,
    created_at: "2026-08-14T00:00:00Z",
    execution_started_at: null,
    resolved_at: null,
  }, overrides)
}


it("hydrates messages and pending proposals with stable ids", () => {
  const state = reduceOpsChat({ items: [] }, {
    type: "snapshot_loaded",
    replace: true,
    snapshot: buildSnapshot({
      messages: [message({ id: 10, role: "assistant", content: "done" })],
      proposals: [proposal({ proposal_id: 7, status: "PENDING" })],
    }),
  })
  expect(state.items.map((item) => item.id)).toEqual([
    "message:10",
    "hitl:7",
  ])
})
```

增加同一 snapshot 重放不重复、older page 前插、replace 保留 pending optimistic user、服务端最终 assistant 替换 streaming 临时项的测试。

- [ ] **Step 2: 写旧请求覆盖新会话的 hook 失败测试**

```typescript
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function snapshotFor(sessionId: number, content: string): AgentSessionSnapshot {
  return buildSnapshot({
    messages: [message({ id: sessionId, session_id: sessionId, role: "assistant", content })],
  })
}


it("ignores a snapshot response from the previous session", async () => {
  const first = deferred<AgentSessionSnapshot>()
  const second = deferred<AgentSessionSnapshot>()
  mockGetSnapshot.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)

  const { result, rerender } = renderHook(
    ({ sessionId }) => useOpsChat({ sessionId }),
    { initialProps: { sessionId: 1 } },
  )
  rerender({ sessionId: 2 })
  second.resolve(snapshotFor(2, "new"))
  first.resolve(snapshotFor(1, "old"))

  await waitFor(() => expect(result.current.messages).toContainEqual(
    expect.objectContaining({ content: "new" }),
  ))
  expect(result.current.messages).not.toContainEqual(
    expect.objectContaining({ content: "old" }),
  )
})
```

在同一文件测试 POST 完成后调用 snapshot、WS disconnected→connected 后调用 snapshot、unmount 会 abort 请求。

- [ ] **Step 3: 运行前端测试并确认失败**

Run:

```powershell
npm test -- src/hooks/ops-chat-reducer.test.ts src/hooks/use-ops-chat.test.tsx
```

Expected: FAIL，快照类型、API 和 reducer action 不存在。

- [ ] **Step 4: 添加快照类型和 API**

```typescript
export interface AgentSessionSnapshot {
  messages: AgentMessage[]
  proposals: HitlProposalSafeSummary[]
  children: ChildAgentSnapshot[]
  has_more_messages: boolean
  next_before_message_id: number | null
}

export async function getAgentSessionSnapshot(
  sessionId: number,
  params: { before_message_id?: number; limit?: number } = {},
  signal?: AbortSignal,
): Promise<AgentSessionSnapshot> {
  const response = await api.get<ApiResponse<AgentSessionSnapshot>>(
    `/agent/sessions/${sessionId}/snapshot`,
    { params, signal },
  )
  return response.data.data
}
```

- [ ] **Step 5: 实现请求世代与恢复时机**

```typescript
const requestGenerationRef = useRef(0)

const reloadSnapshot = useCallback(async () => {
  if (sessionId == null) return
  const generation = ++requestGenerationRef.current
  const controller = new AbortController()
  snapshotAbortRef.current?.abort()
  snapshotAbortRef.current = controller
  const snapshot = await getAgentSessionSnapshot(sessionId, {}, controller.signal)
  if (
    controller.signal.aborted ||
    generation !== requestGenerationRef.current ||
    sessionId !== activeSessionIdRef.current
  ) return
  dispatch({ type: "snapshot_loaded", snapshot, replace: true })
}, [sessionId])
```

`sendMessage` 在 HTTP 完成的 `finally` 中 await `reloadSnapshot()`。记录前一 WS 状态，只在非 connected→connected 时同步，避免每次 render 重拉。

- [ ] **Step 6: 实现向上分页**

`loadOlder` 使用 `next_before_message_id` 请求 snapshot，并 dispatch `replace:false`。ChatMessageList 顶部放 `IntersectionObserver` sentinel；只有 `hasMore && !isLoadingOlder` 时调用一次，加载后保持用户当前滚动位置，不自动跳到底部。

```typescript
useEffect(() => {
  const node = topSentinelRef.current
  if (!node || !hasMore || isLoadingOlder) return
  const observer = new IntersectionObserver(([entry]) => {
    if (entry.isIntersecting) void onLoadOlder()
  })
  observer.observe(node)
  return () => observer.disconnect()
}, [hasMore, isLoadingOlder, onLoadOlder])
```

- [ ] **Step 7: 运行 reducer、hook、消息列表测试和类型检查**

Run:

```powershell
npm test -- src/hooks/ops-chat-reducer.test.ts src/hooks/use-ops-chat.test.tsx
npm run typecheck
```

Expected: PASS。

- [ ] **Step 8: 提交 Task 8**

```powershell
git add frontend/src/types/agent.ts frontend/src/lib/agent-api.ts frontend/src/hooks/use-ops-chat.ts frontend/src/hooks/use-agent-ws.ts frontend/src/hooks/ops-chat-reducer.test.ts frontend/src/hooks/use-ops-chat.test.tsx frontend/src/components/ops-assistant/ChatMessageList.tsx frontend/src/pages/OpsAssistantPage.tsx
git commit -m "用会话快照恢复运维助手状态" -m "- 在初次加载、切换、重连和消息完成后同步数据库快照，并阻止旧请求覆盖新会话。" -m "- 增加 cursor 历史分页和稳定 ID 去重，恢复错过的回答与审批状态。"
```

---

### Task 9: UNKNOWN 审批卡与完全访问会话绑定

**Files:**
- Modify: `frontend/src/lib/hitl-api.ts:7-110`
- Modify: `frontend/src/components/ops-assistant/hitlApprovalCardUtils.ts`
- Modify: `frontend/src/components/ops-assistant/HitlApprovalCard.tsx:140-360`
- Modify: `frontend/src/components/ops-assistant/HitlApprovalCard.test.tsx`
- Modify: `frontend/src/pages/OpsAssistantPage.tsx:66-268`
- Create or Modify: `frontend/src/pages/OpsAssistantPage.test.tsx`

**Interfaces:**
- Produces:

```typescript
resolveUnknownHitlProposal(
  proposalId: number,
  resolution: "confirm_executed" | "allow_retry",
): Promise<HitlProposal>
```

- `HitlProposal` 增加 `execution_started_at`、`status_reason`、`resolved_by_user_id`、`resolved_at`。
- 完全访问弹窗状态改为 `fullAccessTargetSessionId: number | null`。

- [ ] **Step 1: 写 UNKNOWN 卡片失败测试**

```typescript
it("shows two administrator resolutions for UNKNOWN", async () => {
  mockUsePermission.mockReturnValue(permissionResult(true))
  mockGetHitlProposal.mockResolvedValue(buildProposal({ status: "UNKNOWN" }))
  render(<HitlApprovalCard proposalId={1} actionType="device_control" status="UNKNOWN" />)

  expect(await screen.findByTestId("hitl-confirm-executed-button")).toBeEnabled()
  expect(screen.getByTestId("hitl-allow-retry-button")).toBeEnabled()
  expect(screen.queryByTestId("hitl-retry-button")).not.toBeInTheDocument()
})
```

增加无审批权限只显示状态、点击两种按钮发送正确 resolution、UNKNOWN 不显示旧 retry 的测试。

- [ ] **Step 2: 写完全访问目标会话失败测试**

```typescript
it("confirms full access for the session that opened the dialog", async () => {
  render(<OpsAssistantPage />)
  await selectSession(1)
  await openFullAccessDialog()
  await selectSession(2)
  await confirmFullAccess()
  expect(mockPatchAgentSession).not.toHaveBeenCalledWith(
    2,
    { approval_mode: "full" },
  )
})
```

最终设计是切换会话自动关闭弹窗，因此测试同时断言确认按钮已消失；另一个测试在不切换时断言 patch session 1。

- [ ] **Step 3: 运行组件测试并确认失败**

Run:

```powershell
npm test -- src/components/ops-assistant/HitlApprovalCard.test.tsx src/pages/OpsAssistantPage.test.tsx
```

Expected: FAIL，UNKNOWN API/按钮和目标 session 状态不存在。

- [ ] **Step 4: 实现 UNKNOWN API 与卡片**

```typescript
export async function resolveUnknownHitlProposal(
  proposalId: number,
  resolution: "confirm_executed" | "allow_retry",
): Promise<HitlProposal> {
  const response = await api.post<ApiResponse<HitlProposal>>(
    `/hitl/proposals/${proposalId}/resolve-unknown`,
    { resolution },
  )
  return response.data.data
}
```

卡片只在 `status.toUpperCase() === "UNKNOWN" && canApprove` 时显示两个人工按钮。按钮文案明确要求管理员已检查设备；操作成功后用响应更新 local status，并清空动态密码。

- [ ] **Step 5: 绑定完全访问目标**

```typescript
const [fullAccessTargetSessionId, setFullAccessTargetSessionId] =
  useState<number | null>(null)

function requestApprovalMode(mode: ApprovalMode) {
  if (mode === "full" && selectedSessionId != null) {
    setFullAccessTargetSessionId(selectedSessionId)
    return
  }
  void patchApprovalMode(selectedSessionId, mode)
}

useEffect(() => {
  setFullAccessTargetSessionId(null)
}, [selectedSessionId])
```

确认函数必须把 `fullAccessTargetSessionId` 显式传给 `patchApprovalMode(targetId, "full")`，不得在确认时重新读取 selectedSessionId。

- [ ] **Step 6: 运行前端测试与类型检查**

Run:

```powershell
npm test -- src/components/ops-assistant/HitlApprovalCard.test.tsx src/pages/OpsAssistantPage.test.tsx
npm run typecheck
```

Expected: PASS。

- [ ] **Step 7: 提交 Task 9**

```powershell
git add frontend/src/lib/hitl-api.ts frontend/src/components/ops-assistant/hitlApprovalCardUtils.ts frontend/src/components/ops-assistant/HitlApprovalCard.tsx frontend/src/components/ops-assistant/HitlApprovalCard.test.tsx frontend/src/pages/OpsAssistantPage.tsx frontend/src/pages/OpsAssistantPage.test.tsx
git commit -m "增加 UNKNOWN 人工处置界面" -m "- 审批卡提供确认已执行与允许重试两种管理员动作，禁止直接重试不确定命令。" -m "- 完全访问确认绑定打开弹窗时的会话，切换会话自动取消旧确认。"
```

---

### Task 10: 根 Agent 自动 Spawn 工具接入

**Files:**
- Create: `backend/app/agent/spawn_tools.py`
- Create: `backend/tests/test_agent_spawn_tools.py`
- Modify: `backend/app/agent/chat_turn.py:20-181`
- Modify: `backend/app/agent/tool_dispatch.py:296-490`
- Modify: `backend/tests/test_agent_tool_dispatch.py`
- Modify: `backend/tests/test_chat_turn.py`
- Modify: `backend/tests/test_agent_spawn_integration.py`

**Interfaces:**
- Produces `SPAWN_TOOL_NAMES: frozenset[str]`。
- Produces `spawn_tool_schemas() -> list[dict[str, Any]]`。
- Produces `build_spawn_tool_dispatcher(manager: SpawnManager, session_id: int) -> ToolDispatcher`。

- 根 Agent schema 增加 `spawn_agent`、`wait_agent`、`list_agents`、`close_agent`。
- 子 Agent 的 `tool_schemas_for(role.tools_allowlist)` 保持不变，不包含 Spawn/HITL/设备变更。

- [ ] **Step 1: 写 Spawn schema 安全失败测试**

```python
def test_spawn_schema_exposes_only_server_controlled_arguments() -> None:
    schemas = {item["function"]["name"]: item for item in spawn_tool_schemas()}
    parameters = schemas["spawn_agent"]["function"]["parameters"]
    assert set(parameters["properties"]) == {"role", "task_brief"}
    assert set(parameters["properties"]["role"]["enum"]) == {
        "classifier", "kb_explorer", "ops_explorer", "investigator", "reviewer"
    }
    assert "model" not in parameters["properties"]
    assert "tools_allowlist" not in parameters["properties"]
    assert "budget" not in parameters["properties"]
```

- [ ] **Step 2: 写 dispatcher 生命周期失败测试**

```python
async def test_root_spawn_dispatcher_spawns_waits_lists_and_closes(fake_spawn_manager) -> None:
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=9)
    spawned = await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "检查资产 42 的当前监控状态"},
    )
    assert spawned.control == "ok"
    assert "child-1" in spawned.content

    waited = await dispatch("wait_agent", {"child_id": "child-1", "timeout_ms": 1000})
    assert "COMPLETED" in waited.content
    assert fake_spawn_manager.spawn_kwargs["fork_mode"] == "none"
```

增加未知角色、越界 timeout、其他 session child ID、wait 超时不取消 child、异常只返回固定安全错误分类的测试。

- [ ] **Step 3: 运行 Spawn 工具测试并确认失败**

Run:

```powershell
uv run pytest tests/test_agent_spawn_tools.py -q
```

Expected: FAIL，模块和工具 schema 不存在。

- [ ] **Step 4: 实现独立 Spawn 工具模块**

`spawn_tools.py` 可以导入 `spawn.py`，但 `tool_dispatch.py` 不得反向导入 `spawn.py`，避免现有 `spawn.py -> tool_dispatch.py` 形成循环。chat_turn 分别组合普通 root dispatcher 和 Spawn dispatcher。

```python
async def _require_session_child(
    manager: SpawnManager,
    session_id: int,
    child_id: str,
) -> None:
    receipts = await manager.list_agents(session_id)
    if child_id not in {item.child_id for item in receipts}:
        raise ChildNotFoundError(child_id)


async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
    if name == "spawn_agent":
        parsed = SpawnAgentArgs.model_validate(arguments)
        receipt = await manager.spawn_agent(
            session_id=session_id,
            role=parsed.role,
            task_brief=parsed.task_brief,
            fork_mode="none",
        )
        return ToolResult(control="ok", content=_safe_receipt_text(receipt))
    if name == "wait_agent":
        parsed = WaitAgentArgs.model_validate(arguments)
        await _require_session_child(manager, session_id, parsed.child_id)
        receipt = await manager.wait_agent(parsed.child_id, timeout_ms=parsed.timeout_ms)
        return ToolResult(control="ok", content=_safe_receipt_text(receipt))
    if name == "list_agents":
        ListAgentsArgs.model_validate(arguments)
        receipts = await manager.list_agents(session_id)
        return ToolResult(
            control="ok",
            content="\n".join(_safe_receipt_text(item) for item in receipts)
            or "当前会话没有子 Agent",
        )
    if name == "close_agent":
        parsed = CloseAgentArgs.model_validate(arguments)
        await _require_session_child(manager, session_id, parsed.child_id)
        receipt = await manager.close_agent(parsed.child_id)
        return ToolResult(control="ok", content=_safe_receipt_text(receipt))
    return ToolResult(control="rejected", content=f"未知 Spawn 工具：{name}")
```

`WaitAgentArgs.timeout_ms` 限制 0–30000；task brief 限制 1–4000 字符；所有 Pydantic 模型 `extra="forbid"`。

- [ ] **Step 5: 接入根聊天并更新提示词**

```python
spawn_dispatch = build_spawn_tool_dispatcher(spawn_manager, session_id=session_id)
tools = root_tool_schemas() + spawn_tool_schemas()

async def wrapped_dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
    if name in SPAWN_TOOL_NAMES:
        return await spawn_dispatch(name, arguments)
    return await base_dispatch(name, arguments)
```

ROOT_OPS_SYSTEM_PROMPT 明确：简单查询不 Spawn；两个以上独立调查才 Spawn；子 Agent 只读；根 Agent 必须 wait 并汇总；任何设备变更由根 Agent 经过 HITL 发起。

- [ ] **Step 6: 写根聊天端到端假 LLM 测试**

假 LLM 第一轮同时返回两个 `spawn_agent`，第二轮返回两个 `wait_agent`，第三轮给最终汇总。断言两个 child task brief 精确、最终回答包含两份安全摘要、根 transcript 有完整工具对、子 transcript 仍按 child ID 隔离。

Run:

```powershell
uv run pytest tests/test_agent_spawn_tools.py tests/test_agent_tool_dispatch.py tests/test_chat_turn.py tests/test_agent_spawn_integration.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交 Task 10**

```powershell
git add backend/app/agent/spawn_tools.py backend/app/agent/chat_turn.py backend/app/agent/tool_dispatch.py backend/tests/test_agent_spawn_tools.py backend/tests/test_agent_tool_dispatch.py backend/tests/test_chat_turn.py backend/tests/test_agent_spawn_integration.py
git commit -m "把自动 Spawn 接入根运维助手" -m "- 根 Agent 可创建、等待、列出和关闭服务端受控的只读子任务。" -m "- 模型不能指定模型、工具或预算，子 Agent 继续禁止 HITL、设备变更和再次 Spawn。"
```

---

### Task 11: Spawn 生命周期事件与只读子任务卡片

**Files:**
- Modify: `backend/app/agent/spawn.py:55-95,324-1315`
- Modify: `backend/app/agent/ws_hub.py:1-170`
- Modify: `backend/app/schemas/agent_ws.py:15-33`
- Modify: `backend/app/main.py:62-80`
- Modify: `backend/tests/test_agent_spawn.py`
- Modify: `backend/tests/test_agent_ws_hub.py`
- Modify: `frontend/src/types/agent.ts`
- Modify: `frontend/src/hooks/use-ops-chat.ts`
- Modify: `frontend/src/hooks/ops-chat-reducer.test.ts`
- Create: `frontend/src/components/ops-assistant/ChildAgentStatusCard.tsx`
- Create: `frontend/src/components/ops-assistant/ChildAgentStatusCard.test.tsx`
- Modify: `frontend/src/components/ops-assistant/ChatMessageList.tsx`

**Interfaces:**
- Produces `SpawnEventPublisher.publish_child_status(receipt: ChildReceipt) -> None` 异步协议。
- Produces `SpawnManager.set_event_publisher(publisher: SpawnEventPublisher) -> None`。

- WS 新事件：`child_status`。
- 前端 `OpsChatItem` 新 kind：`child`。

- [ ] **Step 1: 写 Spawn 状态发布失败测试**

```python
async def test_spawn_manager_publishes_durable_statuses(spawn_manager, fake_publisher) -> None:
    receipt = await spawn_manager.spawn_agent(
        session_id=1,
        role="ops_explorer",
        task_brief="检查资产 42",
    )
    completed = await spawn_manager.wait_agent(receipt.child_id, timeout_ms=1000)
    assert [item.status for item in fake_publisher.receipts] == [
        "SPAWNING", "RUNNING", "COMPLETED"
    ]
    assert fake_publisher.receipts[-1].child_id == receipt.child_id
```

事件必须在对应 registry 状态提交后发布；发布器失败只能记录日志，不能回滚 child 状态或让 child 失败。

- [ ] **Step 2: 写 WS 安全字段失败测试**

```python
async def test_ws_spawn_publisher_whitelists_child_receipt() -> None:
    await publisher.publish_child_status(receipt_with_secret_artifact)
    payload = websocket.sent[0]["payload"]
    assert set(payload) == {
        "child_id", "role", "task_brief", "status",
        "result_summary", "created_at", "status_changed_at",
    }
    assert "tools_allowlist" not in payload
    assert "budget" not in payload
    assert "artifacts" not in payload
```

- [ ] **Step 3: 运行后端事件测试并确认失败**

Run:

```powershell
uv run pytest tests/test_agent_spawn.py tests/test_agent_ws_hub.py -q
```

Expected: FAIL，发布协议和 child_status 事件不存在。

- [ ] **Step 4: 实现生命周期发布**

`SpawnManager` 在 SPAWNING、RUNNING、COMPLETED、FAILED、CANCELLED、CLOSED 持久化提交完成后调用同一个 `_publish_child_status(receipt)`。main lifespan 给全局 manager 注入 `WsSpawnEventPublisher(hub)`；测试 manager 默认使用 noop publisher。

```python
async def _publish_child_status(self, receipt: ChildReceipt) -> None:
    try:
        await self._event_publisher.publish_child_status(receipt)
    except Exception:
        logger.exception("发布子 Agent 状态失败", extra={"child_id": receipt.child_id})
```

- [ ] **Step 5: 写前端 reducer 和卡片失败测试**

```typescript
it("updates one child card by child_id", () => {
  let state = reduceOpsChat({ items: [] }, {
    type: "ws",
    message: {
      type: "child_status",
      payload: { child_id: "c1", role: "ops_explorer", status: "RUNNING" },
    },
  })
  state = reduceOpsChat(state, {
    type: "ws",
    message: {
      type: "child_status",
      payload: { child_id: "c1", role: "ops_explorer", status: "COMPLETED" },
    },
  })
  expect(state.items.filter((item) => item.kind === "child")).toHaveLength(1)
  expect(state.items[0]).toMatchObject({ status: "COMPLETED" })
})
```

ChildAgentStatusCard 测试 RUNNING spinner、COMPLETED 摘要、FAILED/CANCELLED 文案，断言没有创建、等待、关闭按钮。

- [ ] **Step 6: 实现前端只读卡片**

```typescript
export interface ChildAgentStatusCardProps {
  childId: string
  role: string
  taskBrief: string
  status: string
  resultSummary: string | null
}

export function ChildAgentStatusCard(props: ChildAgentStatusCardProps) {
  const running = ["REQUESTED", "SPAWNING", "RUNNING"].includes(
    props.status.toUpperCase(),
  )
  return (
    <div data-testid={`child-agent-${props.childId}`}>
      <Badge>{props.role}</Badge>
      <p>{props.taskBrief}</p>
      {running ? <Spinner /> : <p>{props.resultSummary ?? statusLabel(props.status)}</p>}
    </div>
  )
}
```

快照中的 children 与 WS child_status 使用同一个稳定 ID `child:<child_id>`。

- [ ] **Step 7: 运行后端与前端状态测试**

Run:

```powershell
cd backend
uv run pytest tests/test_agent_spawn.py tests/test_agent_ws_hub.py -q
cd ../frontend
npm test -- src/hooks/ops-chat-reducer.test.ts src/components/ops-assistant/ChildAgentStatusCard.test.tsx
npm run typecheck
```

Expected: 全部 PASS。

- [ ] **Step 8: 提交 Task 11**

```powershell
git add backend/app/agent/spawn.py backend/app/agent/ws_hub.py backend/app/schemas/agent_ws.py backend/app/main.py backend/tests/test_agent_spawn.py backend/tests/test_agent_ws_hub.py frontend/src/types/agent.ts frontend/src/hooks/use-ops-chat.ts frontend/src/hooks/ops-chat-reducer.test.ts frontend/src/components/ops-assistant/ChildAgentStatusCard.tsx frontend/src/components/ops-assistant/ChildAgentStatusCard.test.tsx frontend/src/components/ops-assistant/ChatMessageList.tsx
git commit -m "展示可恢复的子 Agent 生命周期" -m "- SpawnManager 在持久化状态后发布安全 child_status 事件，发布失败不影响子任务。" -m "- 运维助手用只读卡片展示子任务进度，并与会话快照按 child ID 去重恢复。"
```

---

### Task 12: 强制单 Worker 运行约束

**Files:**
- Modify: `backend/app/main.py:1-82`
- Modify: `backend/tests/test_main.py`
- Modify: `backend/tests/test_config.py`
- Modify: `backend/.env.example` only if it already documents worker variables

**Interfaces:**
- Produces `validate_single_worker_environment(environment: Mapping[str, str]) -> None`。

- 检查已知变量 `WEB_CONCURRENCY`、`UVICORN_WORKERS`；未设置等价于 1。

- [ ] **Step 1: 写 worker 配置失败测试**

```python
@pytest.mark.parametrize("key", ["WEB_CONCURRENCY", "UVICORN_WORKERS"])
def test_rejects_multiple_workers(key: str) -> None:
    with pytest.raises(RuntimeError, match="只支持 1 个 Uvicorn worker"):
        validate_single_worker_environment({key: "2"})


def test_allows_default_and_one_worker() -> None:
    validate_single_worker_environment({})
    validate_single_worker_environment({"WEB_CONCURRENCY": "1"})
```

增加 0、负数、非整数配置返回明确中文错误的测试。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
uv run pytest tests/test_main.py tests/test_config.py -q
```

Expected: FAIL，校验函数不存在。

- [ ] **Step 3: 实现并在 lifespan 最前调用**

```python
def validate_single_worker_environment(environment: Mapping[str, str]) -> None:
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = environment.get(name)
        if raw is None:
            continue
        try:
            workers = int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name} 必须是整数 1") from exc
        if workers != 1:
            raise RuntimeError(
                "当前 Agent Spawn 运行时只支持 1 个 Uvicorn worker；"
                f"检测到 {name}={raw}"
            )


validate_single_worker_environment(os.environ)
```

校验调用必须是现有 lifespan 的第一条语句；通过后才执行 HITL、turn lease 和 Spawn 启动恢复。

不尝试根据 PID 或端口猜测独立实例；文档在 Task 16 明确多个应用实例同样不受支持。

- [ ] **Step 4: 运行启动测试**

Run:

```powershell
uv run pytest tests/test_main.py tests/test_config.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交 Task 12**

```powershell
git add backend/app/main.py backend/tests/test_main.py backend/tests/test_config.py backend/.env.example
git commit -m "强制 Agent 运行时使用单 Worker" -m "- 启动时拒绝已知的多 worker 环境配置，避免错误关闭其他进程的活跃子任务。" -m "- 对非法 worker 值给出明确中文错误，不引入分布式运行时。"
```

如果 `backend/.env.example` 本来没有 worker 配置，本任务不修改或 stage 该文件。

---

### Task 13: WebSocket 每连接发送队列与背压

**Files:**
- Modify: `backend/app/agent/ws_hub.py:31-88`
- Modify: `backend/app/api/v1/agent_ws.py`
- Modify: `backend/tests/test_agent_ws_hub.py`
- Modify: `backend/tests/test_agent_ws_api.py`

**Interfaces:**
- Produces: 内部 `_Peer(queue, writer_task)`。
- `AgentWsHub.connect` 接受连接后创建 writer task。
- `broadcast` 只做 `put_nowait`，不得 await 单个 socket 的 `send_json`。
- 默认 queue size 128、单次发送 timeout 5 秒，构造器允许测试注入更小值。

- [ ] **Step 1: 写慢连接不阻塞失败测试**

```python
async def test_slow_peer_does_not_block_fast_peer() -> None:
    hub = AgentWsHub(queue_size=2, send_timeout_seconds=0.05)
    slow = BlockingWebSocket()
    fast = FakeWebSocket()
    await hub.connect(1, slow)
    await hub.connect(1, fast)

    await asyncio.wait_for(
        hub.broadcast(
            1,
            AgentWsServerMessage(
                type="assistant_delta",
                payload={"text": "x", "done": False},
            ),
        ),
        timeout=0.01,
    )
    await wait_until(lambda: len(fast.sent) == 1)
    assert fast.sent[0]["payload"]["text"] == "x"
```

- [ ] **Step 2: 写队列满和幂等清理测试**

连接 queue size=1，连续广播直到 slow peer queue 满；断言 slow peer 被移除、fast peer 继续收消息。连续调用两次 disconnect，writer task 已结束且不抛异常。

- [ ] **Step 3: 运行 WS 测试并确认失败**

Run:

```powershell
uv run pytest tests/test_agent_ws_hub.py tests/test_agent_ws_api.py -q
```

Expected: 慢连接测试超时或 broadcast 被阻塞。

- [ ] **Step 4: 实现 Peer 队列与 writer**

```python
@dataclass(slots=True)
class _Peer:
    queue: asyncio.Queue[AgentWsServerMessage]
    writer_task: asyncio.Task[None]


async def _writer(self, session_id: int, websocket: WebSocket, queue) -> None:
    try:
        while True:
            message = await queue.get()
            await asyncio.wait_for(
                websocket.send_json(message.model_dump(mode="json")),
                timeout=self._send_timeout_seconds,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        await self.disconnect(session_id, websocket, from_writer=True)
```

`disconnect(session_id, websocket, *, from_writer=False)` 先从 map 移除 peer，再取消 writer；writer 自己触发清理时不能 await/cancel 自己。broadcast 对每个 peer 使用 `put_nowait`；`QueueFull` 时安排该 peer 的 disconnect，不影响其他 peer。

- [ ] **Step 5: 更新 WS 路由清理并运行测试**

API 路由的 `finally` 继续调用 hub.disconnect；hub 内部保证可重复清理。运行：

```powershell
uv run pytest tests/test_agent_ws_hub.py tests/test_agent_ws_api.py tests/test_chat_turn.py -q
```

Expected: PASS；chat token delta 不因一个慢 peer 阻塞。

- [ ] **Step 6: 提交 Task 13**

```powershell
git add backend/app/agent/ws_hub.py backend/app/api/v1/agent_ws.py backend/tests/test_agent_ws_hub.py backend/tests/test_agent_ws_api.py backend/tests/test_chat_turn.py
git commit -m "为 Agent WebSocket 增加连接级背压" -m "- 每个连接使用独立有界队列和发送任务，广播不再串行等待网络。" -m "- 队列满或发送超时只清理慢连接，正常会话和模型流式输出继续运行。"
```

---

### Task 14: 清零 Ruff、mypy 与 ESLint 基线

**Files:**
- Modify: `backend/app/agent/hitl_gate.py`
- Modify: `backend/app/agent/tool_dispatch.py`
- Modify: `backend/app/api/v1/agent_sessions.py`
- Modify: `backend/app/api/v1/system_config.py`
- Modify: `backend/tests/test_agent_hitl.py`
- Modify: `backend/tests/test_agent_hitl_tools.py`
- Modify: `backend/tests/test_agent_sessions_api.py`
- Modify: `backend/tests/test_device_command_execution_integration.py`
- Create: `frontend/src/lib/cmdb-credential-api.ts`
- Create: `frontend/src/components/cmdb/cmdbAssetPickerUtils.ts`
- Modify: `frontend/src/components/cmdb/CmdbAssetFormDialog.tsx`
- Modify: `frontend/src/components/cmdb/CmdbAssetPicker.tsx`
- Modify: `frontend/src/components/common/DataTable.tsx`
- Modify: `frontend/src/components/device-command-policies/DeviceCommandPolicyFormDialog.tsx`
- Modify: `frontend/src/components/system-config/LlmConfigCard.tsx`
- Modify: `frontend/src/pages/CmdbAssetsTrashPage.tsx`
- Modify: `frontend/src/pages/DeviceCommandPoliciesTrashPage.tsx`
- Modify: `frontend/src/pages/PermissionsTrashPage.tsx`
- Modify: `frontend/src/pages/RolesTrashPage.tsx`
- Modify: `frontend/src/pages/UsersTrashPage.tsx`
- Modify tests importing moved frontend helpers

**Interfaces:**
- 不新增业务接口；只解决当前静态检查问题和前面任务产生的新增检查问题。

- [ ] **Step 1: 保存当前检查输出作为验收清单**

Run:

```powershell
cd backend
uv run ruff check app tests
uv run mypy app
cd ../frontend
npm run lint
```

Expected before fixes: Ruff 当前 18 项、mypy 当前 16 项、ESLint 当前 2 errors/9 warnings；前面任务可能改变数量，但本任务必须逐条归零。

- [ ] **Step 2: 修复 Ruff 与 mypy**

执行以下确定性修改：

- 删除 `hitl_gate.py` 未使用的 `Awaitable`、`Callable`。
- 用 Ruff 排序 `system_config.py` 和相关测试导入。
- 删除两个 HITL 测试的未使用 import。
- 删除设备命令集成测试 11 处行尾空格。
- `HitlGateHook` 的 payload model map 标注为 `dict[str, type[BaseModel]]`，并把 action type 收窄成 `ActionType`。
- `build_root_tool_dispatcher` 不再把异构 `common_kwargs` 用 `**dict` 展开；对 notify、device_control、query_device_command 显式传 `session_id`、`actor_user_id`、`publisher`、`gate_hook`。
- `patch_session_approval_mode` 对 CRUD update 的 `None` 结果显式返回 404 或 raise，不把 Optional 赋给非 Optional。

```python
updated = await agent_session_crud.update(
    db,
    session_id,
    {"approval_mode": body.approval_mode},
)
if updated is None:
    raise HTTPException(status_code=404, detail="Agent 会话不存在")
session = updated
```

- [ ] **Step 3: 修复 Fast Refresh 错误**

把 `fetchCmdbAssetCredential` 移到 `frontend/src/lib/cmdb-credential-api.ts`；把 `CmdbAssetOption` 和 `formatCmdbAssetOption` 移到 `cmdbAssetPickerUtils.ts`。组件文件只导出 React 组件，测试改从新文件导入纯函数。

- [ ] **Step 4: 修复 React hook 警告**

三个 React Hook Form 组件用 `useWatch` 替换 `form.watch`：

```typescript
const credentialType = useWatch({
  control: form.control,
  name: "credential_type",
})
```

`DataTable` 的 `useReactTable` 是 TanStack Table 明确不兼容编译器记忆化的 API，在该调用上方加入单行、带原因的 `eslint-disable-next-line react-hooks/incompatible-library`，不关闭全局规则。

五个 Trash Page 用 `useCallback` 包裹 `handleRestore`，再把它加入 columns `useMemo` 依赖：

```typescript
const handleRestore = useCallback(async (id: number) => {
  await restoreItem(id)
  await refetch()
}, [refetch])
```

每个页面按它自己的 restore API 和 refetch 依赖书写，不共享新抽象。

- [ ] **Step 5: 运行静态检查和受影响测试**

Run:

```powershell
cd backend
uv run ruff check app tests
uv run mypy app
uv run pytest tests/test_agent_hitl.py tests/test_agent_hitl_tools.py tests/test_agent_sessions_api.py tests/test_device_command_execution_integration.py -q
cd ../frontend
npm run lint
npm run typecheck
npm test
```

Expected: Ruff、mypy、ESLint 零错误零警告；受影响测试 PASS。

- [ ] **Step 6: 提交 Task 14**

```powershell
git add backend/app/agent/hitl_gate.py backend/app/agent/tool_dispatch.py backend/app/api/v1/agent_sessions.py backend/app/api/v1/system_config.py backend/tests/test_agent_hitl.py backend/tests/test_agent_hitl_tools.py backend/tests/test_agent_sessions_api.py backend/tests/test_device_command_execution_integration.py frontend/src/lib/cmdb-credential-api.ts frontend/src/components/cmdb/cmdbAssetPickerUtils.ts frontend/src/components/cmdb/CmdbAssetFormDialog.tsx frontend/src/components/cmdb/CmdbAssetPicker.tsx frontend/src/components/common/DataTable.tsx frontend/src/components/device-command-policies/DeviceCommandPolicyFormDialog.tsx frontend/src/components/system-config/LlmConfigCard.tsx frontend/src/pages/CmdbAssetsTrashPage.tsx frontend/src/pages/DeviceCommandPoliciesTrashPage.tsx frontend/src/pages/PermissionsTrashPage.tsx frontend/src/pages/RolesTrashPage.tsx frontend/src/pages/UsersTrashPage.tsx
git commit -m "清零前后端静态检查问题" -m "- 修复 Agent 类型收窄、显式参数传递、导入和测试格式问题，使 Ruff 与 mypy 全绿。" -m "- 拆出非组件导出并修正 React hook 依赖，使 ESLint 零错误零警告。"
```

提交前用 `git diff --cached --name-only` 确认 stage 中没有设计范围外文件。

---

### Task 15: 前端路由懒加载与主包分拆

**Files:**
- Modify: `frontend/src/App.tsx:1-186`
- Modify or Create: `frontend/src/App.test.tsx`

**Interfaces:**
- 所有页面仍使用现有 named export；`App.tsx` 通过 `React.lazy` 转成 default-like promise。
- 路由、权限和 URL 不变。

- [ ] **Step 1: 写懒加载路由烟雾测试**

```typescript
import { readFileSync } from "node:fs"

it("loads business pages through dynamic imports", () => {
  const source = readFileSync(new URL("./App.tsx", import.meta.url), "utf8")
  expect(source).toContain("lazy(() =>")
  expect(source).toContain('import("@/pages/OpsAssistantPage")')
  expect(source).not.toMatch(
    /^import\s+\{\s*OpsAssistantPage\s*\}\s+from\s+"@\/pages\/OpsAssistantPage"/m,
  )
})


it("renders the ops assistant route through suspense", async () => {
  render(
    <MemoryRouter initialEntries={[ROUTES.OPS_ASSISTANT]}>
      <App />
    </MemoryRouter>,
  )
  expect(await screen.findByText("运维助手")).toBeInTheDocument()
})
```

现有 auth、ProtectedRoute 和页面 API 在测试中沿用项目 mock；再覆盖一个权限页和 NotFound，确保路由语义没变。

- [ ] **Step 2: 运行路由测试和基线构建**

Run:

```powershell
npm test -- src/App.test.tsx
npm run build
```

Expected before fix: dynamic-import contract FAIL；现有路由 smoke test PASS；build 仍报告约 1.10 MB 主包和 500 KB 警告。

- [ ] **Step 3: 将页面导入改为 lazy**

```typescript
import { lazy, Suspense, useEffect } from "react"

const OpsAssistantPage = lazy(() =>
  import("@/pages/OpsAssistantPage").then((module) => ({
    default: module.OpsAssistantPage,
  })),
)
const CmdbAssetsPage = lazy(() =>
  import("@/pages/CmdbAssetsPage").then((module) => ({
    default: module.CmdbAssetsPage,
  })),
)
```

按同一明确形式转换 Dashboard、运维助手、CMDB/回收站、监控/日志、设备策略/回收站、用户/角色/权限及回收站、审计、系统配置和 Profile。Login、Forbidden、NotFound 也可以 lazy，但 AppLayout、ProtectedRoute、ErrorBoundary 和 Spinner 保持主包同步加载。

- [ ] **Step 4: 添加统一 Suspense 边界**

```typescript
<ErrorBoundary>
  <Suspense
    fallback={
      <div className="flex h-screen items-center justify-center">
        <Spinner className="size-6 text-muted-foreground" />
      </div>
    }
  >
    <Routes>{/* 现有 route tree 原样保留 */}</Routes>
  </Suspense>
</ErrorBoundary>
```

- [ ] **Step 5: 验证路由、类型和构建产物**

Run:

```powershell
npm test -- src/App.test.tsx
npm run typecheck
npm run build
```

Expected: PASS；Vite 输出多个页面 chunk，入口主包不再出现超过 500 KB 的警告。若警告来自单个页面 chunk，使用 Vite 输出定位具体依赖，只对该依赖增加 `build.rollupOptions.output.manualChunks`，不得使用提高 `chunkSizeWarningLimit` 隐藏问题。

- [ ] **Step 6: 提交 Task 15**

```powershell
git add frontend/src/App.tsx frontend/src/App.test.tsx frontend/vite.config.ts
git commit -m "按路由拆分前端生产包" -m "- 使用 React lazy 和 Suspense 延迟加载各业务页面，保持现有权限与路由不变。" -m "- 降低入口主包体积并消除 Vite 500 KB 构建警告。"
```

如果不需要 `frontend/vite.config.ts` 的 manualChunks，就不要修改或 stage 它。

---

### Task 16: 文档同步、完整回归与最终验收

**Files:**
- Modify: `docs/AGENT_ARCHITECTURE.md`
- Modify: `docs/DEPLOYMENT.md`
- Modify: `docs/guide.md`
- Modify: `docs/sequence-diagram.mermaid`
- Modify: `docs/class-diagram.mermaid` only if it currently描述 Agent 状态关系
- Modify: relevant backend/frontend test files only if full regression exposes a real defect

**Interfaces:**
- 文档状态名、API 路径和代码保持完全一致。
- 不新增功能；只同步已实现行为并完成全量验证。

- [ ] **Step 1: 更新 HITL 与会话架构文档**

`docs/AGENT_ARCHITECTURE.md` 必须写明：

```text
PENDING -> APPROVED -> EXECUTING -> EXECUTED
                                \-> UNKNOWN
UNKNOWN -> EXECUTED（人工确认）
UNKNOWN -> APPROVED（检查后允许重试）
```

解释策略在每次认领执行前复检、EXECUTING 先提交、UNKNOWN 不自动重试、turn token 串行化、assistant/tool 完整消息单元和工具感知压缩边界。

- [ ] **Step 2: 更新 Spawn、快照和 WebSocket 文档**

`docs/AGENT_ARCHITECTURE.md` 与时序图增加：

- 根 Agent 自动 `spawn_agent -> wait_agent -> 汇总`。
- 子 Agent 只有角色目录中的只读工具。
- 快照是刷新/重连恢复来源，WS 是实时加速。
- 每连接 queue/writer 隔离慢客户端。

`docs/guide.md` 增加管理员处置 UNKNOWN 的逐步说明，并明确“确认已执行”前必须检查真实设备状态。

- [ ] **Step 3: 更新部署约束**

`docs/DEPLOYMENT.md` 的所有 Uvicorn/Gunicorn 示例固定一个 worker，加入：

```text
当前进程内 SpawnManager 只支持单 Uvicorn worker 和单应用实例。
配置 WEB_CONCURRENCY>1 或 UVICORN_WORKERS>1 时应用拒绝启动。
多实例部署需要未来引入分布式任务所有权，本版本不支持。
```

- [ ] **Step 4: 运行阶段级后端回归**

Run:

```powershell
cd backend
uv run pytest tests/test_agent_crud_hitl.py tests/test_agent_hitl_execution.py tests/test_hitl_api.py tests/test_agent_recovery.py -q
uv run pytest tests/test_agent_loop.py tests/test_chat_turn.py tests/test_agent_sessions_api.py tests/test_agent_compaction.py tests/test_agent_session.py -q
uv run pytest tests/test_agent_spawn.py tests/test_agent_spawn_tools.py tests/test_agent_spawn_integration.py tests/test_agent_ws_hub.py tests/test_agent_ws_api.py -q
```

Expected: 三组全部 PASS。

- [ ] **Step 5: 运行完整后端验收**

Run:

```powershell
uv run pytest -q
uv run ruff check app tests
uv run mypy app
uv run alembic heads
```

Expected: 当前 789 项加本计划新增测试全部 PASS；Ruff/mypy 零错误；Alembic 只显示 `f2b4c6d8e013 (head)`。如果完整 pytest 超过工具单次时限，使用持续等待机制读取同一进程结果，不把超时误报为通过。

- [ ] **Step 6: 运行完整前端验收**

Run:

```powershell
cd ../frontend
npm test
npm run typecheck
npm run lint
npm run build
```

Expected: 全部 PASS；ESLint 零 warning；构建没有 500 KB chunk 警告。

- [ ] **Step 7: 检查迁移与敏感信息**

不得对开发者现有数据库执行 upgrade 或 downgrade。用迁移契约测试和 Alembic 离线 SQL 验证：

```powershell
cd ../backend
uv run pytest tests/test_runtime_reliability_migration.py -q
uv run alembic upgrade c1a8e4b7d902:f2b4c6d8e013 --sql
uv run alembic -x allow-destructive=true downgrade f2b4c6d8e013:c1a8e4b7d902 --sql
```

Expected: 契约测试 PASS；upgrade/downgrade SQL 都能生成且不连接或修改数据库；真实 downgrade 的 destructive guard 已由契约测试验证。

随后运行：

```powershell
cd ..
rg -n -i "password|secret|credential" backend/app/agent backend/app/schemas/agent_ws.py frontend/src/types/agent.ts
```

逐项确认命中都是字段过滤、动态凭据输入或安全说明，快照/WS/Spawn payload 没有明文凭据字段。

- [ ] **Step 8: 提交文档和回归中必要的小修复**

```powershell
git add docs/AGENT_ARCHITECTURE.md docs/DEPLOYMENT.md docs/guide.md docs/sequence-diagram.mermaid docs/class-diagram.mermaid
git commit -m "同步 Agent 可靠性架构与运维说明" -m "- 记录 HITL UNKNOWN、会话租约、快照恢复、自动 Spawn 和 WebSocket 背压的数据流。" -m "- 明确单 worker 部署限制和管理员检查不确定设备结果的操作步骤。"
```

若 `docs/class-diagram.mermaid` 无 Agent 状态内容且无需修改，不要 stage。全量回归若发现业务缺陷，先为缺陷增加失败测试，再做最小修复，并在这个 commit 中列出实际修复文件。

- [ ] **Step 9: 最终 Git 检查**

Run:

```powershell
git branch --show-current
git status --short
git log --oneline -16
```

Expected: 当前分支是 `master`；工作区为空；Task 1–16 的提交按顺序存在；没有执行 push。
