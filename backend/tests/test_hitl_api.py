"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_hitl_api.py
@DateTime: 2026-08-12 11:36
@Docs: 验证 HITL 提案查询与审批 HTTP API 的权限门控与状态机行为。
"""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import device_result_summary
from app.agent.hitl import propose_action
from app.agent.hitl_gate import HitlGateHook, dispatch_through_hitl_gate
from app.agent.tool_dispatch import build_root_tool_dispatcher
from app.core.cmdb_credential import encrypt_credential_password
from app.core.config import settings
from app.core.llm import ChatMessage, ChatResult
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import device_command_policy_crud
from app.crud.hitl_execution_result import hitl_execution_result_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.agent_message import AgentMessage
from app.models.audit_log import AuditLog
from app.models.hitl_proposal import HitlProposal
from app.models.permission import Permission
from app.models.user import User, user_roles
from app.schemas.agent_ws import AgentWsServerMessage

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


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


async def _make_pending_proposal(
    db: AsyncSession,
    *,
    user_id: int,
    action_type: str,
    payload: dict[str, object],
    reason: str,
) -> tuple[int, int]:
    """创建 PENDING 提案并提交，供跨会话的 HTTP 请求读取。"""
    session = await agent_session_crud.create(
        db,
        {"user_id": user_id, "title": "HITL API 测试", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "server",
            "hostname": "srv-hitl-api",
            "ip_address": "10.0.0.30",
            "business_system": "测试系统",
            "subnet_cidr": "",
        },
    )
    await db.flush()
    summary = await propose_action(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type=action_type,  # type: ignore[arg-type]
        asset_id=asset.id,
        payload=payload,
        reason=reason,
        actor_user_id=user_id,
    )
    await db.commit()
    return session.id, summary.proposal_id


async def _make_pending_device_query_proposal(
    db: AsyncSession,
    *,
    user_id: int,
    credential_type: str = "dynamic",
    action_type: str = "device_query",
    command_name: str = "show_version",
) -> tuple[int, int]:
    """创建动态凭据资产的 PENDING 设备提案。"""
    session = await agent_session_crud.create(
        db,
        {"user_id": user_id, "title": "HITL device_query 测试", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "switch",
            "hostname": "sw-hitl-api",
            "ip_address": "10.0.0.31",
            "vendor": "cisco_iosxe",
            "credential_type": credential_type,
            "credential_username": "admin",
            "credential_password_encrypted": None,
        },
    )
    await db.flush()
    summary = await propose_action(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type=action_type,  # type: ignore[arg-type]
        asset_id=asset.id,
        payload={"command_name": command_name},
        reason="查询设备版本",
        actor_user_id=user_id,
    )
    await db.commit()
    return session.id, summary.proposal_id


def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _chat_result(content: str) -> ChatResult:
    """构造不触发真实模型的固定总结结果。"""
    return ChatResult(
        content=content,
        tool_calls=[],
        finish_reason="stop",
        prompt_tokens=1,
        completion_tokens=1,
    )


async def _root_assistant_messages(
    db: AsyncSession,
    *,
    session_id: int,
) -> list[AgentMessage]:
    """读取根 transcript 的 assistant 消息。"""
    rows = (
        await db.execute(
            select(AgentMessage)
            .where(
                AgentMessage.session_id == session_id,
                AgentMessage.agent_id.is_(None),
                AgentMessage.role == "assistant",
            )
            .order_by(AgentMessage.id)
        )
    ).scalars()
    return list(rows.all())


def _hitl_gate(
    db_engine: AsyncEngine,
    *,
    session_id: int,
    user_id: int,
) -> HitlGateHook:
    """为自动审批回归测试构造独立短会话门控。"""
    session_factory = async_sessionmaker(
        db_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    return HitlGateHook(
        session_factory,
        session_id=session_id,
        actor_user_id=user_id,
    )


async def test_list_proposals_without_permission_returns_403(
    client: AsyncClient,
    auth_headers: Headers,
) -> None:
    """未授予 agent:hitl_approve 时查询应被拒绝。"""
    response = await client.get(
        "/api/v1/hitl/proposals",
        params={"session_id": 1},
        headers=auth_headers,
    )
    assert response.status_code == 403, response.text


async def test_list_proposals_returns_approver_payload(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """列表应对审批人返回完整 action_payload。"""
    await _grant_hitl_approve(db_session, test_user)
    session_id, proposal_id = await _make_pending_proposal(
        db_session,
        user_id=test_user.id,
        action_type="notify",
        payload={"message": "口联异常"},
        reason="监控告警",
    )

    response = await client.get(
        "/api/v1/hitl/proposals",
        params={"session_id": session_id},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert len(items) == 1
    assert items[0]["id"] == proposal_id
    assert items[0]["status"] == "PENDING"
    assert items[0]["action_payload"]["message"] == "口联异常"
    assert items[0]["action_payload"]["proposal_reason"] == "监控告警"


async def test_approve_notify_executes_and_writes_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准 notify 应执行到 EXECUTED，并写入审批与执行审计。"""
    summary_delivery = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.hitl.deliver_device_query_summary",
        summary_delivery,
        raising=False,
    )
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_proposal(
        db_session,
        user_id=test_user.id,
        action_type="notify",
        payload={"message": "人工批准通知"},
        reason="需要确认",
    )

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "EXECUTED"

    get_response = await client.get(
        f"/api/v1/hitl/proposals/{proposal_id}",
        headers=auth_headers,
    )
    assert get_response.status_code == 200, get_response.text
    assert get_response.json()["data"]["status"] == "EXECUTED"

    db_session.expire_all()
    actions = {
        row.action
        for row in (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target == f"hitl_proposal:{proposal_id}")
            )
        ).scalars()
    }
    assert "hitl_approved" in actions
    assert "hitl_executed" in actions or "hitl_notify_executed" in actions
    summary_delivery.assert_not_awaited()


