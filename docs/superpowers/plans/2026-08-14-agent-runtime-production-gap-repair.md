# Agent 运行时生产链路缺口修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接通 `child_status`、`monitor_alert`、五个 Spawn 原语和两个确定性编排工作流的生产入口，使默认工作流真实产生二级 reviewer，并让预算、GC、HITL 的代码、测试与文档语义一致。

**Architecture:** 前端只修复 WebSocket 运行时解析边界；监控扫描在数据库提交后通过可注入发布器向带 `monitor:read` 能力的 Hub peer 广播；根 Agent dispatcher 同时承载五个底层原语和两个服务端编排工具。编排器保留最后一波回执到结果解析完成，在并发允许时把 reviewer 挂到一个未关闭 worker 下，否则安全回退为根级 reviewer。

**Tech Stack:** Python 3.14.3、FastAPI、SQLAlchemy async、Pydantic v2、asyncio、pytest/pytest-asyncio、React 19、TypeScript 6、Vitest 4。

## Global Constraints

- 直接在 `master` 工作，不创建或切换分支，不 push。
- 所有 Python 命令从 `backend/` 使用 `uv run`；不得直接运行系统 `python` 或使用 `pip`。
- 不新增依赖、不修改数据库结构、不生成 Alembic 迁移。
- 不调用真实大模型、真实设备或其它有成本的外部服务。
- 监控告警不得包含凭据、探测异常原文或内部配置。
- WebSocket 是通知加速层；数据库与快照继续作为权威状态来源。
- 两个编排工作流保持只读建议，不写知识库分类、CMDB、监控或设备状态。
- 每项生产代码修改前必须先写能因当前缺口正确失败的测试，并实际观察 RED。
- 每个提交只包含当前任务列出的文件；提交信息使用中文详细正文，不含 `Co-Authored-By`。

---

## 文件职责与任务依赖

| 文件 | 本计划中的职责 |
| :--- | :--- |
| `frontend/src/lib/agent-ws.ts` | 接受完整的服务端判别式 WS 类型集合 |
| `backend/app/agent/ws_hub.py` | 保存 peer 的监控读取能力并执行权限过滤广播 |
| `backend/app/api/v1/agent_ws.py` | 建连及周期复检时计算/刷新 `monitor:read` 能力 |
| `backend/app/services/monitor_sweep.py` | 判定状态翻转、提交事实、提交后发布告警 |
| `backend/app/agent/spawn.py` | 五类 child 错误标签及默认 runner 映射 |
| `backend/app/agent/orchestration.py` | 最后一波回执所有权、嵌套 reviewer 与回退清理 |
| `backend/app/agent/spawn_tools.py` | 五个 Spawn 原语、两个工作流工具的 schema 与 dispatcher |
| `backend/app/agent/chat_turn.py` | 向根模型描述何时优先使用确定性工作流 |
| 架构、guide 与现行 spec | 同步运行时真实语义；历史 plan 不回写 |

依赖顺序：Task 2 的 Hub 能力是 Task 3 告警发布的前置；Task 5 的 reviewer 生命周期是 Task 6 工作流工具入口的前置。其它任务可以独立验证，但仍按编号执行，便于定位回归。

---

### Task 1: 修复前端 `child_status` 解析丢帧

**Files:**
- Modify: `frontend/src/lib/agent-ws.ts:6-17`
- Test: `frontend/src/lib/agent-ws.test.ts:31-55`

**Interfaces:**
- Consumes: `AgentWsEventType` 已包含 `"child_status"`。
- Produces: `parseAgentWsMessage(raw)` 对合法 `child_status` 返回 `AgentWsServerMessage`，使现有 hook/reducer 获得实时帧。

- [ ] **Step 1: 写解析层失败测试**

在 `describe("parseAgentWsMessage")` 中加入真实文本帧测试；该测试捕获的变异是“从运行时白名单删除 `child_status`”：

```ts
it("解析 child_status 实时状态帧", () => {
  const msg = parseAgentWsMessage(
    JSON.stringify({
      type: "child_status",
      payload: { child_id: "child-1", role: "reviewer", status: "RUNNING" },
    }),
  )

  expect(msg).toEqual({
    type: "child_status",
    payload: { child_id: "child-1", role: "reviewer", status: "RUNNING" },
  })
})
```

- [ ] **Step 2: 运行测试并确认 RED**

Run from `frontend/`: `npm test -- src/lib/agent-ws.test.ts`

Expected: 新用例失败，实际值为 `null`；其它解析用例通过。

- [ ] **Step 3: 做最小生产修复**

在 `AGENT_WS_EVENT_TYPES` 中加入唯一缺失项：

```ts
  "hitl_execution_failed",
  "child_status",
  "monitor_alert",
```

不修改 reducer、hook 或类型声明。

- [ ] **Step 4: 验证 GREEN 与前端相关回归**

Run from `frontend/`:

```powershell
npm test -- src/lib/agent-ws.test.ts src/hooks/ops-chat-reducer.test.ts src/hooks/use-ops-chat.test.tsx
npm run typecheck
```

Expected: 所有指定测试通过，TypeScript 无报错。

- [ ] **Step 5: 提交 Task 1**

