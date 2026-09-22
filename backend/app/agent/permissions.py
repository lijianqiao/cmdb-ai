"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: permissions.py
@DateTime: 2026-09-22
@Docs: Agent 的权限边界：自动执行授权与执行前复核。

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
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.user import user_crud

AGENT_USE = "agent:use"
HITL_APPROVE = "agent:hitl_approve"
AUTO_EXECUTE = "agent:auto_execute"


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
