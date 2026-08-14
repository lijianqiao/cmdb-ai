# Agent 内核第一部分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给运维 Web Agent 补上：`chat()` 调用失败编码为 `finish_reason=error`、纯循环 before/after 钩子（HITL 门控 + 执行工具改名）、根会话 LLM 压缩摘要（系统提示永不进摘要）。

**Architecture:** `run_loop` 仍不看工具名。传输失败由 `llm.chat` 返回 `ChatResult(finish_reason="error")`，循环收成 `llm_error`。根会话 `HitlGateHook` 在 dispatch 前 `gate_action`，薄工具只执行；自动批准用 `attach_execution_result` 回写提案，禁止二次 `resume_proposal`。压缩走 `compaction.py` 直接调 `llm.chat`（不走会推 WS 的 `chat_fn`），摘要只注入 `agent_id is None` 的模型窗口。

**Tech Stack:** Python 3.14.3、FastAPI、SQLAlchemy 2 async、Alembic、Pydantic v2、httpx、pytest。`uv run`。不新增依赖。

**Spec:** `docs/superpowers/specs/2026-08-14-agent-kernel-context-hooks-llm-design.md`

## Global Constraints

- 只在 `master` 工作，不建分支；每个任务验证通过后按项目规范提交一次，禁止 `Co-Authored-By`，未要求不要 push。
- 后端在 `backend/` 下用 `uv run pytest ...`。不新增 Python 依赖。
- 不编辑真实 `backend/.env`。
- Python 文件头注释、中文 Google 风格文档字符串；禁止 `from __future__ import annotations`。
- 不改 Scrapli 厂商驱动、不改 CMDB/监控确定性管道、不加多厂商 LLM、不把 MCP/沙箱进内核。
- 会话审批三档判定表、动态凭据必须人输密码、黑名单硬拒绝：一字不改。
- `AgentSessionResponse` 不加 `memory_summary` / `compacted_through_message_id`。
- 前端没有 `propose_*` 字符串，本期不改 frontend。
- `ensure_root_compaction` 低于阈值必须直接 return、不调用 `chat`，这样既有 `test_agent_loop` 不必为压缩 mock 网络。

## File Structure

| 文件 | 职责 |
| :--- | :--- |
| `backend/app/core/llm.py` | 调用失败返回 `finish_reason="error"`；未知键仍抛 |
| `backend/app/agent/loop.py` | `llm_error`；`BeforeToolDecision`；before/after；先看 `finish_reason` |
| `backend/app/agent/chat_turn.py` | 挂 `HitlGateHook`；系统提示改工具名；`llm_error` 广播 WS |
| `backend/app/agent/hitl.py` | `propose_action`/`gate_action` 不再 `resume_proposal`；新增 `attach_execution_result` |
| `backend/app/agent/hitl_gate.py` | **新建** `HitlGateHook` |
| `backend/app/agent/hitl_tools.py` | `notify` / `device_control` 薄执行；`query_device_command` 不再建提案 |
| `backend/app/agent/tool_dispatch.py` | Schema `t11-v1`；路由新工具名 |
| `backend/app/agent/compaction.py` | **新建** 根会话压缩 |
| `backend/app/agent/session.py` | 组装窗口：系统提示 + 根摘要块 + 最近原文 |
| `backend/app/models/agent_session.py` + Alembic | `memory_summary`、`compacted_through_message_id` |
| `docs/AGENT_ARCHITECTURE.md`、`docs/guide.md` | 工具名与压缩落地 |

---

### Task 1: `chat()` 调用失败改为 `finish_reason="error"`

**Files:**
- Modify: `backend/app/core/llm.py`
- Modify: `backend/tests/test_agent_llm.py`

**Interfaces:**
- Consumes: 现有 `chat()` / `embed()` 签名不变
- Produces: 传输 / HTTP 非 200 / 坏 JSON / SSE 损坏 → `ChatResult(content="模型调用失败：...", tool_calls=[], finish_reason="error", prompt_tokens=0, completion_tokens=0, cost_usd=0.0)`；未知模型键、capability 不匹配、解密失败仍抛 `LlmRequestError`；`embed()` 仍抛

- [ ] **Step 1: 把失败测试改成期望 `ChatResult`，先跑确认仍是 raises（红）**