```powershell
git add frontend/src/lib/agent-ws.ts frontend/src/lib/agent-ws.test.ts
git commit -m "修复子 Agent WebSocket 实时状态解析" -m "- 将 child_status 纳入前端运行时事件白名单，避免合法帧在 reducer 前被丢弃`n- 增加解析层回归测试，覆盖后端真实 child_status 信封"
```

---

### Task 2: 为 Agent WebSocket peer 增加监控权限能力

**Files:**
- Modify: `backend/app/agent/ws_hub.py:49-193,244-278`
- Modify: `backend/app/api/v1/agent_ws.py:46-265`
- Test: `backend/tests/test_agent_ws_hub.py`
- Test: `backend/tests/test_agent_ws_api.py`

**Interfaces:**
- Consumes: 现有 `get_authorized_session_user(..., permission_code=...)` 与每 peer 有界发送队列。
- Produces: `AgentWsHub.connect(session_id, websocket, *, can_read_monitor=False)`、`AgentWsHub.update_monitor_access(...)`、`AgentWsHub.broadcast_monitor_alert(message)`、`WsMonitorAlertPublisher.publish_monitor_alert(payload)`。

- [ ] **Step 1: 写 Hub 权限过滤失败测试**

在 `test_agent_ws_hub.py` 中导入 `WsMonitorAlertPublisher`，新增测试。它捕获的变异是“监控广播复用普通 broadcast，导致无权限连接收到资产信息”：

```python
async def test_monitor_alert_only_reaches_monitor_read_peers() -> None:
    local_hub = AgentWsHub()
    allowed = FakeWebSocket()
    denied = FakeWebSocket()
    await local_hub.connect(1, allowed, can_read_monitor=True)  # type: ignore[arg-type]
    await local_hub.connect(2, denied, can_read_monitor=False)  # type: ignore[arg-type]

    publisher = WsMonitorAlertPublisher(local_hub)
    await publisher.publish_monitor_alert(
        {"target_id": 7, "status": "down", "message": "核心交换机离线"}
    )

    await wait_until(lambda: len(allowed.sent) == 1)
    assert allowed.sent == [{
        "type": "monitor_alert",
        "payload": {"target_id": 7, "status": "down", "message": "核心交换机离线"},
    }]
    assert denied.sent == []
```

再加一个 `update_monitor_access` 测试：peer 初始无权、更新为有权后能收到下一条监控告警；普通 `broadcast(session_id, ...)` 仍只按会话隔离，不受监控能力影响。

- [ ] **Step 2: 运行 Hub 测试并确认 RED**

Run from `backend/`:

```powershell
uv run pytest tests/test_agent_ws_hub.py::test_monitor_alert_only_reaches_monitor_read_peers -v
```

Expected: 因 `connect` 尚不接受 `can_read_monitor` 或发布器不存在而失败。

- [ ] **Step 3: 实现 Hub peer 能力与发布器**

对 `_Peer` 和 Hub 增加最小字段/方法：

```python
@dataclass(slots=True)
class _Peer:
    queue: asyncio.Queue[AgentWsServerMessage]
    writer_task: asyncio.Task[None]
    can_read_monitor: bool = False

async def connect(
    self,
    session_id: int,
    websocket: WebSocket,
    *,
    can_read_monitor: bool = False,
) -> None:
    ...

def update_monitor_access(
    self,
    session_id: int,
    websocket: WebSocket,
    *,
    can_read_monitor: bool,
) -> None:
    peer = self._connections.get(session_id, {}).get(websocket)
    if peer is not None:
        peer.can_read_monitor = can_read_monitor
```

抽取一个 `_enqueue(session_id, websocket, peer, message)` 私有方法，让普通会话广播和全局监控广播复用连接状态、队列满与断连处理。监控入口只遍历 `peer.can_read_monitor` 为真的 peer：

```python
async def broadcast_monitor_alert(self, message: AgentWsServerMessage) -> None:
    for session_id, peers in list(self._connections.items()):
        for websocket, peer in list(peers.items()):
            if peer.can_read_monitor:
                self._enqueue(session_id, websocket, peer, message)
```

新增发布器并保持 payload 原样为已由 monitor service 生成的安全字典：

```python
class WsMonitorAlertPublisher:
    def __init__(self, bound_hub: AgentWsHub | None = None) -> None:
        self._bound_hub = bound_hub

    async def publish_monitor_alert(self, payload: Mapping[str, object]) -> None:
        message = AgentWsServerMessage(type="monitor_alert", payload=dict(payload))
        await (self._bound_hub or hub).broadcast_monitor_alert(message)
```

- [ ] **Step 4: 写 WebSocket 路由能力失败测试**

在 `test_agent_ws_api.py` 增加两个边界测试：

1. 只有 `agent:use` 的现有默认用户建连后，`hub._connections[session.id][peer].can_read_monitor is False`；
2. 给 `test_role` 新增 `monitor:read` 后建连，通过 `hub.broadcast_monitor_alert(...)` 收到真实 `monitor_alert`。

另对 `_periodic_reauth` 增加测试：连接期间删除 `monitor:read` 的 `role_permissions` 行，下一轮复检不关闭聊天连接，但把 peer 能力刷新为 `False`。

权限准备使用真实模型和关联表：

```python
permission = Permission(name="查看监控", code="monitor:read", module="Monitor")
db_session.add(permission)
await db_session.flush()
await db_session.execute(
    role_permissions.insert().values(role_id=test_role.id, permission_id=permission.id)
)
await db_session.commit()
```

- [ ] **Step 5: 运行 API 测试并确认 RED**

Run from `backend/`: `uv run pytest tests/test_agent_ws_api.py -v`

Expected: 新增能力断言失败；原有 JWT、所有者与 `agent:use` 用例仍通过。

- [ ] **Step 6: 在建连和周期复检中计算能力**

在 `agent_ws.py` 增加 `_MONITOR_READ_PERMISSION = "monitor:read"`，用现有授权查询计算：

```python
async def _can_read_monitor(
    db: AsyncSession,
    *,
    user_id: int,
    family_id: str,
    token_version: int,
) -> bool:
    authorized = await get_authorized_session_user(
        db,
        user_id=user_id,
        family_id=family_id,
        token_version=token_version,
        permission_code=_MONITOR_READ_PERMISSION,
    )
    return bool(
        authorized is not None
        and (authorized.user.is_superuser or authorized.has_permission)
    )
