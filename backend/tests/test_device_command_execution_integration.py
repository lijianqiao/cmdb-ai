"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_device_command_execution_integration.py
@DateTime: 2026-08-12 22:20
@Docs: 设备命令执行端到端验收：白名单自动执行、黑名单拒绝、动态凭据强制人工、密码零泄露。

实现流程：
1. 通过根调度器调用 query_device_command，串联策略判定、HITL 提案与 Scrapli 执行。
2. 白名单 + 静态凭据当场执行；黑名单直接拒绝且不建提案；未分类走人工审批 HTTP 链路。
3. 动态凭据即使白名单也强制 PENDING，decide 缺密码 422、补密码后执行。
4. 全程 HTTP 响应体不得出现已知明文密码或密文；子 Agent 调度器拒绝 query_device_command。
"""

import re
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tool_dispatch import build_root_tool_dispatcher, build_tool_dispatcher
from app.core.cmdb_credential import encrypt_credential_password
from app.core.config import settings
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.permission import Permission
from app.models.user import User, user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]

_PROPOSAL_ID_RE = re.compile(r"设备命令(?:查询)?\s+(\d+)")


def _generate_fernet_key() -> str:
    return Fernet.generate_key().decode()


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


def _fake_scrapli_connection(output: str = "Cisco IOS XE Software") -> AsyncMock:
    """构造 Scrapli 连接 mock，返回指定命令输出。"""
    fake_connection = AsyncMock()
    fake_connection.send_command = AsyncMock(
        return_value=type("Resp", (), {"result": output, "failed": False})()
    )
    return fake_connection


async def test_whitelisted_static_credential_query_executes_in_one_call(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """白名单 + 静态凭据：query_device_command 一次调用当场执行并返回输出。"""
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

    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
    )
    fake_connection = _fake_scrapli_connection("fake device output line")
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        tool_result = await dispatch(
            "query_device_command",
            {
                "asset_id": asset_id,
                "command_name": "show_version",
                "reason": "排查交换机版本",
            },
        )

    assert tool_result.control == "ok"
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

    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
    )
    tool_result = await dispatch(
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
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    session_id, asset_id = await _make_session_and_switch_asset(
        db_session,
        test_user.id,
        credential_password_encrypted=encrypt_credential_password("static-pass"),
    )
    await db_session.commit()

    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
    )
    tool_result = await dispatch(
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

    fake_connection = _fake_scrapli_connection("approved device output")
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
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
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
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

    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
    )
    tool_result = await dispatch(
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

    fake_connection = _fake_scrapli_connection("dynamic otp output")
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
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
    monkeypatch.setattr(settings, "HITL_NOTIFY_AUTO_APPROVE", False)
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

    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
    )

    fake_connection = _fake_scrapli_connection("password-safe output")
    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        unclassified = await dispatch(
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

    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
        decide_static = await client.post(
            f"/api/v1/hitl/proposals/{unclassified_id}/decide",
            json={"approve": True},
            headers=auth_headers,
        )
    http_bodies.append(decide_static.text)

    dynamic_result = await dispatch(
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

    with patch("app.agent.executors._open_scrapli_connection", return_value=fake_connection):
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