把 `test_chat_raises_on_non_200`、`test_chat_wraps_transport_failures_as_model_errors`、`test_chat_raises_llm_request_error_on_invalid_json_body`、`test_chat_stream_raises_on_non_200` 改成断言返回值，例如：

```python
async def test_chat_returns_error_result_on_non_200() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as fake_client:
        result = await chat(
            "local-chat",
            [ChatMessage(role="user", content="hi")],
            client=fake_client,
        )
    assert result.finish_reason == "error"
    assert result.tool_calls == []
    assert result.prompt_tokens == 0
    assert "模型调用失败" in (result.content or "")
    assert "HTTP 500" in (result.content or "")
    assert result.content is not None and len(result.content) <= 400
```

传输失败用例：断言 `finish_reason=="error"`，且 `"transport secret"` 不在 `content` 里。  
`test_chat_rejects_unknown_model_key`、`test_embed_raises_on_non_200` **保持** `pytest.raises(LlmRequestError)`。

- [ ] **Step 2: 跑测试确认失败方式是「仍在 raise」而不是断言写错**

```text
cd backend
uv run pytest tests/test_agent_llm.py::test_chat_returns_error_result_on_non_200 tests/test_agent_llm.py::test_chat_rejects_unknown_model_key -v
```

Expected: 新测试 FAIL（`LlmRequestError` 或 pytest.raises 残留）；未知键测试仍 PASS。

- [ ] **Step 3: 最小实现**

在 `chat()` 的 HTTP/传输/JSON/SSE 分支：捕获后 `return ChatResult(...)`，不要 `raise`。HTTP 正文拼进中文短因时截断到 200 字符。抽取小函数 `_error_result(reason: str) -> ChatResult` 以免流式/非流式两套文案。`_resolve_model_config` / capability 检查保持 raise。

- [ ] **Step 4: 跑 LLM 测试全绿**

```text
cd backend
uv run pytest tests/test_agent_llm.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```text
提交 llm.chat 调用失败编码为 finish_reason=error

- HTTP/传输/坏 JSON/SSE 损坏返回 ChatResult，不再抛给循环
- 未知模型键和 embed 失败仍抛 LlmRequestError
```

---

### Task 2: 循环把 `error` 收成 `llm_error`，聊天 WS 不把失败当终答

**Files:**
- Modify: `backend/app/agent/loop.py`
- Modify: `backend/app/agent/chat_turn.py`
- Modify: `backend/tests/test_agent_loop.py`
- Modify: `backend/tests/test_chat_turn.py`

**Interfaces:**
- Consumes: Task 1 的 `ChatResult.finish_reason`
- Produces: `LoopOutcome.reason` 增加 `"llm_error"`；`run_loop` 在 `record_cost` / `tool_calls` 之前处理 error；`run_chat_turn` 对 `llm_error` 广播 `type="error"` 再 `turn_done`，不 raise

- [ ] **Step 1: 写失败测试**

`test_agent_loop.py`：

```python
async def test_loop_returns_llm_error_without_dispatching_or_final_answer(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "查一下")
    await db_session.commit()

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            content="模型调用失败：HTTP 502",
            tool_calls=[],
            finish_reason="error",
            prompt_tokens=0,
            completion_tokens=0,
        )

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=_never_called_dispatch,
        chat_fn=fake_chat,
    )
    await db_session.commit()

    assert outcome == LoopOutcome(reason="llm_error", final_answer=None)
    history = await build_model_history(db_session, session_id)
    assert [m.role for m in history] == ["user"]
```

`test_chat_turn.py`：mock `run_loop` 返回 `LoopOutcome(reason="llm_error", final_answer=None)`，断言 hub 收到 `type=="error"` 且随后 `turn_done` 的 `reason=="llm_error"`，函数不抛。

- [ ] **Step 2: 跑测试确认红**

```text
cd backend
uv run pytest tests/test_agent_loop.py::test_loop_returns_llm_error_without_dispatching_or_final_answer -v
```

Expected: FAIL（`reason=="final_answer"` 或把错误正文写成助手消息）

- [ ] **Step 3: 最小实现**

`LoopOutcome.reason` Literal 加上 `"llm_error"`。`run_loop` 在拿到 `ChatResult` 后：

```python
if result.finish_reason == "error":
    return LoopOutcome(reason="llm_error", final_answer=None)