```

初次鉴权后把结果传给：

```python
await hub.connect(
    session_id,
    websocket,
    can_read_monitor=can_read_monitor,
)
```

周期复检确认 `agent:use` 和会话仍有效后调用 `hub.update_monitor_access(...)`。撤销 `monitor:read` 只关闭告警能力，不关闭聊天；撤销 `agent:use` 仍按原行为关闭 4403。

- [ ] **Step 7: 验证 Task 2 GREEN**

Run from `backend/`:

```powershell
uv run pytest tests/test_agent_ws_hub.py tests/test_agent_ws_api.py -v
uv run mypy app/agent/ws_hub.py app/api/v1/agent_ws.py
uv run ruff check app/agent/ws_hub.py app/api/v1/agent_ws.py tests/test_agent_ws_hub.py tests/test_agent_ws_api.py
```

Expected: 测试、mypy、ruff 全部通过。

- [ ] **Step 8: 提交 Task 2**

```powershell
git add backend/app/agent/ws_hub.py backend/app/api/v1/agent_ws.py backend/tests/test_agent_ws_hub.py backend/tests/test_agent_ws_api.py
git commit -m "增加监控告警 WebSocket 权限边界" -m "- 为每个 Agent WebSocket peer 持久化 monitor:read 能力并支持周期刷新`n- 新增权限过滤的 monitor_alert 全局广播与安全发布器`n- 保留原有 agent:use、会话归属和慢连接隔离语义"
```

---

### Task 3: 在探活状态翻转后发布 `monitor_alert`

**Files:**
- Modify: `backend/app/services/monitor_sweep.py`
- Modify: `backend/app/models/monitor_status_event.py:1-7`（只修正文档字符串中的 append-only 误述）
- Test: `backend/tests/test_monitor_sweep.py`

**Interfaces:**
- Consumes: Task 2 的 `WsMonitorAlertPublisher.publish_monitor_alert(payload)`。
- Produces: `MonitorAlertPublisher` Protocol、`run_monitor_sweep_once(..., alert_publisher=None)` 的提交后发布行为；生产 `run_monitor_sweep_loop` 注入真实发布器。

- [ ] **Step 1: 写状态翻转发布失败测试**

在 `test_monitor_sweep.py` 增加记录调用的 fake：

```python
class RecordingAlertPublisher:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.payloads: list[dict[str, object]] = []
        self.transaction_states: list[bool] = []

    async def publish_monitor_alert(self, payload: Mapping[str, object]) -> None:
        self.transaction_states.append(self.db.in_transaction())
        self.payloads.append(dict(payload))
```

用连续 `up -> up -> down` 三次探测断言：首次无告警、同状态无告警、翻转只有一条；payload 使用手工常量验证 `previous_status="up"`、`status="down"`、`severity="critical"`、目标字段齐全，且发布时事务已经提交。

- [ ] **Step 2: 运行测试并确认 RED**

Run from `backend/`:

```powershell
uv run pytest tests/test_monitor_sweep.py::test_status_flip_publishes_monitor_alert_after_commit -v
```

Expected: `run_monitor_sweep_once` 不接受 `alert_publisher` 或 fake 没有收到 payload。

- [ ] **Step 3: 实现翻转收集与提交后发布**

在 service 中定义结构协议，不把 WebSocket 具体类写进测试接口：

```python
class MonitorAlertPublisher(Protocol):
    async def publish_monitor_alert(self, payload: Mapping[str, object]) -> None: ...
```

单轮流程改为：

```python
targets = await monitor_target_crud.list_active(db)
previous = await monitor_status_event_crud.get_latest_status_for_targets(
    db, [target.id for target in targets]
)
pending_alerts: list[dict[str, object]] = []

for target in targets:
    previous_status = previous[target.id].status if target.id in previous else None
    try:
        status, latency_ms, detail = await probe_tcp(
            target.ip_address,
            target.port,
            timeout_seconds=probe_timeout_seconds,
        )
    except Exception as exc:
        status, latency_ms, detail = "down", None, str(exc)
    event = await monitor_status_event_crud.record_probe(
        db,
        target_id=target.id,
        status=status,
        latency_ms=latency_ms,
        detail=detail,
    )
    if previous_status is not None and previous_status != status:
        pending_alerts.append(_monitor_alert_payload(target, previous_status, event))

await db.commit()
if alert_publisher is not None:
    for payload in pending_alerts:
        try:
            await alert_publisher.publish_monitor_alert(payload)
        except Exception:
            logger.exception("monitor_alert 发布失败", extra={"target_id": payload["target_id"]})