async def test_approve_device_control_stays_approved_second_decide_conflicts(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """device_control 批准后连接失败应进入 UNKNOWN；再次审批返回 409。"""
    summary_delivery = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.hitl.deliver_device_query_summary",
        summary_delivery,
        raising=False,
    )
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    await _grant_hitl_approve(db_session, test_user)
    ciphertext = encrypt_credential_password("whatever")
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "HITL API device_control", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "server",
            "hostname": "srv-hitl-ctrl",
            "ip_address": "10.0.0.32",
            "vendor": "linux",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password_encrypted": ciphertext,
        },
    )
    await db_session.flush()
    summary = await propose_action(
        db_session,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset.id,
        payload={"command_name": "reboot"},
        reason="故障恢复",
        actor_user_id=test_user.id,
    )
    await db_session.commit()
    proposal_id = summary.proposal_id

    with patch(
        "app.agent.executors._open_netmiko_connection",
        side_effect=ConnectionError("unreachable"),
    ):
        first = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True},
            headers=auth_headers,
        )
    assert first.status_code == 200, first.text
    # 连接没建起来 = 命令确定未下发：回退到可重试的 APPROVED，而不是 UNKNOWN。
    assert first.json()["data"]["status"] == "APPROVED"
    assert first.json()["data"]["status_reason"] == "dispatch_failed_before_send"

    second = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert second.status_code == 409, second.text
    summary_delivery.assert_not_awaited()


async def test_reject_does_not_resume(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拒绝提案只更新状态，不应调用 resume。"""
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_proposal(
        db_session,
        user_id=test_user.id,
        action_type="notify",
        payload={"message": "将被拒绝"},
        reason="误报",
    )

    resume_mock = AsyncMock()
    summary_delivery = AsyncMock()
    monkeypatch.setattr("app.api.v1.hitl.resume_proposal", resume_mock)
    monkeypatch.setattr(
        "app.api.v1.hitl.deliver_device_query_summary",
        summary_delivery,
        raising=False,
    )

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": False},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "REJECTED"
    resume_mock.assert_not_awaited()

    db_session.expire_all()
    proposal = await hitl_proposal_crud.get(db_session, proposal_id)
    assert proposal is not None
    assert proposal.status == "REJECTED"
    summary_delivery.assert_not_awaited()


async def test_decide_device_query_requires_password_for_dynamic_credential(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准动态凭据 device_query 时未提供密码应返回 422。"""
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_device_query_proposal(
        db_session,
        user_id=test_user.id,
    )

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True},
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


