"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: spawn_tools.py
@DateTime: 2026-08-14
@Docs: 根 Agent 专用 Spawn 工具 schema 与安全 dispatcher。

实现流程：
1. 向根 Agent 暴露五个 Spawn 原语与两个确定性编排工作流，共七个服务端受控工具。
2. 模型只能指定角色、任务摘要或工作流领域参数；session_id、模型、工具白名单、
   预算由 SpawnManager 与角色目录决定，不得由模型覆盖。
3. dispatcher 校验参数后调用 SpawnManager 或 orchestration 工作流，回执只含安全字段。
4. 与 tool_dispatch 分离，避免 spawn.py 与 tool_dispatch 形成循环导入。
"""

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.agent.loop import ToolDispatcher, ToolResult
from app.agent.orchestration import (
    DEFAULT_ROOT_CAUSE_BRANCHES,
    BatchClassificationOutcome,
    ClassificationDocument,
    RootCauseBranch,
    RootCauseOutcome,
    classify_documents,
    investigate_root_cause,
)
from app.agent.roles import RoleName
from app.agent.spawn import (
    ChildNotFoundError,
    ChildReceipt,
    ChildRuntimeUnavailableError,
    ChildWaitTimeoutError,
    SpawnManager,
    SpawnRejectedError,
)
from app.agent.tool_dispatch import validation_reason_for_tool

SPAWN_TOOL_SCHEMA_VERSION = "t10-v1"
SPAWN_PRIMITIVE_TOOL_NAMES: frozenset[str] = frozenset(
    {"spawn_agent", "wait_agent", "send_input", "list_agents", "close_agent"}
)
ORCHESTRATION_TOOL_NAMES: frozenset[str] = frozenset(
    {"classify_documents", "investigate_root_cause"}
)
SPAWN_TOOL_NAMES: frozenset[str] = SPAWN_PRIMITIVE_TOOL_NAMES | ORCHESTRATION_TOOL_NAMES
_WORKFLOW_VALIDATION_REASON = "工作流输入不满足执行条件，请修正参数后重试"
_BATCH_OUTCOME_ADAPTER = TypeAdapter(BatchClassificationOutcome)
_ROOT_CAUSE_OUTCOME_ADAPTER = TypeAdapter(RootCauseOutcome)
_SAFE_ERROR_CLASSES: frozenset[str] = frozenset(
    {"model", "tool", "policy_reject", "infra"}
)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SpawnAgentArgs(_Args):
    """spawn_agent 仅允许服务端托管的角色与任务摘要。"""

    role: RoleName
    task_brief: str = Field(min_length=1, max_length=4000)


class WaitAgentArgs(_Args):
    """wait_agent 等待指定 child 终态，超时不取消 child。"""

    child_id: str = Field(min_length=1, max_length=64)
    timeout_ms: int = Field(default=30000, ge=0, le=30000)


class SendInputArgs(_Args):
    """send_input 向本会话内 RUNNING 子 Agent 追加一条用户消息。"""

    child_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class ListAgentsArgs(_Args):
    """list_agents 无模型可控参数，仅列出当前会话子 Agent。"""


class CloseAgentArgs(_Args):
    """close_agent 关闭本会话内一个 child。"""

    child_id: str = Field(min_length=1, max_length=64)


class ClassifyDocumentsArgs(_Args):
    """classify_documents 批量文档分类工作流参数。"""

    documents: list[ClassificationDocument] = Field(min_length=2, max_length=50)
    allowed_categories: list[str] = Field(default_factory=list, max_length=50)


class InvestigateRootCauseArgs(_Args):
    """investigate_root_cause 多分支根因排查工作流参数。"""

    incident_context: str = Field(min_length=1, max_length=8000)
    branches: list[RootCauseBranch] | None = Field(default=None, min_length=2, max_length=10)


def _inline_json_schema_defs(parameters: dict[str, Any]) -> dict[str, Any]:
    """
    把 Pydantic $defs 内联进 parameters，便于 OpenAI 工具 schema 自包含。

    Args:
        parameters: model_json_schema 产出的 parameters 字典。

    Returns:
        去掉顶层 title/$defs 且已内联引用的 parameters。
    """
    parameters.pop("title", None)
    defs = parameters.pop("$defs", None)
    if not defs:
        return parameters

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                def_name = ref.removeprefix("#/$defs/")
                if def_name in defs:
                    return _resolve(defs[def_name])
            return {key: _resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        return node

    return cast(dict[str, Any], _resolve(parameters))


def _safe_receipt_text(receipt: ChildReceipt) -> str:
    """
    把 child 回执格式化为模型可读文本，仅含安全字段。

    Args:
        receipt: SpawnManager 返回的不可变回执。

    Returns:
        不含 model、预算、工具白名单、artifacts 或 trace 的文本行。
    """
    lines = [
        f"child_id: {receipt.child_id}",
        f"role: {receipt.role}",
        f"status: {receipt.status}",
        f"task_brief: {receipt.task_brief}",
    ]
    if receipt.result_summary is not None:
        lines.append(f"result_summary: {receipt.result_summary}")
    lines.append(f"created_at: {receipt.created_at.isoformat()}")
    lines.append(f"status_changed_at: {receipt.status_changed_at.isoformat()}")
    return "\n".join(lines)


def spawn_tool_schemas() -> list[dict[str, Any]]:
    """
    返回根 Agent 可用的 Spawn 工具 JSON Schema。

    Returns:
        OpenAI 兼容的七个严格函数工具定义。
    """
    spawn_parameters = SpawnAgentArgs.model_json_schema()
    spawn_parameters.pop("title", None)
    spawn_defs = spawn_parameters.pop("$defs", None)
    if spawn_defs and "RoleName" in spawn_defs:
        spawn_parameters["properties"]["role"] = spawn_defs["RoleName"]
    wait_parameters = _inline_json_schema_defs(WaitAgentArgs.model_json_schema())
    send_input_parameters = _inline_json_schema_defs(SendInputArgs.model_json_schema())
    list_parameters = _inline_json_schema_defs(ListAgentsArgs.model_json_schema())
    close_parameters = _inline_json_schema_defs(CloseAgentArgs.model_json_schema())
    classify_parameters = _inline_json_schema_defs(ClassifyDocumentsArgs.model_json_schema())
    root_cause_parameters = _inline_json_schema_defs(
        InvestigateRootCauseArgs.model_json_schema()
    )
    return [
        {
            "type": "function",
            "function": {
                "name": "spawn_agent",
                "description": (
                    f"[{SPAWN_TOOL_SCHEMA_VERSION}] 创建服务端受控的只读子 Agent。"
                    "仅指定角色与任务摘要；不得指定模型、工具或预算。"
                ),
                "parameters": spawn_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "wait_agent",
                "description": (
                    f"[{SPAWN_TOOL_SCHEMA_VERSION}] 等待子 Agent 到达终态；"
                    "超时不取消 child，可稍后再次 wait。"
                ),
                "parameters": wait_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "send_input",
                "description": (
                    f"[{SPAWN_TOOL_SCHEMA_VERSION}] 向本会话内 RUNNING 子 Agent "
                    "追加一条用户补充消息；终态 child 不可重开。"
                ),
                "parameters": send_input_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_agents",
                "description": (
                    f"[{SPAWN_TOOL_SCHEMA_VERSION}] 列出当前会话全部子 Agent 回执快照。"
                ),
                "parameters": list_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "close_agent",
                "description": (
                    f"[{SPAWN_TOOL_SCHEMA_VERSION}] 关闭本会话内一个子 Agent 并释放并发槽。"
                ),
                "parameters": close_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "classify_documents",
                "description": (
                    f"[{SPAWN_TOOL_SCHEMA_VERSION}] 批量文档分类（至少 2 份）；"
                    "服务端负责分波并发、结果解析、复核与清理，仅返回只读建议。"
                ),
                "parameters": classify_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "investigate_root_cause",
                "description": (
                    f"[{SPAWN_TOOL_SCHEMA_VERSION}] 多分支根因排查（至少 2 个分支）；"
                    "服务端负责并行调查、复核与清理，仅返回只读建议。"
                ),
                "parameters": root_cause_parameters,
            },
        },
    ]


async def _require_session_child(
    manager: SpawnManager,
    session_id: int,
    child_id: str,
) -> None:
    """
    确认 child_id 属于当前会话。

    Args:
        manager: Spawn 运行时。
        session_id: 根会话 ID。
        child_id: 不透明 child 标识。

    Raises:
        ChildNotFoundError: child 不在当前会话回执列表中。
    """
    receipts = await manager.list_agents(session_id)
    if child_id not in {item.child_id for item in receipts}:
        raise ChildNotFoundError(child_id)


def _spawn_rejected_message(exc: SpawnRejectedError) -> str:
    """把 Spawn 拒绝原因格式化为模型可理解的固定分类。"""
    if exc.limit_name is not None:
        return f"子 Agent 创建被拒绝: {exc.limit_name}"
    return f"子 Agent 创建被拒绝: {exc.reason}"


def build_spawn_tool_dispatcher(
    manager: SpawnManager,
    session_id: int,
) -> ToolDispatcher:
    """
    绑定 SpawnManager 与可信 session_id，生成根 Agent Spawn 工具调度器。

    Args:
        manager: 进程内 Spawn 运行时。
        session_id: 根会话 ID，不允许由模型覆盖。

    Returns:
        仅处理 SPAWN_TOOL_NAMES 内工具的异步调度函数。
    """

    async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in SPAWN_TOOL_NAMES:
            return ToolResult(control="rejected", content=f"未知 Spawn 工具：{name}")
        try:
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
                wait_args = WaitAgentArgs.model_validate(arguments)
                await _require_session_child(manager, session_id, wait_args.child_id)
                receipt = await manager.wait_agent(
                    wait_args.child_id,
                    timeout_ms=wait_args.timeout_ms,
                )
                return ToolResult(control="ok", content=_safe_receipt_text(receipt))
            if name == "send_input":
                send_args = SendInputArgs.model_validate(arguments)
                await _require_session_child(manager, session_id, send_args.child_id)
                receipt = await manager.send_input(
                    send_args.child_id,
                    send_args.message,
                )
                return ToolResult(control="ok", content=_safe_receipt_text(receipt))
            if name == "classify_documents":
                classify_args = ClassifyDocumentsArgs.model_validate(arguments)
                batch_outcome = await classify_documents(
                    manager,
                    session_id=session_id,
                    documents=classify_args.documents,
                    allowed_categories=classify_args.allowed_categories,
                )
                return ToolResult(
                    control="ok",
                    content=_BATCH_OUTCOME_ADAPTER.dump_json(batch_outcome).decode("utf-8"),
                )
            if name == "investigate_root_cause":
                root_cause_args = InvestigateRootCauseArgs.model_validate(arguments)
                branches = (
                    tuple(root_cause_args.branches)
                    if root_cause_args.branches is not None
                    else DEFAULT_ROOT_CAUSE_BRANCHES
                )
                root_cause_outcome = await investigate_root_cause(
                    manager,
                    session_id=session_id,
                    incident_context=root_cause_args.incident_context,
                    branches=branches,
                )
                return ToolResult(
                    control="ok",
                    content=_ROOT_CAUSE_OUTCOME_ADAPTER.dump_json(root_cause_outcome).decode(
                        "utf-8"
                    ),
                )
            if name == "list_agents":
                ListAgentsArgs.model_validate(arguments)
                receipts = await manager.list_agents(session_id)
                return ToolResult(
                    control="ok",
                    content="\n".join(_safe_receipt_text(item) for item in receipts)
                    or "当前会话没有子 Agent",
                )
            close_args = CloseAgentArgs.model_validate(arguments)
            await _require_session_child(manager, session_id, close_args.child_id)
            receipt = await manager.close_agent(close_args.child_id)
            return ToolResult(control="ok", content=_safe_receipt_text(receipt))
        except ValidationError as exc:
            return ToolResult(
                control="clarification",
                content=validation_reason_for_tool(name, exc),
            )
        except SpawnRejectedError as exc:
            return ToolResult(control="rejected", content=_spawn_rejected_message(exc))
        except ValueError:
            return ToolResult(
                control="clarification",
                content=_WORKFLOW_VALIDATION_REASON,
            )
        except ChildNotFoundError:
            return ToolResult(
                control="failed",
                content=f"工具 {name!r} 执行失败: ChildNotFoundError",
            )
        except ChildWaitTimeoutError:
            return ToolResult(
                control="failed",
                content=f"工具 {name!r} 执行失败: ChildWaitTimeoutError",
            )
        except ChildRuntimeUnavailableError as exc:
            safe_reason = (
                exc.reason if exc.reason in _SAFE_ERROR_CLASSES else "infra"
            )
            return ToolResult(
                control="failed",
                content=f"工具 {name!r} 执行失败: {safe_reason}",
            )
        except Exception as exc:
            return ToolResult(
                control="failed",
                content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
            )

    return dispatch