```

`_monitor_alert_payload` 对 down 使用 `title="设备离线告警"`、`severity="critical"`，对 up 使用 `title="设备恢复通知"`、`severity="info"`；`message` 只拼 label/IP/port 和状态变化，不包含 `detail`。

`run_monitor_sweep_loop` 每轮传入模块级 `WsMonitorAlertPublisher()`，确保生产入口真实接通；单测仍可显式注入 fake。

- [ ] **Step 4: 写发布失败不破坏事实的失败测试**

增加抛异常 publisher，先写入已有 `up`，本轮探测 `down`，断言 `run_monitor_sweep_once` 正常返回且重新查询能看到 `down`。该测试捕获“提交后 WS 失败导致整个 sweep 被误判回滚”的回归。

```python
class FailingAlertPublisher:
    async def publish_monitor_alert(self, payload: Mapping[str, object]) -> None:
        raise RuntimeError("fake websocket failure")
```

- [ ] **Step 5: 验证 Task 3 GREEN**

Run from `backend/`:

```powershell
uv run pytest tests/test_monitor_sweep.py tests/test_agent_ws_hub.py -v
uv run mypy app/services/monitor_sweep.py
uv run ruff check app/services/monitor_sweep.py app/models/monitor_status_event.py tests/test_monitor_sweep.py
```

Expected: 首次/同状态/翻转/发布失败四条语义均通过，无静态检查错误。

- [ ] **Step 6: 提交 Task 3**

```powershell
git add backend/app/services/monitor_sweep.py backend/app/models/monitor_status_event.py backend/tests/test_monitor_sweep.py
git commit -m "接通探活状态翻转告警广播" -m "- 在巡检前读取上一状态，仅对真实 up/down 翻转生成 monitor_alert`n- 先提交监控事实再发布 WebSocket，发布失败不回滚数据库状态`n- 生成前端横幅所需安全字段并修正监控事件持久化说明"
```

---

### Task 4: 暴露第五个 Spawn 原语 `send_input`

**Files:**
- Modify: `backend/app/agent/spawn_tools.py`
- Test: `backend/tests/test_agent_spawn_tools.py`

**Interfaces:**
- Consumes: 已有 `SpawnManager.send_input(child_id, message) -> ChildReceipt` 与 `_require_session_child`。
- Produces: `SendInputArgs`、`send_input` JSON schema 和 dispatcher 分支；五原语集合供 Task 6 合并工具全集。

- [ ] **Step 1: 扩展测试 fake 并写 schema/dispatcher 失败测试**

给 `FakeSpawnManager` 加真实语义的最小方法：只在 fake receipt 为 `RUNNING` 时记录消息并返回 receipt，否则抛 `SpawnRejectedError("child_not_running")`。

新增测试：

```python
def test_send_input_schema_exposes_child_and_message_only() -> None:
    schemas = {item["function"]["name"]: item for item in spawn_tool_schemas()}
    properties = schemas["send_input"]["function"]["parameters"]["properties"]
    assert set(properties) == {"child_id", "message"}

async def test_spawn_dispatcher_sends_input_to_current_session_child(
    fake_spawn_manager: FakeSpawnManager,
) -> None:
    dispatch = build_spawn_tool_dispatcher(fake_spawn_manager, session_id=9)
    await dispatch("spawn_agent", {"role": "ops_explorer", "task_brief": "检查资产"})
    result = await dispatch(
        "send_input", {"child_id": "child-0", "message": "再核查最近五分钟"}
    )
    assert result.control == "ok"
    assert fake_spawn_manager.sent_inputs == [("child-0", "再核查最近五分钟")]
```

复用已有其它会话 child 测试形状，再断言 `send_input` 不能跨 session；增加空白 message 返回 `clarification`。

- [ ] **Step 2: 运行新增测试并确认 RED**

Run from `backend/`: `uv run pytest tests/test_agent_spawn_tools.py -v`

Expected: schema 中没有 `send_input`，dispatcher 返回未知工具。

- [ ] **Step 3: 实现五原语集合和 dispatcher 分支**

定义集合与参数：

```python
SPAWN_PRIMITIVE_TOOL_NAMES = frozenset(
    {"spawn_agent", "wait_agent", "send_input", "list_agents", "close_agent"}
)

class SendInputArgs(_Args):
    child_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
```

把严格 schema 插在 wait/list/close 之间，并在 dispatcher 中显式分支：

```python
if name == "send_input":
    send_args = SendInputArgs.model_validate(arguments)
    await _require_session_child(manager, session_id, send_args.child_id)
    receipt = await manager.send_input(send_args.child_id, send_args.message)
    return ToolResult(control="ok", content=_safe_receipt_text(receipt))
