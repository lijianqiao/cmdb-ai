"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_device_command_execution_integration.py
@DateTime: 2026-08-13 11:30
@Docs: 设备命令执行端到端验收：查询与变更类命令的白名单自动执行、黑名单拒绝、动态凭据强制人工、密码零泄露。

实现流程：
1. 通过根调度器调用 query_device_command / device_control，串联策略判定、HITL 提案与 Netmiko 执行。
2. 白名单 + 静态凭据当场执行；黑名单直接拒绝且不建提案；未分类走人工审批 HTTP 链路。
3. 变更类 port_enable/port_disable 缺 interface_name 时在 propose 阶段拒绝；动态凭据即使白名单也强制 PENDING。
4. asset_type 范围创建 reboot 策略被 API 拒绝；全程 HTTP 响应体不得出现已知明文密码或密文。
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.hitl_gate import HitlGateHook, dispatch_through_hitl_gate
from app.agent.loop import ToolResult
from app.agent.tool_dispatch import build_root_tool_dispatcher, build_tool_dispatcher
from app.core.cmdb_credential import encrypt_credential_password
from app.core.config import settings
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.permission import Permission
from app.models.role import role_permissions
from app.models.user import User, user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]

_PROPOSAL_ID_RE = re.compile(r"设备(?:命令(?:查询)?|管控(?:请求|命令))\s+(\d+)")


def _generate_fernet_key() -> str:
    return Fernet.generate_key().decode()


def _make_gated_dispatch(
    db_session: AsyncSession,
    session_id: int,
    actor_user_id: int,
) -> tuple[HitlGateHook, object]:
    gate = HitlGateHook(db_session, session_id=session_id, actor_user_id=actor_user_id)
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=actor_user_id,
        gate_hook=gate,
    )
    return gate, dispatch


async def _dispatch_gated(
    db_session: AsyncSession,
    session_id: int,
    actor_user_id: int,
    name: str,
    arguments: dict[str, object],
) -> ToolResult:
    gate, dispatch = _make_gated_dispatch(db_session, session_id, actor_user_id)
    return await dispatch_through_hitl_gate(gate, dispatch, name, arguments)


def _extract_proposal_id(content: str) -> int:
    """从工具安全摘要中解析提案 ID。"""
    match = _PROPOSAL_ID_RE.search(content)
    assert match is not None, content
    return int(match.group(1))


async def _grant_hitl_approve(db_session: AsyncSession, test_user: User) -> None:
    """将 agent:hitl_approve 挂到 test_user 已有角色上。"""
    from app.models.role import role_permissions

    permission = Permission(name="审批 HITL 提案", code="agent:hitl_approve", module="Agent")
    db_session.add(permission)
    await db_session.flush()

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    await db_session.execute(
        role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
    )
    await db_session.commit()


async def _make_session_and_switch_asset(
    db: AsyncSession,
    user_id: int,
    *,
    credential_type: str = "static",
    credential_password_encrypted: str | None = "placeholder",
) -> tuple[int, int]:
    """创建 Agent 会话与带凭据的交换机资产。"""
    session = await agent_session_crud.create(
        db,
        {"user_id": user_id, "title": "设备命令集成测试", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "switch",
            "hostname": "sw-device-cmd-int",
            "ip_address": "10.0.0.60",
            "vendor": "cisco_iosxe",
            "credential_type": credential_type,
            "credential_username": "admin",
            "credential_password_encrypted": credential_password_encrypted,
        },
    )
    await db.flush()
    return session.id, asset.id


def _fake_netmiko_connection(output: str = "Cisco IOS XE Software") -> MagicMock:
    """构造 Netmiko 连接 mock，send_command 直接返回字符串输出。"""
    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value=output)
    return fake_connection


def _fake_netmiko_reboot_connection(output: str = "System will reboot") -> MagicMock:
    """构造 Netmiko 连接 mock，模拟 reboot 的两段式交互。

    第一次 send_command_timing 返回设备的确认提示（思科是 [confirm]），执行器匹配到
    提示后才会第二次调用把应答发出去，第二次才返回真正的执行输出。
    """
    fake_connection = MagicMock()
    fake_connection.send_command_timing = MagicMock(
        side_effect=["Proceed with reload? [confirm]", output]
    )
    return fake_connection


