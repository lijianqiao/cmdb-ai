"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: tool_dispatch.py
@DateTime: 2026-08-12 11:26
@Docs: 为子 Agent 只读工具和根 Agent 执行类工具提供严格、安全的调度。

实现流程：
1. 为每个既有工具签名定义严格参数模型，并生成提供给模型的 JSON Schema。
2. 将角色持久化的工具白名单冻结到调度闭包中，调用时再次检查权限。
3. 子调度器始终只认识七个只读工具，不接受任何角色白名单扩展写权限。
4. 根调度器绑定可信会话身份；HITL 门控在 run_loop before 钩子，薄工具只真执行。
5. 校验通过后才转发；参数问题要求澄清，意外异常只返回类型。
"""

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.device_commands import CommandName
from app.agent.hitl import HitlEventPublisher
from app.agent.hitl_gate import HitlGateHook
from app.agent.hitl_tools import (
    device_control,
    get_device_query_result,
    list_device_commands_for_asset,
    notify,
    query_device_command,
)
from app.agent.knowledge_tools import kb_glob, kb_grep, kb_read, kb_semantic_search
from app.agent.loop import ToolDispatcher, ToolResult
from app.agent.ops_tools import query_cmdb, query_cmdb_dependencies, query_monitor_status
from app.agent.roles import ToolName

TOOL_SCHEMA_VERSION = "t09-v1"
ROOT_TOOL_SCHEMA_VERSION = "t11-v1"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class KbGlobArgs(_Args):
    pattern: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=100)


class KbGrepArgs(_Args):
    pattern: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=100)
    context_lines: int = Field(default=0, ge=0, le=20)


class KbReadArgs(_Args):
    path: str = Field(min_length=1, max_length=500)
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=4000, ge=1, le=32_000)


class KbSemanticSearchArgs(_Args):
    query: str = Field(min_length=1, max_length=2000)
    category_id: int | None = Field(default=None, ge=1)
    top_k: int = Field(default=5, ge=1, le=20)


class QueryCmdbArgs(_Args):
    asset_ids: list[int] | None = Field(default=None, max_length=100)
    ip: str | None = Field(default=None, min_length=1, max_length=45)
    business_system: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def at_most_one_filter(self) -> QueryCmdbArgs:
        selected = sum(
            value is not None
            for value in (self.asset_ids, self.ip, self.business_system)
        )
        if selected > 1:
            raise ValueError("asset_ids, ip, business_system 最多提供一个")
        return self


class QueryCmdbDependenciesArgs(_Args):
    asset_id: int = Field(ge=1)
    direction: Literal["up", "down"] = "down"
    max_depth: int = Field(default=3, ge=1, le=5)


class QueryMonitorStatusArgs(_Args):
    target_ids: list[int] | None = Field(default=None, max_length=100)
    ip_prefix: str | None = Field(default=None, min_length=1, max_length=45)
    since_limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def at_most_one_filter(self) -> QueryMonitorStatusArgs:
        if self.target_ids is not None and self.ip_prefix is not None:
            raise ValueError("target_ids 与 ip_prefix 最多提供一个")
        return self


class NotifyPayloadArgs(_Args):
    """notify 工具 payload 内的 message 字段。"""

    message: str = Field(min_length=1, max_length=2000)


class NotifyArgs(_Args):
    """根 Agent 通知工具的模型可控参数。"""

    asset_id: int = Field(ge=1)
    payload: NotifyPayloadArgs
    reason: str = Field(min_length=1, max_length=2000)


class DeviceControlArgs(_Args):
    """根 Agent 设备管控工具的模型可控参数。"""

    asset_id: int = Field(ge=1)
    command_name: CommandName
    interface_name: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)


class QueryDeviceCommandArgs(_Args):
    asset_id: int = Field(ge=1)
    command_name: CommandName
    reason: str = Field(min_length=1, max_length=2000)


class GetDeviceQueryResultArgs(_Args):
    proposal_id: int = Field(ge=1)


class ListDeviceCommandsArgs(_Args):
    asset_id: int = Field(ge=1)


def validation_reason_for_tool(name: str, exc: ValidationError) -> str:
    """把校验错误变成模型可自我纠正的提示：字段名 + 期望约束，不回显输入值。"""
    details: list[str] = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(part) for part in error.get("loc", ())) or "(根)"
        details.append(f"{loc}: {error.get('msg', 'invalid')}")
    joined = "; ".join(dict.fromkeys(details))[:1000]
    return f"工具 {name!r} 参数无效: {joined}"


_ARGUMENT_MODELS: dict[ToolName, type[_Args]] = {
    "kb_glob": KbGlobArgs,
    "kb_grep": KbGrepArgs,
    "kb_read": KbReadArgs,
    "kb_semantic_search": KbSemanticSearchArgs,
    "query_cmdb": QueryCmdbArgs,
    "query_cmdb_dependencies": QueryCmdbDependenciesArgs,
    "query_monitor_status": QueryMonitorStatusArgs,
}

_DESCRIPTIONS: dict[ToolName, str] = {
    "kb_glob": "按 glob 列出 knowledge/ 内文档路径。",
    "kb_grep": "在 knowledge/ 授权范围内用 ripgrep 搜索正文并返回行号。",
    "kb_read": "分页读取 knowledge/ 内一个文档的正文。",
    "kb_semantic_search": "关键词不足时对知识分块做向量语义检索。",
    "query_cmdb": "按资产 ID、IP 或业务系统读取 CMDB 资产。",
    "query_cmdb_dependencies": "按方向和有限深度遍历一个资产的依赖图。",
    "query_monitor_status": "读取目标当前派生状态和有限条最近探测历史。",
}


def tool_schemas_for(allowlist: Iterable[str]) -> list[dict[str, Any]]:
    """Build stable OpenAI-compatible schemas for one role's exact allowlist."""
    schemas: list[dict[str, Any]] = []
    for name in allowlist:
        if name not in _ARGUMENT_MODELS:
            raise ValueError(f"unknown child tool definition: {name!r}")
        tool_name: ToolName = name
        parameters = deepcopy(_ARGUMENT_MODELS[tool_name].model_json_schema())
        parameters.pop("title", None)
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"[{TOOL_SCHEMA_VERSION}] {_DESCRIPTIONS[tool_name]}",
                    "parameters": parameters,
                },
            }
        )
    return schemas


