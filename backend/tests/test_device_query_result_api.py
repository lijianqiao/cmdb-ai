"""验证会话归属的设备查询完整结果读取与总结恢复 API。"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import device_result_summary, ws_hub
from app.api.v1 import agent_sessions as agent_sessions_api
from app.core.llm import ChatResult
from app.core.security import hash_password
from app.crud.agent_message import agent_message_crud
from app.crud.agent_session import agent_session_crud
from app.crud.hitl_execution_result import hitl_execution_result_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.permission import Permission
from app.models.role import Role, role_permissions
from app.models.user import User

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


@pytest_asyncio.fixture(autouse=True)
async def _grant_agent_use(db_session: AsyncSession, test_role: Role) -> None:
    permission = Permission(name="使用运维助手", code="agent:use", module="Agent")
    db_session.add(permission)
    await db_session.flush()
    await db_session.execute(
        role_permissions.insert().values(role_id=test_role.id, permission_id=permission.id)
    )
    await db_session.commit()


async def _create_result(
    db: AsyncSession,
    user: User,
    *,
    content: str = "hostname edge-01\ninterface Vlan10\n",
    action_type: str = "device_query",
    create_result: bool = True,
) -> tuple[int, int]:
    session = await agent_session_crud.create(
        db,
        {"user_id": user.id, "title": "设备查询结果", "status": "active"},
    )
    proposal = await hitl_proposal_crud.create(
        db,
        session_id=session.id,
        proposed_by_agent_id=None,
        action_type=action_type,
        action_payload={
            "asset_id": 42,
            "command_name": "show_running_config",
            "proposal_reason": "检查配置",
            "dynamic_password": "never-return-this-password",
        },
    )
    proposal.status = "EXECUTED"
    if create_result:
        await hitl_execution_result_crud.create_for_proposal(
            db,
            proposal_id=proposal.id,
            content=content,
        )
    await db.commit()
    return session.id, proposal.id


async def _other_user(db: AsyncSession, role: Role, *, suffix: str) -> User:
    user = User(
        username=f"result_other_{suffix}",
        email=f"result_other_{suffix}@example.com",
        hashed_password=hash_password("testpassword123"),
        nickname="其他用户",
        is_active=True,
        is_superuser=False,
        roles=[role],
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _result_path(session_id: int, proposal_id: int) -> str:
    return f"/api/v1/agent/sessions/{session_id}/device-query-results/{proposal_id}"


async def test_owner_reads_only_the_full_result_dto(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    raw = "hostname edge-01\nsecret-but-authorized-full-result\n"
    session_id, proposal_id = await _create_result(db_session, test_user, content=raw)
    result_row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal_id)
    assert result_row is not None
    result_row.summary = "summary-must-not-be-returned"
    await db_session.commit()

    response = await client.get(_result_path(session_id, proposal_id), headers=auth_headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == {
        "proposal_id",
        "content",
        "content_length",
        "summary_status",
        "created_at",
    }
    assert data["proposal_id"] == proposal_id
    assert data["content"] == raw
    assert data["content_length"] == len(raw)
    assert data["summary_status"] == "pending"
    assert "summary-must-not-be-returned" not in response.text
    assert "never-return-this-password" not in response.text
    assert "action_payload" not in response.text


async def test_full_result_non_owner_is_hidden_as_404(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    test_role: Role,
    login_user,
) -> None:
    session_id, proposal_id = await _create_result(db_session, test_user)
    other = await _other_user(db_session, test_role, suffix="owner")
    headers = await login_user(other.username, "testpassword123")

    response = await client.get(_result_path(session_id, proposal_id), headers=headers)

    assert response.status_code == 404


@pytest.mark.parametrize("action_type,create_result", [("notify", True), ("device_query", False)])
async def test_full_result_wrong_action_or_missing_row_uses_stable_404(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    action_type: str,
    create_result: bool,
) -> None:
    session_id, proposal_id = await _create_result(
        db_session,
        test_user,
        action_type=action_type,
        create_result=create_result,
    )

    response = await client.get(_result_path(session_id, proposal_id), headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["message"] == "设备查询完整结果不存在"


async def test_full_result_proposal_from_another_owned_session_is_404(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    requested_session_id, _ = await _create_result(db_session, test_user)
    _, other_proposal_id = await _create_result(db_session, test_user)

    response = await client.get(
        _result_path(requested_session_id, other_proposal_id),
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["message"] == "设备查询完整结果不存在"


async def test_result_endpoints_require_agent_use_permission(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    login_user,
) -> None:
    session_id, proposal_id = await _create_result(db_session, test_user)
    role = Role(name="结果无权限角色", description="", permissions=[])
    db_session.add(role)
    await db_session.flush()
    user = User(
        username="resultnoperm",
        email="resultnoperm@example.com",
        hashed_password=hash_password("testpassword123"),
        nickname="无权限用户",
        is_active=True,
        is_superuser=False,
        roles=[role],
    )
    db_session.add(user)
    await db_session.commit()
    headers = await login_user(user.username, "testpassword123")

    get_response = await client.get(_result_path(session_id, proposal_id), headers=headers)
    post_response = await client.post(
        f"{_result_path(session_id, proposal_id)}/summary",
        headers=headers,
    )

    assert get_response.status_code == 403
    assert post_response.status_code == 403


async def test_get_normalizes_only_stale_generating_to_pending(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    active_session, active_proposal = await _create_result(db_session, test_user)
    stale_session, stale_proposal = await _create_result(db_session, test_user)
    active = await hitl_execution_result_crud.get_by_proposal(db_session, active_proposal)
    stale = await hitl_execution_result_crud.get_by_proposal(db_session, stale_proposal)
    assert active is not None and stale is not None
    active.summary_status = "generating"
    active.summary_started_at = datetime.now(UTC) - timedelta(minutes=4)
    stale.summary_status = "generating"
    stale.summary_started_at = datetime.now(UTC) - timedelta(minutes=6)
    await db_session.commit()

    active_response = await client.get(
        _result_path(active_session, active_proposal), headers=auth_headers
    )
    stale_response = await client.get(
        _result_path(stale_session, stale_proposal), headers=auth_headers
    )

    assert active_response.json()["data"]["summary_status"] == "generating"
    assert stale_response.json()["data"]["summary_status"] == "pending"
    db_session.expire_all()
    persisted = await hitl_execution_result_crud.get_by_proposal(db_session, stale_proposal)
    assert persisted is not None
    assert persisted.summary_status == "generating"


async def test_pending_summary_recovers_once_without_device_execution_and_broadcasts(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "hostname edge-01\ninterface Vlan10\n"
    summary = "设备为 edge-01，可在审批卡片查看原文。"
    session_id, proposal_id = await _create_result(db_session, test_user, content=raw)
    model_calls = 0
    device_calls = 0
    broadcasts: list[tuple[int, object]] = []

    async def fake_chat(*args: Any, **kwargs: Any) -> ChatResult:
        nonlocal model_calls
        model_calls += 1
        return ChatResult(
            content=summary,
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def forbidden_resume(*args: Any, **kwargs: Any) -> None:
        nonlocal device_calls
        device_calls += 1
        raise AssertionError("总结恢复不得执行设备命令")

    async def capture_broadcast(session: int, message: object) -> None:
        broadcasts.append((session, message))

    monkeypatch.setattr(device_result_summary, "chat", fake_chat)
    monkeypatch.setattr(agent_sessions_api, "resume_proposal", forbidden_resume, raising=False)
    monkeypatch.setattr(ws_hub.hub, "broadcast", capture_broadcast)

    response = await client.post(
        f"{_result_path(session_id, proposal_id)}/summary",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary_status"] == "completed"
    assert data["content"] == raw
    assert model_calls == 1
    assert device_calls == 0
    messages = await agent_message_crud.list_for_agent(
        db_session, session_id, agent_id=None
    )
    assert [(message.role, message.content) for message in messages] == [("assistant", summary)]
    assert len(broadcasts) == 1
    broadcast_session, envelope = broadcasts[0]
    assert broadcast_session == session_id
    assert envelope.model_dump(mode="json") == {
        "type": "assistant_delta",
        "payload": {"text": summary, "done": True},
    }
    assert raw not in str(envelope.model_dump(mode="json"))


@pytest.mark.parametrize("summary_status", ["completed", "fallback"])
async def test_finished_summary_is_idempotent_without_model_message_or_broadcast(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    summary_status: str,
) -> None:
    session_id, proposal_id = await _create_result(db_session, test_user)
    row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal_id)
    assert row is not None
    row.summary_status = summary_status
    row.summary = f"existing-{summary_status}"
    await agent_message_crud.append(
        db_session,
        session_id=session_id,
        agent_id=None,
        role="assistant",
        content=row.summary,
    )
    await db_session.commit()
    model_calls = 0
    broadcasts = 0

    async def forbidden_chat(*args: Any, **kwargs: Any) -> ChatResult:
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("幂等恢复不得调用模型")

    async def capture_broadcast(*args: Any, **kwargs: Any) -> None:
        nonlocal broadcasts
        broadcasts += 1

    monkeypatch.setattr(device_result_summary, "chat", forbidden_chat)
    monkeypatch.setattr(ws_hub.hub, "broadcast", capture_broadcast)

    response = await client.post(
        f"{_result_path(session_id, proposal_id)}/summary",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["summary_status"] == summary_status
    assert model_calls == 0
    assert broadcasts == 0
    messages = await agent_message_crud.list_for_agent(
        db_session, session_id, agent_id=None
    )
    assert [message.content for message in messages] == [f"existing-{summary_status}"]


async def test_active_generating_summary_returns_stable_conflict(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    session_id, proposal_id = await _create_result(db_session, test_user)
    row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal_id)
    assert row is not None
    row.summary_status = "generating"
    row.summary_started_at = datetime.now(UTC)
    await db_session.commit()

    response = await client.post(
        f"{_result_path(session_id, proposal_id)}/summary",
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["message"] == "设备查询结果正在生成总结"


async def test_stale_generating_summary_is_reclaimed(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, proposal_id = await _create_result(db_session, test_user)
    row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal_id)
    assert row is not None
    row.summary_status = "generating"
    row.summary_started_at = datetime.now(UTC) - timedelta(minutes=6)
    await db_session.commit()
    model_calls = 0

    async def fake_chat(*args: Any, **kwargs: Any) -> ChatResult:
        nonlocal model_calls
        model_calls += 1
        return ChatResult(
            content="过期任务恢复成功，可在审批卡片查看原文。",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    monkeypatch.setattr(device_result_summary, "chat", fake_chat)

    response = await client.post(
        f"{_result_path(session_id, proposal_id)}/summary",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["summary_status"] == "completed"
    assert model_calls == 1


async def test_summary_wrong_session_is_hidden_before_model_call(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_session, _ = await _create_result(db_session, test_user)
    _, other_proposal = await _create_result(db_session, test_user)
    model_calls = 0

    async def forbidden_chat(*args: Any, **kwargs: Any) -> ChatResult:
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("错会话不得调用模型")

    monkeypatch.setattr(device_result_summary, "chat", forbidden_chat)

    response = await client.post(
        f"{_result_path(requested_session, other_proposal)}/summary",
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["message"] == "设备查询完整结果不存在"
    assert model_calls == 0


async def test_broadcast_failure_does_not_rollback_recovered_summary(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, proposal_id = await _create_result(db_session, test_user)

    async def fake_chat(*args: Any, **kwargs: Any) -> ChatResult:
        return ChatResult(
            content="总结已持久化，可在审批卡片查看原文。",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    async def failing_broadcast(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fake hub failure")

    monkeypatch.setattr(device_result_summary, "chat", fake_chat)
    monkeypatch.setattr(ws_hub.hub, "broadcast", failing_broadcast)

    response = await client.post(
        f"{_result_path(session_id, proposal_id)}/summary",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    db_session.expire_all()
    row = await hitl_execution_result_crud.get_by_proposal(db_session, proposal_id)
    assert row is not None
    assert row.summary_status == "completed"
    messages = await agent_message_crud.list_for_agent(
        db_session, session_id, agent_id=None
    )
    assert [message.content for message in messages] == [
        "总结已持久化，可在审批卡片查看原文。"
    ]