def _fake_netmiko_configs_connection(output: str = "ok") -> MagicMock:
    """构造 Netmiko 连接 mock，port_enable/port_disable 走 send_config_set。"""
    fake_connection = MagicMock()
    fake_connection.send_config_set = MagicMock(return_value=output)
    return fake_connection


async def _grant_policy_permissions(
    db_session: AsyncSession,
    test_user: User,
    *,
    read: bool = True,
    manage: bool = True,
) -> None:
    """现场创建 device_command_policy 权限并挂到 test_user 角色上。"""
    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    grants: list[tuple[str, str]] = []
    if read:
        grants.append(("device_command_policy:read", "查看设备命令策略"))
    if manage:
        grants.append(("device_command_policy:manage", "管理设备命令策略"))
    for code, name in grants:
        permission = Permission(name=name, code=code, module="设备命令策略")
        db_session.add(permission)
        await db_session.flush()
        await db_session.execute(
            role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
        )
    await db_session.commit()


async def test_whitelisted_static_credential_query_executes_in_one_call(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assist 档位下白名单 + 静态凭据：query_device_command 一次调用当场执行并返回输出。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    session_id, asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_password_encrypted=encrypt_credential_password("static-pass"),
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    await db_session.commit()
    session = await agent_session_crud.get(db_session, session_id)
    assert session is not None
    session.approval_mode = "assist"
    await db_session.flush()

    fake_connection = _fake_netmiko_connection("fake device output line")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        tool_result = await _dispatch_gated(db_session, session_id, test_user.id,
            "query_device_command",
            {
                "asset_id": asset_id,
                "command_name": "show_version",
                "reason": "排查交换机版本",
            },
        )

    assert tool_result.control == "ok", tool_result.content
    assert "fake device output line" in tool_result.content
    assert "static-pass" not in tool_result.content


async def test_blacklisted_command_is_rejected_without_creating_proposal(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """黑名单命令应直接拒绝，且不创建 HITL 提案。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    session_id, asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_password_encrypted=encrypt_credential_password("static-pass"),
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_running_config",
            "decision": "blacklist",
        },
    )
    await db_session.commit()

    tool_result = await _dispatch_gated(db_session, session_id, test_user.id,
        "query_device_command",
        {
            "asset_id": asset_id,
            "command_name": "show_running_config",
            "reason": "尝试读取配置",
        },
    )

    assert tool_result.control == "rejected"
    proposals = await hitl_proposal_crud.list_for_session(db_session, session_id)
    assert proposals == []


async def test_unclassified_command_creates_pending_proposal_visible_via_hitl_api(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未分类命令走 HITL：提案在审批 API 可见，批准后执行完成。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    session_id, asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_password_encrypted=encrypt_credential_password("static-pass"),
    )
    await db_session.commit()

    tool_result = await _dispatch_gated(db_session, session_id, test_user.id,
        "query_device_command",
        {
            "asset_id": asset_id,
            "command_name": "show_version",
            "reason": "未分类命令需人工审批",
        },
    )
    assert tool_result.control == "pending_approval"
    proposal_id = _extract_proposal_id(tool_result.content)
    await db_session.commit()

    await _grant_hitl_approve(db_session, test_user)
    list_response = await client.get(
        "/api/v1/hitl/proposals",
        params={"session_id": session_id},
        headers=auth_headers,
    )
    assert list_response.status_code == 200, list_response.text
    items = list_response.json()["data"]
    assert any(item["id"] == proposal_id and item["status"] == "PENDING" for item in items)

    fake_connection = _fake_netmiko_connection("approved device output")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        decide_response = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True},
            headers=auth_headers,
        )
    assert decide_response.status_code == 200, decide_response.text
    assert decide_response.json()["data"]["status"] == "EXECUTED"
    assert "approved device output" in decide_response.text or "EXECUTED" in decide_response.text


async def test_dynamic_credential_requires_password_even_when_whitelisted(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动态凭据即使白名单也强制人工；decide 缺密码 422，补密码后执行。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    session_id, asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_type="dynamic",
        credential_password_encrypted=None,
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    await db_session.commit()

    tool_result = await _dispatch_gated(db_session, session_id, test_user.id,
        "query_device_command",
        {
            "asset_id": asset_id,
            "command_name": "show_version",
            "reason": "动态凭据白名单仍须人工",
        },
    )
    assert tool_result.control == "pending_approval"
    proposal_id = _extract_proposal_id(tool_result.content)
    await db_session.commit()

    await _grant_hitl_approve(db_session, test_user)
    missing_password = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert missing_password.status_code == 422, missing_password.text

    fake_connection = _fake_netmiko_connection("dynamic otp output")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        with_password = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True, "dynamic_credential_password": "one-time-pass"},
            headers=auth_headers,
        )
    assert with_password.status_code == 200, with_password.text
    assert with_password.json()["data"]["status"] == "EXECUTED"


async def test_response_bodies_never_contain_plaintext_or_ciphertext_password(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全程 HTTP 响应体不得出现已知明文密码或对应密文。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    known_password = "KNOWN_PLAINTEXT_SECRET_99"
    ciphertext = encrypt_credential_password(known_password)
    http_bodies: list[str] = []

    session_id, static_asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_password_encrypted=ciphertext,
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": static_asset_id,
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )

    _, dynamic_asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_type="dynamic",
        credential_password_encrypted=None,
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": dynamic_asset_id,
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    await db_session.commit()
    await _grant_hitl_approve(db_session, test_user)


    fake_connection = _fake_netmiko_connection("password-safe output")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        unclassified = await _dispatch_gated(db_session, session_id, test_user.id,
            "query_device_command",
            {
                "asset_id": static_asset_id,
                "command_name": "ping",
                "reason": "未分类命令密码泄露回归",
            },
        )
    assert unclassified.control == "pending_approval"
    unclassified_id = _extract_proposal_id(unclassified.content)
    await db_session.commit()

    list_resp = await client.get(
        "/api/v1/hitl/proposals",
        params={"session_id": session_id},
        headers=auth_headers,
    )
    http_bodies.append(list_resp.text)

    detail_resp = await client.get(
        f"/api/v1/hitl/proposals/{unclassified_id}",
        headers=auth_headers,
    )
    http_bodies.append(detail_resp.text)

    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        decide_static = await client.post(
            f"/api/v1/hitl/proposals/{unclassified_id}/decide",
            json={"approve": True},
            headers=auth_headers,
        )
    http_bodies.append(decide_static.text)

    dynamic_result = await _dispatch_gated(db_session, session_id, test_user.id,
        "query_device_command",
        {
            "asset_id": dynamic_asset_id,
            "command_name": "show_version",
            "reason": "动态凭据密码泄露回归",
        },
    )
    assert dynamic_result.control == "pending_approval"
    dynamic_id = _extract_proposal_id(dynamic_result.content)
    await db_session.commit()

    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        decide_dynamic = await client.post(
            f"/api/v1/hitl/proposals/{dynamic_id}/decide",
            json={"approve": True, "dynamic_credential_password": known_password},
            headers=auth_headers,
        )
    http_bodies.append(decide_dynamic.text)

    for body in http_bodies:
        assert known_password not in body
        assert ciphertext not in body


async def test_child_agent_dispatcher_rejects_query_device_command(
    db_session: AsyncSession,
) -> None:
    """子角色调度器不得暴露或执行 query_device_command。"""
    dispatch = build_tool_dispatcher(
        db_session,
        ("query_monitor_status", "query_device_command"),
    )

    result = await dispatch(
        "query_device_command",
        {
            "asset_id": 1,
            "command_name": "show_version",
            "reason": "子角色越权尝试",
        },
    )

    assert result.control == "rejected"
    assert "未知工具" in result.content


async def test_whitelisted_reboot_executes_with_interactive_confirmation(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assist 档位下白名单 + 静态凭据的交换机：device_control 一次调用当场执行 reboot。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    session_id, asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_password_encrypted=encrypt_credential_password("reboot-static-pass"),
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "reboot",
            "decision": "whitelist",
        },
    )
    await db_session.commit()
    session = await agent_session_crud.get(db_session, session_id)
    assert session is not None
    session.approval_mode = "assist"
    await db_session.flush()

    fake_connection = _fake_netmiko_reboot_connection("rebooting now")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        tool_result = await _dispatch_gated(db_session, session_id, test_user.id,
            "device_control",
            {
                "asset_id": asset_id,
                "command_name": "reboot",
                "reason": "故障恢复重启",
            },
        )

    assert tool_result.control == "ok", tool_result.content
    assert "rebooting now" in tool_result.content
    assert "reboot-static-pass" not in tool_result.content
    # 第一次拿确认提示，第二次发应答。
    assert fake_connection.send_command_timing.call_count == 2
    fake_connection.send_command.assert_not_called()


async def test_blacklisted_port_disable_is_rejected_without_creating_proposal(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """黑名单 port_disable 应直接拒绝，且不创建 HITL 提案。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    session_id, asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_password_encrypted=encrypt_credential_password("static-pass"),
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "port_disable",
            "decision": "blacklist",
        },
    )
    await db_session.commit()

    tool_result = await _dispatch_gated(db_session, session_id, test_user.id,
        "device_control",
        {
            "asset_id": asset_id,
            "command_name": "port_disable",
            "interface_name": "GigabitEthernet0/1",
            "reason": "尝试禁用端口",
        },
    )

    assert tool_result.control == "rejected"
    assert "黑名单" in tool_result.content
    proposals = await hitl_proposal_crud.list_for_session(db_session, session_id)
    assert proposals == []


