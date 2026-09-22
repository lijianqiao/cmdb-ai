"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_tool_permissions.py
@DateTime: 2026-09-22
@Docs: R2：Agent 工具按当前用户的业务权限授权，与页面/REST 的授权结果一致。

实现流程：
1. 每个工具都登记了所需业务权限，未登记的工具一律拒绝（新增工具忘了登记会在这里失败）。
2. 根调度器、HITL 门控在执行边界逐次现查权限：撤销立即生效，超管沿用既有规则。
3. 监控历史探测记录单独需要 monitor_log:read，缺它时只返回当前状态。
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent.hitl_gate import HitlGateHook
from app.agent.permissions import TOOL_PERMISSIONS
from app.agent.roles import list_roles
from app.agent.spawn_tools import spawn_tool_schemas
from app.agent.tool_dispatch import build_root_tool_dispatcher, root_tool_schemas
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_every_agent_tool_declares_its_business_permission() -> None:
    """根工具、编排工具、所有子 Agent 角色的工具都必须登记权限要求。"""
    names = {schema["function"]["name"] for schema in root_tool_schemas()}
    names |= {schema["function"]["name"] for schema in spawn_tool_schemas()}
    for role in list_roles():
        names |= set(role.tools_allowlist)

    assert names - set(TOOL_PERMISSIONS) == set()


async def _session_and_asset(db: AsyncSession, user_id: int) -> tuple[int, int]:
    session = await agent_session_crud.create(
        db, {"user_id": user_id, "title": "权限测试", "status": "active"}
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "switch",
            "hostname": "SW-PERM-01",
            "ip_address": "10.9.0.1",
            "vendor": "cisco_iosxe",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password_encrypted": "placeholder",
        },
    )
    await db.commit()
    return session.id, asset.id


async def test_query_cmdb_requires_cmdb_read(
    db_session: AsyncSession,
    test_user: User,
    grant_permissions,
) -> None:
    """审查报告 R2 的复现：只有 agent:use 的用户经根调度器调 query_cmdb 必须被拒；
    授予 cmdb:read 后同一个调度器立刻放行（每次调用现查，不缓存）。"""
    await grant_permissions(test_user, "agent:use")
    session_id, asset_id = await _session_and_asset(db_session, test_user.id)
    dispatch = build_root_tool_dispatcher(
        db_session, session_id=session_id, actor_user_id=test_user.id
    )

    denied = await dispatch("query_cmdb", {"asset_ids": [asset_id]})
    assert denied.control == "rejected"
    assert "cmdb:read" in denied.content
    assert "SW-PERM-01" not in denied.content

    await grant_permissions(test_user, "cmdb:read")
    allowed = await dispatch("query_cmdb", {"asset_ids": [asset_id]})
    assert allowed.control == "ok"
    assert "SW-PERM-01" in allowed.content


async def test_revoking_permission_takes_effect_on_next_tool_call(
    db_session: AsyncSession,
    test_user: User,
    grant_permissions,
    revoke_permissions,
) -> None:
    await grant_permissions(test_user, "agent:use", "cmdb:read")
    session_id, asset_id = await _session_and_asset(db_session, test_user.id)
    dispatch = build_root_tool_dispatcher(
        db_session, session_id=session_id, actor_user_id=test_user.id
    )
    assert (await dispatch("query_cmdb", {"asset_ids": [asset_id]})).control == "ok"

    await revoke_permissions(test_user, "cmdb:read")

    result = await dispatch("query_cmdb", {"asset_ids": [asset_id]})
    assert result.control == "rejected"
    assert "SW-PERM-01" not in result.content


@pytest.mark.parametrize(
    ("tool_name", "arguments", "permission"),
    [
        ("kb_glob", {"pattern": "*.md"}, "knowledge:read"),
        ("kb_semantic_search", {"query": "交换机"}, "knowledge:read"),
        ("query_monitor_status", {}, "monitor:read"),
        ("query_cmdb_dependencies", {"asset_id": 1}, "cmdb:read"),
        ("list_device_commands", {"asset_id": 1}, "cmdb:read"),
        ("get_device_query_result", {"proposal_id": 1}, "cmdb:read"),
    ],
)
async def test_root_tools_require_their_business_permission(
    db_session: AsyncSession,
    test_user: User,
    grant_permissions,
    tool_name: str,
    arguments: dict[str, Any],
    permission: str,
) -> None:
    await grant_permissions(test_user, "agent:use")
    dispatch = build_root_tool_dispatcher(db_session, session_id=1, actor_user_id=test_user.id)

    result = await dispatch(tool_name, arguments)

    assert result.control == "rejected"
    assert permission in result.content