async def test_decide_device_query_with_password_executes(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准 device_query 后应持久化完整结果与唯一总结，并按顺序广播。"""
    one_time_password = "one-time-pass-never-persist"
    fixed_summary = "固定总结：设备版本与 VLAN 配置正常，可在审批卡片查看原文。"
    full_output = "hostname sw-hitl-api\n" + "x" * 5_000
    model_messages: list[list[ChatMessage]] = []
    event_order: list[str] = []
    ws_envelopes: list[AgentWsServerMessage] = []

    async def fake_chat(
        model_key: str,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> ChatResult:
        assert model_key == "local-chat"
        assert kwargs.get("db") is not None
        model_messages.append(messages)
        return _chat_result(fixed_summary)

    async def record_broadcast(
        session_id: int,
        message: AgentWsServerMessage,
    ) -> None:
        assert session_id > 0
        event_order.append(message.type)
        ws_envelopes.append(message)

    monkeypatch.setattr(device_result_summary, "chat", fake_chat)
    monkeypatch.setattr("app.agent.ws_hub.hub.broadcast", record_broadcast)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    await _grant_hitl_approve(db_session, test_user)
    session_id, proposal_id = await _make_pending_device_query_proposal(
        db_session,
        user_id=test_user.id,
    )

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value=full_output)
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        response = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True, "dynamic_credential_password": one_time_password},
            headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "EXECUTED"
    db_session.expire_all()
    result_row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal_id)
    assert result_row is not None
    assert result_row.content == full_output
    assert result_row.summary == fixed_summary
    messages = await _root_assistant_messages(db_session, session_id=session_id)
    assert [message.content for message in messages] == [fixed_summary]

    assert event_order == ["hitl_resolved", "assistant_delta"]
    assert ws_envelopes[-1].payload == {"text": fixed_summary, "done": True}
    assert model_messages
    model_text = "\n".join(message.content for call in model_messages for message in call)
    ws_text = "\n".join(str(envelope.model_dump(mode="json")) for envelope in ws_envelopes)
    persisted_text = "\n".join(
        [response.text, result_row.content, result_row.summary or ""]
        + [message.content for message in messages]
    )
    assert one_time_password not in model_text
    assert one_time_password not in persisted_text
    assert one_time_password not in ws_text


async def test_assistant_broadcast_failure_keeps_executed_query_and_summary(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """assistant 广播异常不得改变已提交的设备结果、总结或 HTTP 成功语义。"""
    fixed_summary = "固定总结广播失败也必须保留"
    event_order: list[str] = []

    async def fake_chat(*args: Any, **kwargs: Any) -> ChatResult:
        return _chat_result(fixed_summary)

    async def fail_assistant_broadcast(
        session_id: int,
        message: AgentWsServerMessage,
    ) -> None:
        assert session_id > 0
        event_order.append(message.type)
        if message.type == "assistant_delta":
            raise RuntimeError("private broadcast detail")

    monkeypatch.setattr(device_result_summary, "chat", fake_chat)
    monkeypatch.setattr("app.agent.ws_hub.hub.broadcast", fail_assistant_broadcast)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    caplog.set_level(logging.WARNING, logger="app.api.v1.hitl")
    await _grant_hitl_approve(db_session, test_user)
    session_id, proposal_id = await _make_pending_device_query_proposal(
        db_session,
        user_id=test_user.id,
    )

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value="safe device output")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        response = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True, "dynamic_credential_password": "temporary-secret"},
            headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "EXECUTED"
    assert event_order == ["hitl_resolved", "assistant_delta"]
    db_session.expire_all()
    result_row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal_id)
    assert result_row is not None
    assert result_row.summary == fixed_summary
    messages = await _root_assistant_messages(db_session, session_id=session_id)
    assert [message.content for message in messages] == [fixed_summary]
    route_warnings = [
        record.getMessage() for record in caplog.records if record.name == "app.api.v1.hitl"
    ]
    assert route_warnings == [
        f"设备查询总结广播失败 proposal_id={proposal_id} exc_type=RuntimeError"
    ]
    assert fixed_summary not in route_warnings[0]
    assert "private broadcast detail" not in route_warnings[0]
    assert "temporary-secret" not in route_warnings[0]


async def test_summary_delivery_failure_keeps_executed_query_successful(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """总结服务异常不得把已 EXECUTED 的设备执行改成失败。"""
    calls: list[int] = []

    async def fake_chat(*args: Any, **kwargs: Any) -> ChatResult:
        return _chat_result("不会被服务替身使用")

    async def fail_delivery(*, session_factory: object, proposal_id: int) -> None:
        assert session_factory is not None
        calls.append(proposal_id)
        raise RuntimeError("private summary detail")

    monkeypatch.setattr(device_result_summary, "chat", fake_chat)
    monkeypatch.setattr(
        "app.api.v1.hitl.deliver_device_query_summary",
        fail_delivery,
        raising=False,
    )
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    caplog.set_level(logging.WARNING, logger="app.api.v1.hitl")
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_device_query_proposal(
        db_session,
        user_id=test_user.id,
    )

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value="persist before summary")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        response = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True, "dynamic_credential_password": "ephemeral-password"},
            headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "EXECUTED"
    assert calls == [proposal_id]
    db_session.expire_all()
    result_row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal_id)
    assert result_row is not None
    assert result_row.content == "persist before summary"
    route_warnings = [
        record.getMessage() for record in caplog.records if record.name == "app.api.v1.hitl"
    ]
    assert route_warnings == [
        f"设备查询总结交付失败 proposal_id={proposal_id} exc_type=RuntimeError"
    ]
    assert "private summary detail" not in route_warnings[0]
    assert "ephemeral-password" not in route_warnings[0]


async def test_approve_executed_device_control_does_not_deliver_query_summary(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工批准成功的 device_control 也不得进入 device_query 总结服务。"""
    summary_delivery = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.hitl.deliver_device_query_summary",
        summary_delivery,
        raising=False,
    )
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_device_query_proposal(
        db_session,
        user_id=test_user.id,
        action_type="device_control",
        command_name="reboot",
    )

    fake_connection = MagicMock()
    fake_connection.send_command_timing = MagicMock(return_value="reboot control output")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        response = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True, "dynamic_credential_password": "temporary-control-password"},
            headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "EXECUTED"
    summary_delivery.assert_not_awaited()