async def test_unclassified_port_enable_creates_pending_and_requires_interface_name(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未分类 port_enable：缺 interface_name 时在 propose 阶段拒绝；补齐后走 PENDING → 批准 → 执行。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    session_id, asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_password_encrypted=encrypt_credential_password("static-pass"),
    )
    await db_session.commit()

    missing_iface = await _dispatch_gated(db_session, session_id, test_user.id,
        "device_control",
        {
            "asset_id": asset_id,
            "command_name": "port_enable",
            "reason": "启用端口缺接口名",
        },
    )
    assert missing_iface.control == "rejected"
    assert "接口名" in missing_iface.content
    proposals_before = await hitl_proposal_crud.list_for_session(db_session, session_id)
    assert proposals_before == []

    tool_result = await _dispatch_gated(db_session, session_id, test_user.id,
        "device_control",
        {
            "asset_id": asset_id,
            "command_name": "port_enable",
            "interface_name": "GigabitEthernet0/1",
            "reason": "启用端口",
        },
    )
    assert tool_result.control == "pending_approval"
    proposal_id = _extract_proposal_id(tool_result.content)
    await db_session.commit()

    await _grant_hitl_approve(db_session, test_user)
    fake_connection = _fake_netmiko_configs_connection("port enabled")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        decide_response = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True},
            headers=auth_headers,
        )
    assert decide_response.status_code == 200, decide_response.text
    assert decide_response.json()["data"]["status"] == "EXECUTED"
    assert "port enabled" in decide_response.text or "EXECUTED" in decide_response.text
    fake_connection.send_config_set.assert_called_once()
    assert "static-pass" not in decide_response.text