```

不要增加 API 路由；模型工具是本设计要求的出口。

- [ ] **Step 4: 验证 Task 4 GREEN**

Run from `backend/`:

```powershell
uv run pytest tests/test_agent_spawn_tools.py tests/test_agent_spawn.py::test_send_input_only_appends_to_a_running_child -v
uv run mypy app/agent/spawn_tools.py
uv run ruff check app/agent/spawn_tools.py tests/test_agent_spawn_tools.py
```

Expected: manager 和工具两层语义都通过。

- [ ] **Step 5: 提交 Task 4**

```powershell
git add backend/app/agent/spawn_tools.py backend/tests/test_agent_spawn_tools.py
git commit -m "向根 Agent 暴露 send_input 原语" -m "- 将 send_input 加入五个 Spawn 原语的严格工具契约`n- 复用会话 child 归属和 RUNNING 状态校验，禁止跨会话补充输入`n- 补齐 schema、空白消息和 dispatcher 行为测试"
```

---

### Task 5: 让工作流 reviewer 实际成为二级子 Agent

**Files:**
- Modify: `backend/app/agent/orchestration.py`
- Test: `backend/tests/test_agent_orchestration.py`
- Test: `backend/tests/test_agent_spawn_integration.py`

**Interfaces:**
- Consumes: `SpawnController.spawn_agent(..., parent_agent_id=...)`、终态未 close 仍占并发槽、只有 reviewer 可嵌套。
- Produces: `WaveResult.open_final_receipts`、`_run_reviewer(..., parent_agent_id=None)`；默认并发下 reviewer 具有 worker parent，单并发安全回退根级。

- [ ] **Step 1: 写默认嵌套 reviewer 失败测试**

修改低置信分类和根因综合测试，断言 reviewer 的 spawn request 指向最后一波成功 worker：

```python
reviewer_call = controller.spawn_calls[-1]
assert reviewer_call.role == "reviewer"
assert reviewer_call.parent_agent_id == "child-1"
assert controller.close_count_at_spawn[-1] >= 1
```

这里 `child-1` 是两文档测试最后一波中选定的成功 worker；断言 reviewer 创建前其它 sibling 已关闭，从而确实释放槽位。

- [ ] **Step 2: 运行单测并确认 RED**

Run from `backend/`:

```powershell
uv run pytest tests/test_agent_orchestration.py::test_confidence_below_point_eight_triggers_reviewer tests/test_agent_orchestration.py::test_partial_branch_failure_still_spawns_reviewer -v
```

Expected: `parent_agent_id` 实际为 `None`。

- [ ] **Step 3: 把最后一波关闭责任交给 workflow**

扩展结果：

```python
@dataclass(frozen=True, slots=True)
class WaveResult:
    receipts: tuple[ChildReceipt | None, ...]
    failures: tuple[WaveFailure, ...]
    open_final_receipts: tuple[ChildReceipt, ...]
```

`_run_wave` 继续在每个非最后波 wait 后 `_close_all`；最后一波 wait 完成后不关闭，把本波所有已 spawn receipt 放进 `open_final_receipts`。若 spawn/wait 流程抛异常或 task 取消，仍由 `_run_wave` 原有异常分支关闭它当前拥有的全部回执。

工作流拿到 `WaveResult` 后必须用 `try/finally` 接管：任何早退、解析错误、reviewer 异常或取消最终都关闭 `open_final_receipts`。

- [ ] **Step 4: 实现嵌套选择、顺序关闭和回退**

给 reviewer helper 增加可选父节点：

```python
async def _run_reviewer(
    controller: SpawnController,
    *,
    session_id: int,
    trace_id: str,
    task_brief: str,
    parent_agent_id: str | None = None,
) -> ChildReceipt | None:
    receipt = await controller.spawn_agent(
        session_id=session_id,
        role="reviewer",
        task_brief=task_brief,
        trace_id=trace_id,
        parent_agent_id=parent_agent_id,
    )
    ...
```

在每个 workflow 判定需要 review 后：

1. 从 `wave.receipts` 中倒序选择同时位于 `open_final_receipts`、wait 成功且状态为 `COMPLETED` 的 worker；
2. 只有 `max_concurrent_children >= 2` 才保留它；
3. 先关闭其它最后一波回执，再 spawn nested reviewer；
4. reviewer helper 返回或失败后，在 workflow `finally` 关闭保留 parent。

如果没有候选或并发为 1，先关闭最后一波全部回执，再以 `parent_agent_id=None` 创建根级 reviewer。

- [ ] **Step 5: 写单并发回退和取消清理测试**

新增测试使用 `FakeSpawnController(max_concurrent_children=1)`，断言 reviewer 仍创建但 `parent_agent_id is None`，且 reviewer spawn 前 worker 已 close。扩展取消测试断言所有 worker、reviewer 和保留 parent 每个最终都出现于 `close_calls`，没有未关闭 receipt。

- [ ] **Step 6: 写真实 SpawnManager 集成失败测试**

在批量分类集成测试中查询 `manager.list_agents(session_id)`，找到 `role == "reviewer"` 的回执并断言：

```python
assert reviewer.parent_agent_id in {
    receipt.child_id for receipt in receipts if receipt.role == "classifier"
}
assert reviewer.agent_path.count("/") == 3
```

沿用 fake child runner，不发真实模型请求。该测试捕获“fake controller 记录正确，但真实 manager 生产关系仍是根级”的接线错误。

- [ ] **Step 7: 验证 Task 5 GREEN**

Run from `backend/`:

```powershell
uv run pytest tests/test_agent_orchestration.py tests/test_agent_spawn_integration.py -v
uv run mypy app/agent/orchestration.py
uv run ruff check app/agent/orchestration.py tests/test_agent_orchestration.py tests/test_agent_spawn_integration.py
```

Expected: 默认嵌套、单并发回退、波次上限、异常清理和取消清理全部通过。

- [ ] **Step 8: 提交 Task 5**

```powershell
git add backend/app/agent/orchestration.py backend/tests/test_agent_orchestration.py backend/tests/test_agent_spawn_integration.py
git commit -m "让编排 reviewer 使用二级子 Agent 关系" -m "- 将最后一波回执保留到结果解析后再决定 reviewer 父节点`n- 默认并发下形成 root-worker-reviewer，单并发时回退根级避免死锁`n- 保持异常、取消和 close 失败路径的槽位清理保证"
```

---

### Task 6: 把两个确定性编排工作流接入根 Agent 工具面

**Files:**
- Modify: `backend/app/agent/spawn_tools.py`
- Modify: `backend/app/agent/chat_turn.py:54-68,115-205`
- Test: `backend/tests/test_agent_spawn_tools.py`
- Test: `backend/tests/test_chat_turn.py`

**Interfaces:**
- Consumes: Task 4 的五原语集合、Task 5 的 `classify_documents`/`investigate_root_cause` 生命周期。
- Produces: `ORCHESTRATION_TOOL_NAMES`、七工具 dispatcher 全集、两个严格工作流参数模型和 JSON outcome。

- [ ] **Step 1: 写工具 schema 失败测试**

新增测试明确区分五原语和两工作流：

```python
def test_spawn_primitives_and_orchestration_tools_are_exposed_separately() -> None:
    assert SPAWN_PRIMITIVE_TOOL_NAMES == {
        "spawn_agent", "wait_agent", "send_input", "list_agents", "close_agent"
    }
    assert ORCHESTRATION_TOOL_NAMES == {
        "classify_documents", "investigate_root_cause"
    }
    assert SPAWN_TOOL_NAMES == SPAWN_PRIMITIVE_TOOL_NAMES | ORCHESTRATION_TOOL_NAMES
    assert {item["function"]["name"] for item in spawn_tool_schemas()} == SPAWN_TOOL_NAMES