```

然后再 `record_cost`。`chat_turn` 在 `run_loop` 返回后、广播 `turn_done` 前：若 `outcome.reason == "llm_error"`，先广播中文 `error`（「模型调用失败，请稍后重试」）。

- [ ] **Step 4: 跑相关测试**

```text
cd backend
uv run pytest tests/test_agent_loop.py tests/test_chat_turn.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```text
循环将模型 finish_reason=error 收成 llm_error

- 不把失败正文写成助手终答，也不调度工具
- 聊天回合对 llm_error 推 WS error 后 turn_done，不再当异常抛出
```

---

### Task 3: `run_loop` 增加 before/after 钩子（循环仍不解析工具参数）

**Files:**
- Modify: `backend/app/agent/loop.py`
- Modify: `backend/tests/test_agent_loop.py`

**Interfaces:**
- Consumes: 现有 `dispatch_tool` / `ToolResult`
- Produces:

```python
@dataclass(frozen=True, slots=True)
class BeforeToolDecision:
    block: bool
    result: ToolResult | None = None

BeforeToolCall = Callable[[str, dict[str, Any]], Awaitable[BeforeToolDecision]]
AfterToolCall = Callable[[str, dict[str, Any], ToolResult], Awaitable[None]]
```

`run_loop(..., before_tool_call=None, after_tool_call=None)`。默认等价于始终 `BeforeToolDecision(block=False)` 与空 after。`block=True` 时 `result` 必填；循环写入该 `ToolResult`，不调 dispatch、不调 after；`pending_approval` 仍 early_exit 并给后续 call 写「已跳过」。

- [ ] **Step 1: 写失败测试**

```python
async def test_loop_before_hook_can_block_without_dispatching(
    db_session: AsyncSession, test_user: User
) -> None:
    session_id = await _make_session(db_session, test_user.id)
    await append_user_message(db_session, session_id, "重启")
    dispatched: list[str] = []

    async def fake_chat(model_key: str, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            content=None,
            tool_calls=[ToolCall(id="call_1", name="device_control", arguments='{"asset_id":1}')],
            finish_reason="tool_calls",
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def fake_dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        dispatched.append(name)
        return ToolResult(control="ok", content="should not run")

    async def before(name: str, arguments: dict[str, Any]) -> BeforeToolDecision:
        assert name == "device_control"
        assert arguments == {"asset_id": 1}
        return BeforeToolDecision(
            block=True,
            result=ToolResult(control="pending_approval", content="已提交审批"),
        )

    outcome = await run_loop(
        db_session,
        session_id=session_id,
        model_key="local-chat",
        dispatch_tool=fake_dispatch,
        chat_fn=fake_chat,
        before_tool_call=before,
    )
    assert outcome.reason == "early_exit"
    assert outcome.control == "pending_approval"
    assert dispatched == []
```

再写一条：before 放行后 after 被调用一次，且循环不根据 `name=="device_control"` 写死逻辑（用 `query_cmdb` 也能走 after）。

- [ ] **Step 2: 跑测试确认红**

```text
cd backend
uv run pytest tests/test_agent_loop.py::test_loop_before_hook_can_block_without_dispatching -v
```

Expected: FAIL（`before_tool_call` 不是合法参数或仍调用了 dispatch）

- [ ] **Step 3: 最小实现**

只改 `loop.py`：增加类型与默认 no-op；在 `_parse_arguments` 之后、`dispatch_tool` 之前调用 before。不要在 loop 里 import hitl。

- [ ] **Step 4: 跑 loop 测试**

```text
cd backend
uv run pytest tests/test_agent_loop.py -v
```

Expected: PASS（含既有 pending_approval 用例：不传钩子时仍靠 dispatch 返回的 control）

- [ ] **Step 5: Commit**

```text
Agent 循环增加 before/after 工具钩子

- block 时不调度工具、不调用 after
- 循环仍然不根据工具名做 HITL 分支
```

---

### Task 4: HITL 门控进钩子，模型改为执行工具名