async def test_agent_use_itself_is_rechecked_on_every_tool_call(
    db_session: AsyncSession,
    test_user: User,
    grant_permissions,
) -> None:
    """只有业务权限、没有 agent:use（例如会话中途被撤销）也不能继续调工具。"""
    await grant_permissions(test_user, "cmdb:read")
    session_id, asset_id = await _session_and_asset(db_session, test_user.id)
    dispatch = build_root_tool_dispatcher(
        db_session, session_id=session_id, actor_user_id=test_user.id
    )

    result = await dispatch("query_cmdb", {"asset_ids": [asset_id]})

    assert result.control == "rejected"
    assert "agent:use" in result.content


async def test_superuser_uses_tools_without_explicit_grants(
    db_session: AsyncSession,
    superuser: User,
) -> None:
    session_id, asset_id = await _session_and_asset(db_session, superuser.id)
    dispatch = build_root_tool_dispatcher(
        db_session, session_id=session_id, actor_user_id=superuser.id
    )

    result = await dispatch("query_cmdb", {"asset_ids": [asset_id]})

    assert result.control == "ok"
    assert "SW-PERM-01" in result.content


async def test_deactivated_user_cannot_call_tools(
    db_session: AsyncSession,
    test_user: User,
    grant_permissions,
) -> None:
    await grant_permissions(test_user, "agent:use", "cmdb:read")
    session_id, asset_id = await _session_and_asset(db_session, test_user.id)
    test_user.is_active = False
    await db_session.commit()
    dispatch = build_root_tool_dispatcher(
        db_session, session_id=session_id, actor_user_id=test_user.id
    )

    result = await dispatch("query_cmdb", {"asset_ids": [asset_id]})

    assert result.control == "rejected"
    assert "SW-PERM-01" not in result.content


async def test_monitor_history_needs_monitor_log_read(
    db_session: AsyncSession,
    test_user: User,
    grant_permissions,
) -> None:
    """monitor:read 只能看当前状态；探测历史与 REST 事件接口一样还要 monitor_log:read。"""
    target = await monitor_target_crud.create(
        db_session,
        {"cmdb_asset_id": None, "ip_address": "10.9.0.5", "port": 22, "label": "core"},
    )
    await db_session.flush()
    await monitor_status_event_crud.record(db_session, target_id=target.id, status="up")
    await db_session.commit()
    await grant_permissions(test_user, "agent:use", "monitor:read")
    dispatch = build_root_tool_dispatcher(db_session, session_id=1, actor_user_id=test_user.id)

    current_only = await dispatch("query_monitor_status", {"target_ids": [target.id]})
    assert current_only.control == "ok"
    assert "当前: up" in current_only.content
    assert "最近记录" not in current_only.content

    await grant_permissions(test_user, "monitor_log:read")
    with_history = await dispatch("query_monitor_status", {"target_ids": [target.id]})
    assert "最近记录" in with_history.content


async def test_gated_device_tool_without_permission_creates_no_proposal(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    grant_permissions,
) -> None:
    """门控工具在 before 钩子里执行，先于普通调度器——权限检查也必须放在那里，
    否则只给调度器加检查等于没加。"""
    await grant_permissions(test_user, "agent:use")
    session_id, asset_id = await _session_and_asset(db_session, test_user.id)
    gate = HitlGateHook(
        async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False),
        session_id=session_id,
        actor_user_id=test_user.id,
    )

    decision = await gate.before(
        "device_control",
        {"asset_id": asset_id, "command_name": "reboot", "reason": "故障恢复"},
    )

    assert decision.block is True
    assert decision.result is not None
    assert decision.result.control == "rejected"
    assert "cmdb:read" in decision.result.content
    assert await hitl_proposal_crud.list_for_session(db_session, session_id) == []