```

再断言分类 documents 最少 2、根因 incident_context 非空，且 schema 不包含 `session_id`、`model`、`budget` 或 `tools_allowlist`。

- [ ] **Step 2: 运行 schema 测试并确认 RED**

Run from `backend/`: `uv run pytest tests/test_agent_spawn_tools.py -v`

Expected: 两个工作流工具和集合不存在。

- [ ] **Step 3: 定义严格参数与 JSON 序列化**

在 `spawn_tools.py` 复用 orchestration 的领域模型：

```python
class ClassifyDocumentsArgs(_Args):
    documents: list[ClassificationDocument] = Field(min_length=2, max_length=50)
    allowed_categories: list[str] = Field(default_factory=list, max_length=50)

class InvestigateRootCauseArgs(_Args):
    incident_context: str = Field(min_length=1, max_length=8000)
    branches: list[RootCauseBranch] | None = Field(default=None, min_length=2, max_length=10)
```

定义：

```python
ORCHESTRATION_TOOL_NAMES = frozenset(
    {"classify_documents", "investigate_root_cause"}
)
SPAWN_TOOL_NAMES = SPAWN_PRIMITIVE_TOOL_NAMES | ORCHESTRATION_TOOL_NAMES
```

使用 `TypeAdapter(BatchClassificationOutcome)` 和 `TypeAdapter(RootCauseOutcome)` 的 `dump_json(...).decode("utf-8")` 产生 JSON，避免手写递归转换遗漏 Pydantic 子模型。

- [ ] **Step 4: 写真实 dispatcher 工作流失败测试**

扩展 `FakeSpawnManager` 具备 `max_concurrent_children=5` 和脚本化 summary。分类测试给两个 classifier 返回合法 `_classification_json`，断言：

```python
result = await dispatch("classify_documents", {
    "documents": [
        {"document_id": 1, "title": "交换机", "file_path": "network/a.md"},
        {"document_id": 2, "title": "数据库", "file_path": "db/b.md"},
    ],
    "allowed_categories": ["网络", "数据库"],
})
payload = json.loads(result.content)
assert result.control == "ok"
assert [item["document_id"] for item in payload["suggestions"]] == [1, 2]
assert fake_spawn_manager.spawn_kwargs_history[0]["session_id"] == 9
```

根因测试使用两个自定义 branches 和合法 finding/review summary，断言 outcome 包含两个 finding 与 synthesis。测试只 fake child 模型结果，真实执行 dispatcher、workflow、严格解析、close 和 JSON 序列化。

- [ ] **Step 5: 运行 dispatcher 测试并确认 RED**

Run from `backend/`: `uv run pytest tests/test_agent_spawn_tools.py -v`

Expected: dispatcher 将工作流名判为未知工具。

- [ ] **Step 6: 实现两个 dispatcher 分支**

在 list/close fallback 之前显式分支：

```python
if name == "classify_documents":
    workflow_args = ClassifyDocumentsArgs.model_validate(arguments)
    outcome = await classify_documents(
        manager,
        session_id=session_id,
        documents=workflow_args.documents,
        allowed_categories=workflow_args.allowed_categories,
    )
    return ToolResult(
        control="ok",
        content=_BATCH_OUTCOME_ADAPTER.dump_json(outcome).decode("utf-8"),
    )

if name == "investigate_root_cause":
    workflow_args = InvestigateRootCauseArgs.model_validate(arguments)
    branches = (
        tuple(workflow_args.branches)
        if workflow_args.branches is not None
        else DEFAULT_ROOT_CAUSE_BRANCHES
    )
    outcome = await investigate_root_cause(
        manager,
        session_id=session_id,
        incident_context=workflow_args.incident_context,
        branches=branches,
    )
    return ToolResult(
        control="ok",
        content=_ROOT_CAUSE_OUTCOME_ADAPTER.dump_json(outcome).decode("utf-8"),
    )