async def _dispatch_validated(
    db: AsyncSession,
    name: ToolName,
    parsed: _Args,
) -> ToolResult:
    if isinstance(parsed, KbGlobArgs):
        return await kb_glob(parsed.pattern, category=parsed.category)
    if isinstance(parsed, KbGrepArgs):
        return await kb_grep(
            parsed.pattern,
            category=parsed.category,
            context_lines=parsed.context_lines,
        )
    if isinstance(parsed, KbReadArgs):
        return await kb_read(parsed.path, offset=parsed.offset, limit=parsed.limit)
    if isinstance(parsed, KbSemanticSearchArgs):
        return await kb_semantic_search(
            db,
            parsed.query,
            category_id=parsed.category_id,
            top_k=parsed.top_k,
        )
    if isinstance(parsed, QueryCmdbArgs):
        return await query_cmdb(
            db,
            asset_ids=parsed.asset_ids,
            ip=parsed.ip,
            business_system=parsed.business_system,
        )
    if isinstance(parsed, QueryCmdbDependenciesArgs):
        return await query_cmdb_dependencies(
            db,
            parsed.asset_id,
            direction=parsed.direction,
            max_depth=parsed.max_depth,
        )
    if isinstance(parsed, QueryMonitorStatusArgs):
        return await query_monitor_status(
            db,
            target_ids=parsed.target_ids,
            ip_prefix=parsed.ip_prefix,
            since_limit=parsed.since_limit,
        )
    return ToolResult(control="failed", content=f"工具 {name!r} 参数模型未绑定执行器")


def build_tool_dispatcher(
    db: AsyncSession,
    allowlist: Iterable[str],
) -> ToolDispatcher:
    """Bind one DB session and immutable role allowlist into a loop dispatcher."""
    allowed = frozenset(allowlist)

    async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in allowed:
            return ToolResult(control="rejected", content=f"工具 {name!r} 不在角色白名单")
        if name not in _ARGUMENT_MODELS:
            return ToolResult(control="rejected", content=f"未知工具 {name!r}")
        tool_name: ToolName = name
        argument_model = _ARGUMENT_MODELS[tool_name]
        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                control="clarification",
                content=validation_reason_for_tool(name, exc),
            )
        try:
            return await _dispatch_validated(db, tool_name, parsed)
        except Exception as exc:
            return ToolResult(
                control="failed",
                content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
            )

    return dispatch


