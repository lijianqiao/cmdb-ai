"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: tool_dispatch.py
@DateTime: 2026-08-12 11:26
@Docs: 为子 Agent 只读工具和根 Agent HITL 工具提供严格、安全的调度。

实现流程：
1. 为每个既有工具签名定义严格参数模型，并生成提供给模型的 JSON Schema。
2. 将角色持久化的工具白名单冻结到调度闭包中，调用时再次检查权限。
3. 子调度器始终只认识七个只读工具，不接受任何角色白名单扩展写权限。
4. 根调度器额外绑定可信会话身份，并单独开放 propose_remediation。
5. 校验通过后才转发；参数问题要求澄清，意外异常只返回类型。
"""

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.device_commands import CommandName
from app.agent.hitl import HitlEventPublisher
from app.agent.hitl_tools import (
    get_device_query_result,
    list_device_commands_for_asset,
    propose_device_control,
    propose_remediation,
    query_device_command,
)
from app.agent.knowledge_tools import kb_glob, kb_grep, kb_read, kb_semantic_search
from app.agent.loop import ToolDispatcher, ToolResult
from app.agent.ops_tools import query_cmdb, query_cmdb_dependencies, query_monitor_status
from app.agent.roles import ToolName

TOOL_SCHEMA_VERSION = "t09-v1"
ROOT_TOOL_SCHEMA_VERSION = "t10-v1"


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


class ProposeRemediationArgs(_Args):
    """根 Agent 整改提案的模型可控参数。"""

    asset_id: int = Field(ge=1)
    action_type: Literal["notify"]
    payload: dict[str, object]
    reason: str = Field(min_length=1, max_length=2000)


class ProposeDeviceControlArgs(_Args):
    """根 Agent 设备管控提案的模型可控参数。"""

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


def _validation_reason(name: str, exc: ValidationError) -> str:
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
                content=_validation_reason(name, exc),
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


def root_tool_schemas() -> list[dict[str, Any]]:
    """返回根 Agent 的七个只读工具、整改提案工具和四个设备命令工具 Schema。

    Returns:
        OpenAI 兼容的十二个严格函数工具定义。
    """
    propose_parameters = deepcopy(ProposeRemediationArgs.model_json_schema())
    propose_parameters.pop("title", None)
    query_parameters = deepcopy(QueryDeviceCommandArgs.model_json_schema())
    query_parameters.pop("title", None)
    # command_name: CommandName 是具名 type 别名，Pydantic 会生成 $defs + $ref
    # 间接引用；内联展开成跟 action_type 一样的直接 enum，不依赖模型端点
    # 是否正确解析 $ref。
    query_parameters["properties"]["command_name"] = query_parameters.pop("$defs")["CommandName"]
    propose_control_parameters = deepcopy(ProposeDeviceControlArgs.model_json_schema())
    propose_control_parameters.pop("title", None)
    propose_control_parameters["properties"]["command_name"] = propose_control_parameters.pop(
        "$defs"
    )["CommandName"]
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
                    "审批策略（白名单/黑名单/需人工审批）与凭据前提。"
                    "不确定命令名或是否需要审批时先调用这个工具。"
                ),
                "parameters": list_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_remediation",
                "description": (
                    f"[{ROOT_TOOL_SCHEMA_VERSION}] 为指定资产创建需人工审批的整改提案。"
                ),
                "parameters": propose_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_device_command",
                "description": (
                    f"[{ROOT_TOOL_SCHEMA_VERSION}] 对已配置凭据的资产发起只读诊断命令查询"
                    "（白名单自动执行，否则需要人工审批）。command_name 必须是 show_version"
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
                "name": "propose_device_control",
                "description": (
                    f"[{ROOT_TOOL_SCHEMA_VERSION}] 对已配置凭据的资产发起会改变设备状态的命令"
                    "（reboot/shutdown/port_enable/port_disable）。白名单命中且资产非动态凭据时"
                    "会当场执行，否则进入人工审批。port_enable/port_disable 必须提供 interface_name。"
                    "不确定这台设备支持哪些变更类命令时先调用 list_device_commands。"
                ),
                "parameters": propose_control_parameters,
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
) -> ToolDispatcher:
    """创建绑定可信身份的根 Agent 工具调度器。

    Args:
        db: 当前事务使用的异步数据库会话。
        session_id: 根 Agent 会话 ID，不允许由模型覆盖。
        actor_user_id: 当前认证用户 ID，不允许由模型覆盖。
        proposed_by_agent_id: 发起提案的 Agent ID，可为空。
        publisher: 可选的 HITL 安全事件发布器。

    Returns:
        可调用七个只读工具、整改提案工具和四个设备命令工具的调度函数。
    """
    read_dispatch = build_tool_dispatcher(db, _ROOT_READ_ONLY_TOOLS)

    async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "list_device_commands":
            try:
                list_args = ListDeviceCommandsArgs.model_validate(arguments)
            except ValidationError as exc:
                return ToolResult(
                    control="clarification",
                    content=_validation_reason(name, exc),
                )
            try:
                return await list_device_commands_for_asset(db, asset_id=list_args.asset_id)
            except Exception as exc:
                return ToolResult(
                    control="failed",
                    content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
                )
        if name == "query_device_command":
            try:
                query_args = QueryDeviceCommandArgs.model_validate(arguments)
            except ValidationError as exc:
                return ToolResult(
                    control="clarification",
                    content=_validation_reason(name, exc),
                )
            try:
                return await query_device_command(
                    db,
                    session_id=session_id,
                    actor_user_id=actor_user_id,
                    proposed_by_agent_id=proposed_by_agent_id,
                    asset_id=query_args.asset_id,
                    command_name=query_args.command_name,
                    reason=query_args.reason,
                    publisher=publisher,
                )
            except Exception as exc:
                return ToolResult(
                    control="failed",
                    content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
                )
        if name == "propose_device_control":
            try:
                control_args = ProposeDeviceControlArgs.model_validate(arguments)
            except ValidationError as exc:
                return ToolResult(
                    control="clarification",
                    content=_validation_reason(name, exc),
                )
            try:
                return await propose_device_control(
                    db,
                    session_id=session_id,
                    actor_user_id=actor_user_id,
                    proposed_by_agent_id=proposed_by_agent_id,
                    asset_id=control_args.asset_id,
                    command_name=control_args.command_name,
                    interface_name=control_args.interface_name,
                    reason=control_args.reason,
                    publisher=publisher,
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
                    content=_validation_reason(name, exc),
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
        if name != "propose_remediation":
            return await read_dispatch(name, arguments)
        try:
            remediation_args = ProposeRemediationArgs.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                control="clarification",
                content=_validation_reason(name, exc),
            )
        try:
            return await propose_remediation(
                db,
                session_id=session_id,
                actor_user_id=actor_user_id,
                proposed_by_agent_id=proposed_by_agent_id,
                asset_id=remediation_args.asset_id,
                action_type=remediation_args.action_type,
                payload=remediation_args.payload,
                reason=remediation_args.reason,
                publisher=publisher,
            )
        except Exception as exc:
            return ToolResult(
                control="failed",
                content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
            )

    return dispatch
