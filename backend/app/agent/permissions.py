"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: permissions.py
@DateTime: 2026-09-22
@Docs: Agent 的权限边界：工具与业务权限的对应、自动执行授权与执行前复核。

实现流程：
1. 「使用运维助手」「人工审批」「允许自动执行」是三种不同的责任，各有独立权限码。
   会话里保存的审批档位（ask / assist / full）只是用户的选择，不是授权——
   能不能由档位自动批准，每次都按当时账号的权限现查，而不是信任数据库里存的档位。
2. load_permission_context 一次拿到账号状态与权限集合。账号不存在、已删除或已停用时
   返回 None，调用方一律按「没有任何权限」处理（失败关闭）；超管沿用既有规则，
   视为持有全部权限。
3. effective_approval_mode：会话存的是 assist/full，但账号没有 agent:auto_execute 时
   按 ask 处理。门控是否自动批准、给模型看的命令策略句都用它，保证模型听到的
   与门控实际做的一致。
4. TOOL_PERMISSIONS：Agent 工具读的是哪类业务数据，就要求页面/REST 读同类数据时的
   同一个权限（agent:use 只代表能用对话入口，不隐含任何业务权限）。根 Agent、HITL 门控、
   子 Agent 都在真正执行工具的地方逐次调用 tool_denial 现查，撤销立即生效；
   给模型的工具清单也按它过滤，但那只是体验，真正的阻断在执行边界。
5. 已经写进会话历史的工具结果（当时有权限时查到的）不会因为后来撤销权限而消失，
   会话所有者仍能在聊天记录里看到；撤销只挡住之后的新调用和完整结果接口。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.user import user_crud

AGENT_USE = "agent:use"
HITL_APPROVE = "agent:hitl_approve"
AUTO_EXECUTE = "agent:auto_execute"
KNOWLEDGE_READ = "knowledge:read"
CMDB_READ = "cmdb:read"
MONITOR_READ = "monitor:read"
MONITOR_LOG_READ = "monitor_log:read"

# 每个工具在 agent:use 之外要求的业务权限；None 表示只需要 agent:use。
# 编排工具本身不读业务数据，它派出的子 Agent 每调一个工具都会各自再查权限。
# 设备相关工具：系统没有按资产的访问控制，按「能看 CMDB 资产」这一层授权。
# 没登记的工具一律拒绝——新增工具时必须在这里写清它读的是哪类数据。
TOOL_PERMISSIONS: Mapping[str, str | None] = MappingProxyType(
    {
        "kb_glob": KNOWLEDGE_READ,
        "kb_grep": KNOWLEDGE_READ,
        "kb_read": KNOWLEDGE_READ,
        "kb_semantic_search": KNOWLEDGE_READ,
        "query_cmdb": CMDB_READ,
        "query_cmdb_dependencies": CMDB_READ,
        "query_monitor_status": MONITOR_READ,
        "list_device_commands": CMDB_READ,
        "query_device_command": CMDB_READ,
        "device_control": CMDB_READ,
        "get_device_query_result": CMDB_READ,
        "notify": CMDB_READ,
        "classify_documents": None,
        "investigate_root_cause": None,
    }
)


@dataclass(frozen=True, slots=True)
class PermissionContext:
    """某一时刻账号的可用状态与权限集合。"""

    user_id: int
    is_superuser: bool
    codes: frozenset[str]

    def has(self, code: str) -> bool:
        """是否持有某个权限；超管视为持有全部权限（与 require_permission 一致）。"""
        return self.is_superuser or code in self.codes


async def load_permission_context(
    db: AsyncSession,
    user_id: int | None,
) -> PermissionContext | None:
    """读取账号当前状态与权限。

    Args:
        db: 数据库会话。
        user_id: 待查账号 ID；None 表示没有可信的发起人。

    Returns:
        可用账号的权限上下文；账号不存在、已删除或已停用时返回 None。
    """
    if user_id is None:
        return None
    user = await user_crud.get(db, user_id)
    if user is None or not user.is_active:
        return None
    if user.is_superuser:
        return PermissionContext(user_id=user.id, is_superuser=True, codes=frozenset())
    codes = await user_crud.get_permission_codes(db, user.id)
    return PermissionContext(user_id=user.id, is_superuser=False, codes=frozenset(codes))


async def effective_approval_mode(
    db: AsyncSession,
    *,
    approval_mode: str,
    user_id: int | None,
) -> str:
    """会话实际生效的审批档位。

    Args:
        db: 数据库会话。
        approval_mode: 会话里保存的档位。
        user_id: 自动批准会记到谁名下（会话所有者 / 发起人）。

    Returns:
        没有自动执行权限（或账号不可用）时返回 "ask"，否则原样返回保存的档位。
    """
    if approval_mode == "ask":
        return "ask"
    context = await load_permission_context(db, user_id)
    if context is None or not context.has(AUTO_EXECUTE):
        return "ask"
    return approval_mode


def tool_denial(context: PermissionContext | None, tool_name: str) -> str | None:
    """判断当前账号能否调用某个工具。

    Args:
        context: load_permission_context 的结果；None 表示账号不可用。
        tool_name: 工具名。

    Returns:
        拒绝原因（直接作为工具结果回给模型，由它转述给用户）；None 表示放行。
    """
    if context is None:
        return "当前账号不可用（不存在、已删除或已停用），不能调用工具"
    if tool_name not in TOOL_PERMISSIONS:
        return f"工具 {tool_name} 未登记权限要求，已拒绝"
    required = (AGENT_USE, TOOL_PERMISSIONS[tool_name])
    missing = [code for code in required if code is not None and not context.has(code)]
    if missing:
        return f"当前账号无权使用工具 {tool_name}（需要权限：{'、'.join(missing)}）"
    return None


def permitted_tool_schemas(
    context: PermissionContext | None,
    schemas: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """只保留当前账号能调用的工具定义，交给模型。"""
    return [
        schema
        for schema in schemas
        if tool_denial(context, str(schema["function"]["name"])) is None
    ]