**Files:**
- Create: `backend/app/agent/hitl_gate.py`
- Modify: `backend/app/agent/hitl.py`
- Modify: `backend/app/agent/hitl_tools.py`
- Modify: `backend/app/agent/tool_dispatch.py`
- Modify: `backend/app/agent/chat_turn.py`
- Modify: `backend/tests/test_agent_hitl.py`
- Modify: `backend/tests/test_agent_hitl_tools.py`
- Modify: `backend/tests/test_agent_tool_dispatch.py`
- Modify: `backend/tests/test_hitl_integration.py`
- Modify: `backend/tests/test_device_command_execution_integration.py`
- Modify: `backend/tests/test_chat_turn.py`
- Modify: `backend/tests/test_agent_roles.py`（若断言旧工具名）

**Interfaces:**
- Consumes: Task 3 的 `BeforeToolDecision`；现有 `should_auto_approve` / `decide_proposal` / `resume_proposal` / 执行器
- Produces:
  - `gate_action(...)`：今天 `propose_action` 去掉末尾 `resume_proposal`；可保留 `propose_action = gate_action` 别名以免漏改内部调用，但**不得**再自动执行
  - `attach_execution_result(db, proposal_id, tool_result, actor_user_id, publisher)`：把 `ok` 标 `EXECUTED` 并写入 `last_result_excerpt`；失败保持 `APPROVED` + `last_error`。内部**禁止**调用执行器和 `resume_proposal`
  - `HitlGateHook.before/after`：门控集合 `notify` / `device_control` / `query_device_command`；其它工具立即放行
  - 根 Schema `ROOT_TOOL_SCHEMA_VERSION = "t11-v1"`：删除 `propose_remediation` / `propose_device_control`；增加 `notify`（`asset_id`, `payload`, `reason`）、`device_control`（同今天的管控参数）
  - `query_device_command` 只跑 Scrapli
  - `run_chat_turn` 传入 `HitlGateHook`；`ROOT_OPS_SYSTEM_PROMPT` 改用新工具名
  - 子调度器仍拒绝这三个执行工具
  - `hitl.py` 混用命令的中文错误改为提示 `device_control` / `query_device_command`

- [ ] **Step 1: 写/改失败测试（先红）**

1. `test_agent_hitl.py`：自动批准路径下 `gate_action`/`propose_action` **不再**把提案变成 `EXECUTED`（停留 `APPROVED`），且不调执行器。  
2. 新增 `test_attach_execution_result_marks_executed_without_resume`：mock `resume_proposal` 若被调用则失败。  
3. `test_agent_hitl_tools.py`：根 Schema 函数名含 `notify`/`device_control`，不含 `propose_*`。  
4. `test_hitl_integration.py` / 设备集成：`dispatch("notify"|"device_control"|...)` 必须经过 `HitlGateHook`（与 `run_loop` 相同顺序：before → dispatch → after）。`assist` + 白名单静态凭据：执行器调用次数 `== 1`。`ask`：PENDING 且执行器次数 `== 0`。  
5. `test_chat_turn.py`：系统提示含 `notify`/`device_control`，不含 `propose_remediation`。  
6. 子调度器：`dispatch("notify", ...)` → `rejected`。  
7. `list_device_commands` 返回给模型的说明若仍写 `propose_device_control`，改为 `device_control`。

- [ ] **Step 2: 跑一小组确认红**

先把 Schema 测试改成断言 `notify` / `device_control` 且无 `propose_*`，然后：

```text
cd backend
uv run pytest tests/test_agent_hitl_tools.py -k schema -v
```

Expected: FAIL（根 Schema 仍是 t10 旧名）。

- [ ] **Step 3: 实现（按依赖顺序，仍属本任务）**

1. `hitl.py`：拆掉 `propose_action` 里的 `resume_proposal`；抽出 `attach_execution_result`（从 `resume_proposal` 成功/失败写库那段复制语义，不调执行器）。  
2. `hitl_tools.py`：`async def notify(...)` / `async def device_control(...)` 只调 `NotifyExecutor` / `DeviceQueryExecutor`；删除或停止导出 `propose_remediation` / `propose_device_control`。`query_device_command` 去掉 `propose_action`。  
3. `hitl_gate.py`：`HitlGateHook` 用 `tool_dispatch` 里同一套 Args 模型校验门控工具 → `gate_action` → 返回 `BeforeToolDecision`；`after` 若有记住的 `proposal_id` 则 `attach_execution_result`。  
4. `tool_dispatch.py`：Schema 与路由改名；`build_root_tool_dispatcher` 不再在工具函数里建提案。  
5. `chat_turn.py`：构造 `HitlGateHook` 传给 `run_loop`；改系统提示。  
6. 所有测试里的旧工具名替换；集成测试用 hook+dispatch，不要只调薄工具而跳过门控。