_ROOT_READ_ONLY_TOOLS: tuple[ToolName, ...] = (
    "kb_glob",
    "kb_grep",
    "kb_read",
    "kb_semantic_search",
    "query_cmdb",
    "query_cmdb_dependencies",
    "query_monitor_status",
)

_ROOT_EXECUTION_TOOLS: frozenset[str] = frozenset(
    {"notify", "device_control", "query_device_command"}
)


def _inline_command_name_enum(parameters: dict[str, Any]) -> dict[str, Any]:
    """将 CommandName $ref 内联为 enum，避免模型端点 $ref 解析问题。"""
    defs = parameters.get("$defs")
    if defs and "CommandName" in defs:
        parameters = deepcopy(parameters)
        parameters["properties"]["command_name"] = defs["CommandName"]
        parameters.pop("$defs", None)
    return parameters


def root_tool_schemas() -> list[dict[str, Any]]:
    """返回根 Agent 的只读工具、执行类工具与设备命令辅助工具 Schema。

    Spawn 工具（spawn_agent 等）由 spawn_tools.spawn_tool_schemas 单独提供，
    chat_turn 在运行时与本文案合并，子 Agent 白名单不包含 Spawn/HITL/设备变更。

    Returns:
        OpenAI 兼容的十二个严格函数工具定义。
    """
    notify_parameters = deepcopy(NotifyArgs.model_json_schema())
    notify_parameters.pop("title", None)
    notify_defs = notify_parameters.pop("$defs", None)
    if notify_defs and "NotifyPayloadArgs" in notify_defs:
        notify_parameters["properties"]["payload"] = notify_defs["NotifyPayloadArgs"]
    query_parameters = _inline_command_name_enum(QueryDeviceCommandArgs.model_json_schema())
    query_parameters.pop("title", None)
    control_parameters = _inline_command_name_enum(DeviceControlArgs.model_json_schema())
    control_parameters.pop("title", None)
    result_parameters = deepcopy(GetDeviceQueryResultArgs.model_json_schema())
    result_parameters.pop("title", None)
    list_parameters = deepcopy(ListDeviceCommandsArgs.model_json_schema())
    list_parameters.pop("title", None)
    return [
        *tool_schemas_for(_ROOT_READ_ONLY_TOOLS),
        {
            "type": "function",
            "function": {
                "name": "list_device_commands",
                "description": (
                    f"[{ROOT_TOOL_SCHEMA_VERSION}] 列出一台资产支持的设备诊断命令、"
                    "审批策略与凭据前提（策略句会随当前会话审批档位变化）。"
                    "不确定命令名或是否需要审批时先调用这个工具。"
                ),
                "parameters": list_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "notify",
                "description": (
                    f"[{ROOT_TOOL_SCHEMA_VERSION}] 向指定资产关联用户发送站内通知。"
                    "是否当场执行取决于当前会话审批档位。"
                ),
                "parameters": notify_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_device_command",
                "description": (
                    f"[{ROOT_TOOL_SCHEMA_VERSION}] 对已配置凭据的资产发起只读诊断命令查询"
                    "（是否当场执行取决于当前会话审批档位，以 list_device_commands 策略句"
                    "与工具返回为准）。command_name 必须是 show_version"
                    "（版本信息）/show_running_config（当前配置）/show_interfaces（接口状态）"
                    "/ping（连通性测试）之一——这是命令目录里的语义 key，不是某个厂商的原始 "
                    "CLI 语法，真实命令字符串由平台按资产厂商自动转换。"
                ),
                "parameters": query_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "device_control",
                "description": (
                    f"[{ROOT_TOOL_SCHEMA_VERSION}] 对已配置凭据的资产发起会改变设备状态的命令"
                    "（reboot/shutdown/port_enable/port_disable）。是否当场执行取决于当前会话"
                    "审批档位，以 list_device_commands 策略句与工具返回为准。"
                    "port_enable/port_disable 必须提供 interface_name。"
                    "不确定这台设备支持哪些变更类命令时先调用 list_device_commands。"
                ),
                "parameters": control_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_device_query_result",
                "description": (
                    f"[{ROOT_TOOL_SCHEMA_VERSION}] 回查一个已提交的设备命令查询提案的当前结果。"
                ),
                "parameters": result_parameters,
            },
        },
    ]