async def test_create_asset_type_scope_policy_for_reboot_is_rejected_via_api(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """asset_type 范围创建 reboot 变更类策略应被 API 拒绝。"""
    await _grant_policy_permissions(db_session, test_user)
    response = await client.post(
        "/api/v1/device-command-policies/policies",
        json={
            "scope": "asset_type",
            "asset_type": "switch",
            "command_name": "reboot",
            "decision": "whitelist",
        },
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


async def test_dynamic_credential_reboot_still_forces_manual_approval_even_when_whitelisted(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动态凭据即使白名单 reboot 也强制人工；decide 缺密码 422，补密码后执行。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    session_id, asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_type="dynamic",
        credential_password_encrypted=None,
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "reboot",
            "decision": "whitelist",
        },
    )
    await db_session.commit()

    tool_result = await _dispatch_gated(db_session, session_id, test_user.id,
        "device_control",
        {
            "asset_id": asset_id,
            "command_name": "reboot",
            "reason": "动态凭据白名单仍须人工",
        },
    )
    assert tool_result.control == "pending_approval"
    proposal_id = _extract_proposal_id(tool_result.content)
    await db_session.commit()

    await _grant_hitl_approve(db_session, test_user)
    missing_password = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert missing_password.status_code == 422, missing_password.text

    fake_connection = _fake_netmiko_reboot_connection("dynamic reboot output")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        with_password = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True, "dynamic_credential_password": "one-time-pass"},
            headers=auth_headers,
        )
    assert with_password.status_code == 200, with_password.text
    assert with_password.json()["data"]["status"] == "EXECUTED"
    assert "one-time-pass" not in with_password.text