- [ ] **Step 4: 跑 HITL / 调度 / 聊天 / 设备执行测试**

```text
cd backend
uv run pytest tests/test_agent_hitl.py tests/test_agent_hitl_tools.py tests/test_agent_tool_dispatch.py tests/test_hitl_integration.py tests/test_device_command_execution_integration.py tests/test_chat_turn.py tests/test_agent_roles.py tests/test_agent_loop.py -v
```

Expected: PASS。若个别文件名/测试名已改，按新名跑，覆盖不得变少。

- [ ] **Step 5: Commit**

```text
HITL 门控挪到 before 钩子，模型改为 notify/device_control

- propose_action 不再自动执行；自动批准由薄工具执行一次再 attach 回写
- 根 Schema t11 去掉 propose_*；子 Agent 仍不能调用执行工具
- 审批三档、黑名单、动态密码规则保持不变
```

---

### Task 5: 根会话压缩摘要（系统提示永不进摘要）

**Files:**
- Modify: `backend/app/models/agent_session.py`
- Create: `backend/alembic/versions/2026_08_14_1000-c1a8e4b7d902_session_compaction.py`（`down_revision = "b9e2d4c1a856"`）
- Create: `backend/app/agent/compaction.py`
- Modify: `backend/app/agent/session.py`
- Modify: `backend/app/agent/loop.py`（根会话调用 `ensure_root_compaction`，注入的 `chat_fn` 不传给压缩）
- Modify: `backend/tests/test_agent_session.py`
- Create: `backend/tests/test_agent_compaction.py`
- Modify: `backend/tests/test_agent_sessions_api.py`（详情 JSON 不含摘要字段）
- Modify: `backend/tests/test_agent_models.py` / `test_agent_migration_contract.py`（若合同断言列集合）

**Interfaces:**
- Consumes: Task 1 的 `finish_reason=="error"`；`Budget.record_cost`；`llm.chat`
- Produces:
  - 列：`memory_summary: str | None`、`compacted_through_message_id: int | None`
  - 常量：`COMPACT_TOKEN_THRESHOLD=12000`、`COMPACT_RECENT_RAW_MESSAGES=16`、`COMPACT_FALLBACK_MAX_MESSAGES=40`、`COMPACT_TOOL_RESULT_CHAR_LIMIT=2000`
  - `ensure_root_compaction(db, session_id, *, budget, system_prompt)` → 直接 `from app.core.llm import chat`，`stream=False`
  - `build_model_history`：`agent_id is None` 且有摘要时插入 `role=user` 前缀块；子 Agent 不插入；有摘要时原文从 `id > compacted_through_message_id` 取最近 16 条，否则 40 条

- [ ] **Step 1: 写失败测试**

`test_agent_compaction.py` 核心用例：

```python
async def test_compaction_excludes_ops_system_prompt_and_does_not_delete_messages(
    db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    ...
    captured: dict[str, object] = {}

    async def fake_chat(model_key, messages, **kwargs):
        captured["messages"] = messages
        captured["stream"] = kwargs.get("stream", False)
        return ChatResult(
            content="查过资产 12，IP 10.0.0.5",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
        )

    monkeypatch.setattr("app.agent.compaction.chat", fake_chat)
    # 造足够多根消息使估计 token >= 12000，或直接把阈值 monkeypatch 小
    monkeypatch.setattr("app.agent.compaction.COMPACT_TOKEN_THRESHOLD", 10)
    await ensure_root_compaction(db_session, session_id, budget=Budget(), system_prompt="运维助手根指令")
    sent = captured["messages"]
    assert captured["stream"] is False
    assert all("运维助手根指令" not in (m.content or "") for m in sent)
    assert any(m.role == "system" for m in sent)  # 摘要器自己的短指令
    rows = await agent_message_crud.list_for_agent(db_session, session_id, agent_id=None)
    assert len(rows) >= original_count  # 一行未删
```