async def test_retry_executed_device_query_delivers_one_summary(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重试成功的 device_query 应同步持久化并广播唯一一条总结。"""
    fixed_summary = "重试后的固定总结"
    event_order: list[str] = []
    envelopes: list[AgentWsServerMessage] = []

    async def fake_chat(*args: Any, **kwargs: Any) -> ChatResult:
        return _chat_result(fixed_summary)

    async def record_broadcast(
        session_id: int,
        message: AgentWsServerMessage,
    ) -> None:
        assert session_id > 0
        event_order.append(message.type)
        envelopes.append(message)

    monkeypatch.setattr(device_result_summary, "chat", fake_chat)
    monkeypatch.setattr("app.agent.ws_hub.hub.broadcast", record_broadcast)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    await _grant_hitl_approve(db_session, test_user)
    session_id, proposal_id = await _make_pending_device_query_proposal(
        db_session,
        user_id=test_user.id,
    )
    proposal = await hitl_proposal_crud.get(db_session, proposal_id)
    assert proposal is not None
    proposal.status = "APPROVED"
    await db_session.commit()

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value="retry full output")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        response = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/retry",
            json={"dynamic_credential_password": "retry-only-password"},
            headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "EXECUTED"
    assert event_order == ["hitl_resolved", "assistant_delta"]
    assert envelopes[-1].payload == {"text": fixed_summary, "done": True}
    db_session.expire_all()
    result_row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal_id)
    assert result_row is not None
    assert result_row.content == "retry full output"
    messages = await _root_assistant_messages(db_session, session_id=session_id)
    assert [message.content for message in messages] == [fixed_summary]
    combined = "\n".join(
        [response.text, result_row.content, result_row.summary or ""]
        + [message.content for message in messages]
        + [str(envelope.model_dump(mode="json")) for envelope in envelopes]
    )
    assert "retry-only-password" not in combined


async def test_retry_approved_device_control_executes(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNKNOWN 经 allow_retry 回到 APPROVED 后可通过 /retry 再次执行。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode()))
    await _grant_hitl_approve(db_session, test_user)
    ciphertext = encrypt_credential_password("whatever")
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "HITL API retry", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "server",
            "hostname": "srv-hitl-retry",
            "ip_address": "10.0.0.33",
            "vendor": "linux",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password_encrypted": ciphertext,
        },
    )
    await db_session.flush()
    summary = await propose_action(
        db_session,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="device_control",
        asset_id=asset.id,
        payload={"command_name": "reboot"},
        reason="故障恢复",
        actor_user_id=test_user.id,
    )
    await db_session.commit()
    proposal_id = summary.proposal_id

    # 连接已建立但下发途中断开：结果不确定，走 UNKNOWN 人工核实流程。
    broken_connection = MagicMock()
    broken_connection.send_command = MagicMock(side_effect=ConnectionError("dropped mid-command"))
    with patch(
        "app.agent.executors._open_netmiko_connection",
        return_value=broken_connection,
    ):
        failed = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/decide",
            json={"approve": True},
            headers=auth_headers,
        )
    assert failed.status_code == 200, failed.text
    assert failed.json()["data"]["status"] == "UNKNOWN"
    assert "last_error" in failed.json()["data"]["action_payload"]

    authorized = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/resolve-unknown",
        json={"resolution": "allow_retry"},
        headers=auth_headers,
    )
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["data"]["status"] == "APPROVED"

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value="Linux host info")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        retried = await client.post(
            f"/api/v1/hitl/proposals/{proposal_id}/retry",
            json={},
            headers=auth_headers,
        )
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["status"] == "EXECUTED"
    assert "last_error" not in retried.json()["data"]["action_payload"]


async def test_retry_pending_proposal_conflicts(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PENDING 提案不能走重试通道。"""
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_proposal(
        db_session,
        user_id=test_user.id,
        action_type="notify",
        payload={"message": "仍待审批"},
        reason="监控告警",
    )

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/retry",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.text


async def test_retry_without_permission_returns_403(
    client: AsyncClient,
    auth_headers: Headers,
) -> None:
    """未授予 agent:hitl_approve 时重试应被拒绝。"""
    response = await client.post(
        "/api/v1/hitl/proposals/1/retry",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 403, response.text


async def test_retry_dynamic_device_query_requires_password(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动态凭据资产重试时必须提供本次登录密码。"""
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_device_query_proposal(
        db_session,
        user_id=test_user.id,
    )
    stored = await hitl_proposal_crud.get(db_session, proposal_id)
    assert stored is not None
    stored.status = "APPROVED"
    await db_session.commit()

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/retry",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


async def test_auto_approved_device_query_keeps_only_existing_agent_reply(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动审批的 device_query 不应被 HTTP 人工审批链路追加总结消息。"""
    original_reply = "Agent loop 原有回复"

    async def fake_chat(*args: Any, **kwargs: Any) -> ChatResult:
        return _chat_result("不应自动追加的设备总结")

    monkeypatch.setattr(device_result_summary, "chat", fake_chat)
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    encrypted_password = encrypt_credential_password("static-test-password")
    session = await agent_session_crud.create(
        db_session,
        {
            "user_id": test_user.id,
            "title": "自动审批不重复总结",
            "status": "active",
            "approval_mode": "assist",
        },
    )
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": "sw-auto-query",
            "ip_address": "10.0.0.41",
            "vendor": "cisco_iosxe",
            "credential_type": "static",
            "credential_username": "admin",
            "credential_password_encrypted": encrypted_password,
        },
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset.id,
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    db_session.add(
        AgentMessage(
            session_id=session.id,
            agent_id=None,
            role="assistant",
            content=original_reply,
        )
    )
    session_id = session.id
    asset_id = asset.id
    await db_session.commit()

    fake_connection = MagicMock()
    fake_connection.send_command = MagicMock(return_value="automatic query output")
    gate = _hitl_gate(
        db_engine,
        session_id=session_id,
        user_id=test_user.id,
    )
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=test_user.id,
        gate_hook=gate,
    )
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        await dispatch_through_hitl_gate(
            gate,
            dispatch,
            "query_device_command",
            {"asset_id": asset_id, "command_name": "show_version", "reason": "自动查询"},
        )

    db_session.expire_all()
    proposals = await hitl_proposal_crud.list_for_session(db_session, session_id)
    assert [proposal.status for proposal in proposals] == ["EXECUTED"]
    messages = await _root_assistant_messages(db_session, session_id=session_id)
    assert [message.content for message in messages] == [original_reply]


@pytest_asyncio.fixture
async def unknown_proposal(
    db_session: AsyncSession,
    test_user: User,
) -> HitlProposal:
    """创建处于 UNKNOWN 状态的提案，供 resolve-unknown 测试使用。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "unknown", "status": "active"},
    )
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type="notify",
        action_payload={"message": "test", "proposal_reason": "test"},
    )
    await hitl_proposal_crud.decide(
        db_session,
        proposal.id,
        approve=True,
        reviewed_by_user_id=test_user.id,
    )
    await hitl_proposal_crud.claim_execution(db_session, proposal.id)
    await hitl_proposal_crud.mark_unknown(
        db_session, proposal.id, reason="dispatch_outcome_unknown"
    )
    await db_session.commit()
    return proposal


async def test_resolve_unknown_requires_permission_and_records_actor(
    client: AsyncClient,
    auth_headers: Headers,
    db_session: AsyncSession,
    unknown_proposal: HitlProposal,
    test_user: User,
) -> None:
    """allow_retry 应回到 APPROVED 并记录处置人与 status_reason。"""
    await _grant_hitl_approve(db_session, test_user)
    proposal_id = unknown_proposal.id
    user_id = test_user.id
    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/resolve-unknown",
        json={"resolution": "allow_retry"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "APPROVED"

    db_session.expire_all()
    persisted = await hitl_proposal_crud.get(db_session, proposal_id)
    assert persisted is not None
    assert persisted.resolved_by_user_id == user_id
    assert persisted.status_reason == "retry_authorized"


async def test_resolve_unknown_invalid_resolution_returns_422(
    client: AsyncClient,
    auth_headers: Headers,
    db_session: AsyncSession,
    unknown_proposal: HitlProposal,
    test_user: User,
) -> None:
    """非法 resolution 应返回 422。"""
    await _grant_hitl_approve(db_session, test_user)
    response = await client.post(
        f"/api/v1/hitl/proposals/{unknown_proposal.id}/resolve-unknown",
        json={"resolution": "not_valid"},
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


async def test_resolve_unknown_non_unknown_returns_409(
    client: AsyncClient,
    auth_headers: Headers,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """非 UNKNOWN 提案调用 resolve-unknown 应返回 409。"""
    await _grant_hitl_approve(db_session, test_user)
    _, proposal_id = await _make_pending_proposal(
        db_session,
        user_id=test_user.id,
        action_type="notify",
        payload={"message": "仍待审批"},
        reason="监控告警",
    )
    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/resolve-unknown",
        json={"resolution": "allow_retry"},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.text


async def test_resolve_unknown_without_permission_returns_403(
    client: AsyncClient,
    auth_headers: Headers,
    unknown_proposal: HitlProposal,
) -> None:
    """未授予 agent:hitl_approve 时 resolve-unknown 应被拒绝。"""
    response = await client.post(
        f"/api/v1/hitl/proposals/{unknown_proposal.id}/resolve-unknown",
        json={"resolution": "allow_retry"},
        headers=auth_headers,
    )
    assert response.status_code == 403, response.text


async def test_resolve_unknown_confirm_executed_writes_executed_at_and_audit(
    client: AsyncClient,
    auth_headers: Headers,
    db_session: AsyncSession,
    unknown_proposal: HitlProposal,
    test_user: User,
) -> None:
    """confirm_executed 应写入 executed_at 并记录 hitl_unknown_confirmed 审计。"""
    await _grant_hitl_approve(db_session, test_user)
    proposal_id = unknown_proposal.id
    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/resolve-unknown",
        json={"resolution": "confirm_executed"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "EXECUTED"
    assert response.json()["data"]["executed_at"] is not None

    db_session.expire_all()
    actions = {
        row.action
        for row in (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target == f"hitl_proposal:{proposal_id}")
            )
        ).scalars()
    }
    assert "hitl_unknown_confirmed" in actions
