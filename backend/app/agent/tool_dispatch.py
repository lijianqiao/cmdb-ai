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
5. 每次调用都按绑定的可信用户现查业务权限（permissions.tool_denial）：角色白名单只说明
   「这个角色可以用哪些工具」，替代不了登录用户的授权；用户 ID 只来自可信上下文，
   绝不从模型参数里取。
6. 校验通过后才转发；参数问题要求澄清，意外异常只返回类型。
7. 不在等工具时占连接（R5）：调度器拿会话工厂时，查权限、每个查库工具各用一个短会话，
   用完即还；需要调向量模型的工具自己分段，调模型时不持有会话。
"""

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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
from app.agent.permissions import (
    MONITOR_LOG_READ,
    PermissionContext,
    load_permission_context,
    tool_denial,
)
from app.agent.roles import ToolName

# 参数模型定义在叶子模块 tool_args 里（见该模块 docstring：打破本模块与
# hitl_gate 的循环 import）。这里原样再导出，既有调用方与测试的导入路径不变。
from app.agent.tool_args import (
    DeviceControlArgs,
    GetDeviceQueryResultArgs,
    KbGlobArgs,
    KbGrepArgs,
    KbReadArgs,
    KbSemanticSearchArgs,
    ListDeviceCommandsArgs,
    NotifyArgs,
    NotifyPayloadArgs,
    QueryCmdbArgs,
    QueryCmdbDependenciesArgs,
    QueryDeviceCommandArgs,
    QueryMonitorStatusArgs,
    _Args,
    validate_and_run,
    validation_reason_for_tool,
)
from app.core.database import SessionSource, session_scope

__all__ = [
    "ROOT_TOOL_SCHEMA_VERSION",
    "TOOL_SCHEMA_VERSION",
    "DeviceControlArgs",
    "GetDeviceQueryResultArgs",
    "KbGlobArgs",
    "KbGrepArgs",
    "KbReadArgs",
    "KbSemanticSearchArgs",
    "ListDeviceCommandsArgs",
    "NotifyArgs",
    "NotifyPayloadArgs",
    "QueryCmdbArgs",
    "QueryCmdbDependenciesArgs",
    "QueryDeviceCommandArgs",
    "QueryMonitorStatusArgs",
    "build_root_tool_dispatcher",
    "build_tool_dispatcher",
    "root_tool_schemas",
    "tool_schemas_for",
    "validate_and_run",
    "validation_reason_for_tool",
]

TOOL_SCHEMA_VERSION = "t09-v1"
ROOT_TOOL_SCHEMA_VERSION = "t11-v1"


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
    "query_cmdb": "按资产 ID、IP、业务系统或主机名读取 CMDB 资产（四选一）。",
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
    db: SessionSource,
    name: ToolName,
    parsed: _Args,
    context: PermissionContext,
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
        async with session_scope(db) as session:
            return await query_cmdb(
                session,
                asset_ids=parsed.asset_ids,
                ip=parsed.ip,
                business_system=parsed.business_system,
                hostname=parsed.hostname,
            )
    if isinstance(parsed, QueryCmdbDependenciesArgs):
        async with session_scope(db) as session:
            return await query_cmdb_dependencies(
                session,
                parsed.asset_id,
                direction=parsed.direction,
                max_depth=parsed.max_depth,
            )
    if isinstance(parsed, QueryMonitorStatusArgs):
        async with session_scope(db) as session:
            return await query_monitor_status(
                session,
                target_ids=parsed.target_ids,
                ip_prefix=parsed.ip_prefix,
                since_limit=parsed.since_limit,
                # 探测历史与 REST 事件接口一样要 monitor_log:read，缺它只给当前状态
                include_history=context.has(MONITOR_LOG_READ),
            )
    return ToolResult(control="failed", content=f"工具 {name!r} 参数模型未绑定执行器")


def build_tool_dispatcher(
    db: SessionSource,
    allowlist: Iterable[str],
    *,
    user_id: int | None,
) -> ToolDispatcher:
    """Bind a session source, immutable role allowlist and trusted user into a dispatcher.

    ``user_id`` is the account whose business permissions cap every call (the
    session owner). It must come from the trusted request context, never from
    model arguments; ``None`` means no usable account and rejects everything.
    """
    allowed = frozenset(allowlist)

    async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in allowed:
            return ToolResult(control="rejected", content=f"工具 {name!r} 不在角色白名单")
        if name not in _ARGUMENT_MODELS:
            return ToolResult(control="rejected", content=f"未知工具 {name!r}")
        async with session_scope(db) as session:
            context = await load_permission_context(session, user_id)
        denial = tool_denial(context, name)
        if denial is not None:
            return ToolResult(control="rejected", content=denial)
        assert context is not None  # 账号不可用时 tool_denial 一定给出拒绝原因
        tool_name: ToolName = name
        permitted: PermissionContext = context
        return await validate_and_run(
            name,
            arguments,
            _ARGUMENT_MODELS[tool_name],
            lambda parsed: _dispatch_validated(db, tool_name, parsed, permitted),
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

# 只有根 Agent 才有的工具（执行类薄工具与设备命令辅助工具）
_ROOT_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "notify",
        "device_control",
        "query_device_command",
        "list_device_commands",
        "get_device_query_result",
    }
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
                    "（reboot/port_enable/port_disable）。是否当场执行取决于当前会话"
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
    db: SessionSource,
    *,
    session_id: int,
    actor_user_id: int,
    proposed_by_agent_id: str | None = None,
    publisher: HitlEventPublisher | None = None,
    gate_hook: HitlGateHook | None = None,
) -> ToolDispatcher:
    """创建绑定可信身份的根 Agent 工具调度器。

    Args:
        db: 会话工厂（每次调用各用短会话）或借用的会话。
        session_id: 根 Agent 会话 ID，不允许由模型覆盖。
        actor_user_id: 当前认证用户 ID，不允许由模型覆盖。
        proposed_by_agent_id: 发起提案的 Agent ID，可为空。
        publisher: 可选的 HITL 安全事件发布器。
        gate_hook: 可选门控钩子，薄工具从中读取当前提案 ID。

    Returns:
        可调用只读与执行类工具的调度函数。
    """
    read_dispatch = build_tool_dispatcher(db, _ROOT_READ_ONLY_TOOLS, user_id=actor_user_id)

    async def run_root_only(
        session: AsyncSession, name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        if name == "notify":
            return await validate_and_run(
                name,
                arguments,
                NotifyArgs,
                lambda args: notify(
                    session,
                    asset_id=args.asset_id,
                    payload=args.payload.model_dump(),
                    reason=args.reason,
                    session_id=session_id,
                    actor_user_id=actor_user_id,
                    proposed_by_agent_id=proposed_by_agent_id,
                    publisher=publisher,
                    gate_hook=gate_hook,
                ),
            )
        if name == "device_control":
            return await validate_and_run(
                name,
                arguments,
                DeviceControlArgs,
                lambda args: device_control(
                    session,
                    asset_id=args.asset_id,
                    command_name=args.command_name,
                    interface_name=args.interface_name,
                    reason=args.reason,
                    session_id=session_id,
                    actor_user_id=actor_user_id,
                    proposed_by_agent_id=proposed_by_agent_id,
                    publisher=publisher,
                    gate_hook=gate_hook,
                ),
            )
        if name == "query_device_command":
            return await validate_and_run(
                name,
                arguments,
                QueryDeviceCommandArgs,
                lambda args: query_device_command(
                    session,
                    asset_id=args.asset_id,
                    command_name=args.command_name,
                    reason=args.reason,
                    session_id=session_id,
                    actor_user_id=actor_user_id,
                    proposed_by_agent_id=proposed_by_agent_id,
                    publisher=publisher,
                    gate_hook=gate_hook,
                ),
            )
        if name == "list_device_commands":
            return await validate_and_run(
                name,
                arguments,
                ListDeviceCommandsArgs,
                lambda args: list_device_commands_for_asset(
                    session, session_id=session_id, asset_id=args.asset_id
                ),
            )
        return await validate_and_run(
            name,
            arguments,
            GetDeviceQueryResultArgs,
            lambda args: get_device_query_result(
                session, session_id=session_id, proposal_id=args.proposal_id
            ),
        )

    async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        # 只读工具由 read_dispatch 自己查权限、自己开短会话
        if name not in _ROOT_ONLY_TOOLS:
            return await read_dispatch(name, arguments)
        # 根专属工具都是很短的库操作（门控工具在这里只会失败关闭，真执行在门控钩子里），
        # 查权限和执行共用一个短会话
        async with session_scope(db) as session:
            denial = tool_denial(await load_permission_context(session, actor_user_id), name)
            if denial is not None:
                return ToolResult(control="rejected", content=denial)
            return await run_root_only(session, name, arguments)

    return dispatch