另测：超长 tool 正文送进摘要器前被截到 `COMPACT_TOOL_RESULT_CHAR_LIMIT`；`build_model_history(..., agent_id="child-1")` 不含「不是新的用户指令」；根历史第一条仍是传入的 `system_prompt`，第二条摘要前缀；压缩 `finish_reason=error` 时不更新 `memory_summary`，根窗口仍 40 条上限；低于阈值时 `fake_chat` 调用次数为 0。  
`test_agent_sessions_api.py`：`GET /sessions/{id}` 的 `data` keys 不含 `memory_summary`。

- [ ] **Step 2: 跑测试确认红**

```text
cd backend
uv run pytest tests/test_agent_compaction.py -v
```

Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

模型加列 + Alembic（可空，无 server_default 文本；无外键）。`compaction.py` 文件头中文说明：为什么压缩、为什么不走 `chat_fn`、系统提示为何隔离。`run_loop` 仅 `agent_id is None` 时、在用户可见 `chat_fn` 之前调用；压缩费用 `record_cost`，超预算或 error 则跳过。局部变量 `last_prompt_tokens` 用于下一轮触发，不落库。

- [ ] **Step 4: 跑压缩 + 会话 + loop 测试**

```text
cd backend
uv run pytest tests/test_agent_compaction.py tests/test_agent_session.py tests/test_agent_sessions_api.py tests/test_agent_loop.py tests/test_agent_models.py tests/test_agent_migration_contract.py -v
```

Expected: PASS。SQLite 单测加列需与现有 fixture 一致（模型字段即可；迁移合同按仓库现有写法补两列）。

- [ ] **Step 5: Commit**

```text
根会话增加 LLM 压缩摘要，审计消息不删除

- 系统提示每轮从代码注入，不进摘要请求
- 摘要直接调 llm.chat，不推 WebSocket
- 子 Agent 历史不注入根摘要；会话 API 不输出摘要列
```

---

### Task 6: 架构文档与 guide 工具名

**Files:**
- Modify: `docs/AGENT_ARCHITECTURE.md`（§8 压缩落地；工具表 `notify` / `device_control`；L1/L2 文案）
- Modify: `docs/guide.md`（本项目工具名若仍写 `propose_*` 则改）
- Modify: `backend/app/agent/session.py` 模块 docstring（去掉「Deliberately no compaction」）

**Interfaces:**
- Consumes: Task 4–5 已落地行为
- Produces: 文档与代码一致；不新增产品功能

- [ ] **Step 1: 对照 spec §8 改文档**（无单独失败测试；改完跑一次全量相关测试防回归）

```text
cd backend
uv run pytest tests/test_agent_llm.py tests/test_agent_loop.py tests/test_agent_session.py tests/test_agent_compaction.py tests/test_agent_hitl.py tests/test_agent_hitl_tools.py tests/test_hitl_integration.py tests/test_device_command_execution_integration.py tests/test_chat_turn.py tests/test_agent_sessions_api.py -v
```

Expected: PASS

- [ ] **Step 2: Commit**

```text
文档同步 Agent 内核压缩、钩子与执行工具名

- 架构 §8 写明模型窗口与审计历史分离
- 工具表改为 notify/device_control，不再把 propose_* 当模型入口
```

---

## Plan Self-Review

**Spec coverage:**

| Spec | Task |
| :--- | :--- |
| §6 chat 错误编码 | Task 1 |
| §5.1 / §6.3 llm_error 与先看 finish_reason | Task 2 |
| §5.1 before/after | Task 3 |
| §5.2–5.5 工具改名、gate、attach、HitlGateHook、系统提示 | Task 4 |
| §4 压缩、迁移、不走 chat_fn、不注入 child、API 不暴露 | Task 5 |
| §8 文档 | Task 6 |
| 判定表/Scrapli/MCP/JSONL 不做 | Global Constraints |

**Placeholder scan:** 无 TBD。测试与命令均为可执行路径。

**Type consistency:** `BeforeToolDecision`、`LoopOutcome.reason="llm_error"`、`finish_reason="error"`、`ROOT_TOOL_SCHEMA_VERSION="t11-v1"`、`attach_execution_result`、`ensure_root_compaction`、Alembic `down_revision=b9e2d4c1a856` 前后任务一致。