def build_root_tool_dispatcher(
    db: AsyncSession,
    *,
    session_id: int,
    actor_user_id: int,
    proposed_by_agent_id: str | None = None,
    publisher: HitlEventPublisher | None = None,
    gate_hook: HitlGateHook | None = None,
) -> ToolDispatcher:
    """创建绑定可信身份的根 Agent 工具调度器。

    Args:
        db: 当前事务使用的异步数据库会话。
        session_id: 根 Agent 会话 ID，不允许由模型覆盖。
        actor_user_id: 当前认证用户 ID，不允许由模型覆盖。
        proposed_by_agent_id: 发起提案的 Agent ID，可为空。
        publisher: 可选的 HITL 安全事件发布器。
        gate_hook: 可选门控钩子，薄工具从中读取当前提案 ID。

    Returns:
        可调用只读与执行类工具的调度函数。
    """
    read_dispatch = build_tool_dispatcher(db, _ROOT_READ_ONLY_TOOLS)

    async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        if name in _ROOT_EXECUTION_TOOLS:
            try:
                if name == "notify":
                    notify_args = NotifyArgs.model_validate(arguments)
                    return await notify(
                        db,
                        asset_id=notify_args.asset_id,
                        payload=notify_args.payload.model_dump(),
                        reason=notify_args.reason,
                        session_id=session_id,
                        actor_user_id=actor_user_id,
                        proposed_by_agent_id=proposed_by_agent_id,
                        publisher=publisher,
                        gate_hook=gate_hook,
                    )
                if name == "device_control":
                    control_args = DeviceControlArgs.model_validate(arguments)
                    return await device_control(
                        db,
                        asset_id=control_args.asset_id,
                        command_name=control_args.command_name,
                        interface_name=control_args.interface_name,
                        reason=control_args.reason,
                        session_id=session_id,
                        actor_user_id=actor_user_id,
                        proposed_by_agent_id=proposed_by_agent_id,
                        publisher=publisher,
                        gate_hook=gate_hook,
                    )
                query_args = QueryDeviceCommandArgs.model_validate(arguments)
                return await query_device_command(
                    db,
                    asset_id=query_args.asset_id,
                    command_name=query_args.command_name,
                    reason=query_args.reason,
                    session_id=session_id,
                    actor_user_id=actor_user_id,
                    proposed_by_agent_id=proposed_by_agent_id,
                    publisher=publisher,
                    gate_hook=gate_hook,
                )
            except ValidationError as exc:
                return ToolResult(
                    control="clarification",
                    content=validation_reason_for_tool(name, exc),
                )
            except Exception as exc:
                return ToolResult(
                    control="failed",
                    content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
                )

        if name == "list_device_commands":
            try:
                list_args = ListDeviceCommandsArgs.model_validate(arguments)
            except ValidationError as exc:
                return ToolResult(
                    control="clarification",
                    content=validation_reason_for_tool(name, exc),
                )
            try:
                return await list_device_commands_for_asset(
                    db, session_id=session_id, asset_id=list_args.asset_id
                )
            except Exception as exc:
                return ToolResult(
                    control="failed",
                    content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
                )
        if name == "get_device_query_result":
            try:
                result_args = GetDeviceQueryResultArgs.model_validate(arguments)
            except ValidationError as exc:
                return ToolResult(
                    control="clarification",
                    content=validation_reason_for_tool(name, exc),
                )
            try:
                return await get_device_query_result(
                    db,
                    session_id=session_id,
                    proposal_id=result_args.proposal_id,
                )
            except Exception as exc:
                return ToolResult(
                    control="failed",
                    content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
                )
        return await read_dispatch(name, arguments)

    return dispatch