```

`ValueError` 来自工作流领域校验时映射为 `clarification` 且只返回固定安全原因，不透传可能含用户数据的异常全文。

- [ ] **Step 7: 更新根提示词并验证真实 chat 工具表**

把 `ROOT_OPS_SYSTEM_PROMPT` 中只依赖手工 spawn 的说明改为：批量文档分类优先 `classify_documents`，多分支根因排查优先 `investigate_root_cause`；单对象操作禁止使用批量工作流；底层原语用于工作流前置条件不满足的其它并行任务。

在 `test_chat_turn.py` 的 fake chat 中读取首次请求 `tools`，断言两个工作流名和 `send_input` 均真实出现。该测试验证生产 `chat_turn -> spawn_tool_schemas -> dispatcher` 接线，而不是 grep 提示词文本。

- [ ] **Step 8: 验证 Task 6 GREEN**

Run from `backend/`:

```powershell
uv run pytest tests/test_agent_spawn_tools.py tests/test_chat_turn.py tests/test_agent_spawn_integration.py -v
uv run mypy app/agent/spawn_tools.py app/agent/chat_turn.py
uv run ruff check app/agent/spawn_tools.py app/agent/chat_turn.py tests/test_agent_spawn_tools.py tests/test_chat_turn.py
```

Expected: schema、真实 workflow、生产 chat 工具表和现有回合测试全部通过。

- [ ] **Step 9: 提交 Task 6**

```powershell
git add backend/app/agent/spawn_tools.py backend/app/agent/chat_turn.py backend/tests/test_agent_spawn_tools.py backend/tests/test_chat_turn.py
git commit -m "接入根 Agent 确定性并行工作流" -m "- 暴露批量文档分类和多分支根因排查两个严格工具 schema`n- 由服务端工作流负责分波并发、结果解析、复核和清理`n- 更新根工具选择说明并用 chat_turn 测试证明生产入口可达"
```

---

### Task 7: 区分预算超限、模型错误与策略提前退出

**Files:**
- Modify: `backend/app/agent/spawn.py:40-46,747-824,885-923`
- Modify: `backend/app/agent/spawn_tools.py:35-37`
- Test: `backend/tests/test_agent_spawn.py`
- Test: `backend/tests/test_agent_spawn_tools.py`

**Interfaces:**
- Consumes: `LoopOutcome.reason` 的四个值：`final_answer`、`budget_exceeded`、`early_exit`、`llm_error`。
- Produces: `ChildErrorClass` 五类和稳定映射；无需数据库迁移。

- [ ] **Step 1: 把现有墙钟测试期望改为失败的新契约**

重命名两个用例为 `test_wall_timeout_persists_failed_budget_exceeded` 和 `test_wall_timeout_wins_with_budget_exceeded_when_runner_swallows_cancellation`，把 trace 断言从 `policy_reject` 改为 `budget_exceeded`。保留 `test_inner_timeout_error_is_infra_not_wall_timeout`，证明普通依赖 `TimeoutError` 仍为 `infra`。

- [ ] **Step 2: 增加默认 runner 的预算和 llm_error 失败测试**

预算用例让 fake chat 返回包含 tool call 且本次 cost 超过 child 上限，断言工具没有执行、child `FAILED/budget_exceeded`。模型用例让 fake chat 抛 `LlmRequestError`，由于 `run_loop` 将其转换成 `LoopOutcome(reason="llm_error")`，断言最终 trace 为 `model`。

继续保留现有 clarification 测试并断言 `policy_reject`，形成三个互斥标签：

```python
assert budget_event.error_class == "budget_exceeded"
assert llm_event.error_class == "model"
assert clarification_event.error_class == "policy_reject"
```

- [ ] **Step 3: 运行分类测试并确认 RED**

Run from `backend/`:

```powershell
uv run pytest tests/test_agent_spawn.py -k "wall_timeout or budget_exceeded or llm_error or clarification or inner_timeout" -v
```

Expected: 墙钟和默认 runner 新期望收到现有 `policy_reject`。

- [ ] **Step 4: 实现五类类型和映射**

修改固定集合：

```python
type ChildErrorClass = Literal[
    "model", "tool", "policy_reject", "infra", "budget_exceeded"
]
_SAFE_ERROR_CLASSES = frozenset(
    {"model", "tool", "policy_reject", "infra", "budget_exceeded"}
)
```

墙钟分支只在 `wall_timeout.expired()` 时映射 `budget_exceeded`，内部 `TimeoutError` 仍映射 `infra`。默认 runner 使用显式 reason 表：

```python
if outcome.reason == "final_answer":
    return ChildRunResult(status="COMPLETED", result_summary=outcome.final_answer)
error_class: ChildErrorClass = {
    "budget_exceeded": "budget_exceeded",
    "llm_error": "model",
    "early_exit": "policy_reject",
}[outcome.reason]
return ChildRunResult(status="FAILED", result_summary=None, error_class=error_class)
```

同步把 `spawn_tools.py` 的模型可见安全白名单加入 `budget_exceeded`。

- [ ] **Step 5: 验证 Task 7 GREEN**

Run from `backend/`:

```powershell
uv run pytest tests/test_agent_spawn.py tests/test_agent_spawn_tools.py -v
uv run mypy app/agent/spawn.py app/agent/spawn_tools.py
uv run ruff check app/agent/spawn.py app/agent/spawn_tools.py tests/test_agent_spawn.py tests/test_agent_spawn_tools.py
```

Expected: 五类映射、内部 timeout、默认 runner 和安全错误输出全部通过。

- [ ] **Step 6: 提交 Task 7**

```powershell
git add backend/app/agent/spawn.py backend/app/agent/spawn_tools.py backend/tests/test_agent_spawn.py backend/tests/test_agent_spawn_tools.py
git commit -m "细分子 Agent 预算与模型失败标签" -m "- 新增 budget_exceeded 并用于 step、cost 和墙钟预算终止`n- 将 run_loop 的 llm_error 映射为 model，保留 early_exit 的 policy_reject`n- 保证内部依赖 TimeoutError 继续归为 infra，避免误判墙钟预算"
```

---

### Task 8: 同步运行时文档并执行全量验收

**Files:**
- Modify: `docs/AGENT_ARCHITECTURE.md:310-386,390-406,423-430,482-495`
- Modify: `docs/guide.md:232-258,350-375,482-495,620-632`
- Modify: `docs/superpowers/specs/2026-08-11-t09-spawn-orchestration-design.md`
- Modify: `docs/superpowers/specs/2026-08-14-agent-runtime-reliability-repair-design.md:40-86`
- Verify only: all production/test files changed by Tasks 1-7

**Interfaces:**
- Consumes: Tasks 1-7 已通过的真实运行语义。
- Produces: 当前架构、guide 和现行 spec 对五原语、两工作流、权限广播、二级 reviewer、五类错误、GC 与 HITL 边的统一说明。

- [ ] **Step 1: 修正 Spawn、reviewer 与 GC 文档**

在三份当前契约文档中明确：

```text
spawn_agent / wait_agent / send_input / list_agents / close_agent
```

是五个原语；`classify_documents` 与 `investigate_root_cause` 是服务端确定性工作流工具。默认 reviewer 形成 `root -> worker -> reviewer`，单并发或无可用父节点才回退根级。

把 GC 规则改成：只关闭超过 TTL 的终态回执且 `force_closed=false`；运行中 task 只有显式 close 取消超时强制 detach 才 `force_closed=true`；启动 reconciliation 也是 false。

- [ ] **Step 2: 修正监控广播、错误标签与持久化说明**

把“订阅资产/网段”改为“所有带 `monitor:read` 的在线 Agent WebSocket peer”。明确首次探测不广播、同状态更新当前行、翻转追加行并在 commit 后广播。

把错误分类统一为：

```text
model | tool | policy_reject | infra | budget_exceeded
```

并列出 budget/cost/wall-time、llm_error、early_exit 的对应关系。

- [ ] **Step 3: 补全 HITL 状态图和规则**

在 `AGENT_ARCHITECTURE.md`、`guide.md` 与 2026-08-14 可靠性设计中加入：

```text
APPROVED --preflight: policy_blacklisted--> REJECTED
```

同时保留精确区分：命令不存在或动态凭据缺失时不认领、状态保持 `APPROVED`；当前策略已黑名单时原子转 `REJECTED` 并写 `status_reason=policy_blacklisted`。

- [ ] **Step 4: 执行文档一致性扫描**

Run from repository root:

```powershell
rg -n "四个.*Spawn|四类|订阅了该资产|GC.*force_closed=true|root-level reviewer|根级 reviewer" docs/AGENT_ARCHITECTURE.md docs/guide.md docs/superpowers/specs/2026-08-11-t09-spawn-orchestration-design.md docs/superpowers/specs/2026-08-14-agent-runtime-reliability-repair-design.md
git diff --check
```

Expected: 只允许命中“单并发回退根级 reviewer”的新说明；不得残留旧四原语、四类错误、伪订阅或 GC 强制关闭描述；diff 无空白错误。

- [ ] **Step 5: 运行后端全量验收**

Run from `backend/`:

```powershell
uv run pytest -v
uv run mypy app
uv run ruff check .
```

Expected: pytest 全部通过（依赖外部 Postgres 的既有条件用例可保持 SKIPPED），mypy 输出 `Success: no issues found`，ruff 输出 `All checks passed!`。

- [ ] **Step 6: 运行前端全量验收**

Run from `frontend/`:

```powershell
npm test
npm run typecheck
npm run lint
npm run build
```

Expected: Vitest 全部通过，typecheck/lint 无错误，Vite 生产构建成功。

- [ ] **Step 7: 检查最终变更范围**

Run from repository root:

```powershell
git status --short
git diff --stat 9493dc3..HEAD
git diff --check 9493dc3..HEAD
```

逐项确认每个文件都能追溯到本计划八个问题；不得包含 `.env`、凭据、构建产物、无关格式化或依赖锁变动。

- [ ] **Step 8: 提交 Task 8 文档收尾**

```powershell
git add docs/AGENT_ARCHITECTURE.md docs/guide.md docs/superpowers/specs/2026-08-11-t09-spawn-orchestration-design.md docs/superpowers/specs/2026-08-14-agent-runtime-reliability-repair-design.md
git commit -m "同步 Agent 生产运行时契约" -m "- 记录五个 Spawn 原语、两个工作流工具和二级 reviewer 的实际生命周期`n- 对齐 monitor:read 告警广播、五类错误与 GC force_closed 语义`n- 补全 HITL 黑名单复检从 APPROVED 到 REJECTED 的状态边"
```

---

## 完成定义

只有同时满足以下条件才可宣布修复完成：

- `child_status` 经过真实前端 parser 后进入现有 reducer；
- 首次/同状态探活不告警，up/down 翻转在 commit 后只推给 `monitor:read` peer；
- 根工具表真实包含五个 Spawn 原语和两个工作流工具；
- 默认分类/根因工作流的 reviewer 持久化为深度 2，单并发无死锁且所有回执关闭；
- wall-time/budget、llm_error、early_exit 在 trace 中分别可区分；
- GC 和 HITL 文档与现有代码迁移一致；
- 后端 pytest/mypy/ruff 与前端 test/typecheck/lint/build 全部达到 Task 8 的预期；
- 所有提交只留在本地 `master`，没有 push。
