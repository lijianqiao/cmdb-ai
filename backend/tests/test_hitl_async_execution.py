"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_hitl_async_execution.py
@DateTime: 2026-09-22
@Docs: R8 第二阶段：批准/重试只提交审批并排进后台执行队列，立刻返回 202。

实现流程：
1. 批准返回 202，提案仍是 APPROVED 并带 execution_state；后台执行完成后查到终态。
2. 队列满时在提交审批之前拒绝（503），提案仍是 PENDING——不会出现「失败但其实已批准」。
3. 同一提案同时只有一个执行任务：排队/执行期间重复点重试拿到同一个执行请求。
4. 刷新页面：快照带 execution_state，卡片知道它还在排队/执行，不显示重试按钮。
5. 动态密码只在后台任务内存里，提案与审计里都没有。
6. 关停：还在排队的不执行、提案停在 APPROVED；执行中被打断的落 UNKNOWN 等人工核实。
"""

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.executors import DeviceQueryExecutor, ExecutionResult
from app.agent.hitl import propose_action
from app.agent.hitl_executor import HitlExecutionQueue, hitl_execution_queue
from app.api.v1 import hitl as hitl_api
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.audit_log import AuditLog
from app.models.hitl_proposal import HitlProposal
from app.models.user import User

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def _pending_proposal(
    db: AsyncSession,
    user_id: int,
    *,
    action_type: str,
    payload: dict[str, object],
    credential_type: str = "static",
) -> tuple[int, int]:
    session = await agent_session_crud.create(
        db, {"user_id": user_id, "title": "后台执行", "status": "active"}
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "switch",
            "hostname": "SW-ASYNC-01",
            "ip_address": "10.9.0.1",
            "vendor": "cisco_iosxe",
            "credential_type": credential_type,
            "credential_username": "admin",
            "credential_password_encrypted": "placeholder" if credential_type == "static" else None,
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
        reason="后台执行测试",
        actor_user_id=user_id,
    )
    await db.commit()
    return session.id, summary.proposal_id


class BlockingDevice:
    """替身设备：记录每次调用，卡在 release 上直到测试放行。"""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[dict[str, Any]] = []

    async def execute(self, _executor: object, _db: object, **kwargs: Any) -> ExecutionResult:
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        return ExecutionResult(ok=True, message="ok", detail={"output": "done", "truncated": False})


@pytest.fixture
def blocking_device(monkeypatch: pytest.MonkeyPatch) -> BlockingDevice:
    device = BlockingDevice()

    async def execute(self: DeviceQueryExecutor, db: object, **kwargs: Any) -> ExecutionResult:
        return await device.execute(self, db, **kwargs)

    monkeypatch.setattr(DeviceQueryExecutor, "execute", execute)
    return device


async def _status(db: AsyncSession, proposal_id: int) -> str:
    db.expire_all()
    proposal = await hitl_proposal_crud.get(db, proposal_id)
    assert proposal is not None
    return proposal.status


async def test_approve_returns_202_and_executes_in_background(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    grant_permissions,
) -> None:
    await grant_permissions(test_user, "agent:hitl_approve")
    _, proposal_id = await _pending_proposal(
        db_session, test_user.id, action_type="notify", payload={"message": "后台执行的通知"}
    )

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide", json={"approve": True}, headers=auth_headers
    )

    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["status"] == "APPROVED"
    assert data["execution_state"] == "queued"
    assert data["execution_request_id"]

    await hitl_execution_queue.drain()
    after = await client.get(f"/api/v1/hitl/proposals/{proposal_id}", headers=auth_headers)
    assert after.json()["data"]["status"] == "EXECUTED"
    assert after.json()["data"]["execution_state"] is None


async def test_approve_refused_before_commit_when_queue_is_full(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    grant_permissions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """排不进队就不提交审批：响应明确说审批没提交，提案仍待审批，可以稍后再批。"""
    await grant_permissions(test_user, "agent:hitl_approve")
    _, proposal_id = await _pending_proposal(
        db_session, test_user.id, action_type="notify", payload={"message": "排不进队"}
    )
    full_queue = HitlExecutionQueue(max_running=1, max_queued=0)
    assert full_queue.reserve(999_999) is not None
    monkeypatch.setattr(hitl_api, "hitl_execution_queue", full_queue)

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide", json={"approve": True}, headers=auth_headers
    )

    assert response.status_code == 503, response.text
    assert "未提交" in response.json()["message"]
    assert int(response.headers["Retry-After"]) > 0
    assert await _status(db_session, proposal_id) == "PENDING"


async def test_retry_while_execution_active_returns_same_request(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    grant_permissions,
    blocking_device: BlockingDevice,
) -> None:
    """重复点重试不会重复入队：同一提案同时只有一个执行任务，设备只被连一次。"""
    await grant_permissions(test_user, "agent:hitl_approve")
    _, proposal_id = await _pending_proposal(
        db_session, test_user.id, action_type="device_query", payload={"command_name": "show_version"}
    )
    proposal = await db_session.get(HitlProposal, proposal_id)
    assert proposal is not None
    proposal.status = "APPROVED"
    await db_session.commit()

    first = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/retry", json={}, headers=auth_headers
    )
    await blocking_device.started.wait()
    second = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/retry", json={}, headers=auth_headers
    )
    blocking_device.release.set()
    await hitl_execution_queue.drain()

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert second.json()["data"]["execution_request_id"] == first.json()["data"][
        "execution_request_id"
    ]
    assert len(blocking_device.calls) == 1
    assert await _status(db_session, proposal_id) == "EXECUTED"


async def test_snapshot_reports_execution_state_until_it_finishes(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    grant_permissions,
    blocking_device: BlockingDevice,
) -> None:
    """刷新页面时卡片要知道提案还在执行，而不是把它当成「已批准、可重试」。"""
    await grant_permissions(test_user, "agent:use", "agent:hitl_approve")
    session_id, proposal_id = await _pending_proposal(
        db_session, test_user.id, action_type="device_query", payload={"command_name": "show_version"}
    )

    await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide", json={"approve": True}, headers=auth_headers
    )
    await blocking_device.started.wait()
    during = await client.get(f"/api/v1/agent/sessions/{session_id}/snapshot", headers=auth_headers)
    blocking_device.release.set()
    await hitl_execution_queue.drain()
    after = await client.get(f"/api/v1/agent/sessions/{session_id}/snapshot", headers=auth_headers)

    assert during.status_code == 200, during.text
    [running] = [p for p in during.json()["data"]["proposals"] if p["proposal_id"] == proposal_id]
    assert running["execution_state"] == "running"
    [done] = [p for p in after.json()["data"]["proposals"] if p["proposal_id"] == proposal_id]
    assert done["execution_state"] is None
    assert done["status"] == "EXECUTED"


async def test_dynamic_password_is_only_kept_in_memory(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    grant_permissions,
    blocking_device: BlockingDevice,
) -> None:
    await grant_permissions(test_user, "agent:hitl_approve")
    _, proposal_id = await _pending_proposal(
        db_session,
        test_user.id,
        action_type="device_query",
        payload={"command_name": "show_version"},
        credential_type="dynamic",
    )
    secret = "Once-Only!Pass 2026"

    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True, "dynamic_credential_password": secret},
        headers=auth_headers,
    )
    await blocking_device.started.wait()
    blocking_device.release.set()
    await hitl_execution_queue.drain()

    assert response.status_code == 202, response.text
    assert blocking_device.calls[0]["dynamic_password"] == secret
    db_session.expire_all()
    proposal = await hitl_proposal_crud.get(db_session, proposal_id)
    assert proposal is not None
    assert secret not in str(proposal.action_payload)
    assert secret not in str(proposal.evidence_snapshot)
    audits = (await db_session.execute(select(AuditLog))).scalars().all()
    assert all(secret not in (audit.detail or "") for audit in audits)


async def test_shutdown_leaves_queued_approved_and_marks_interrupted_unknown(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    grant_permissions,
    blocking_device: BlockingDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重启不会自动重跑可能已生效的写操作：没开始的停在 APPROVED，跑到一半的落 UNKNOWN。"""
    queue = HitlExecutionQueue(max_running=1, max_queued=1)
    monkeypatch.setattr(hitl_api, "hitl_execution_queue", queue)
    await grant_permissions(test_user, "agent:hitl_approve")
    _, running_id = await _pending_proposal(
        db_session, test_user.id, action_type="device_query", payload={"command_name": "show_version"}
    )
    _, queued_id = await _pending_proposal(
        db_session, test_user.id, action_type="device_query", payload={"command_name": "show_version"}
    )

    await client.post(
        f"/api/v1/hitl/proposals/{running_id}/decide", json={"approve": True}, headers=auth_headers
    )
    await blocking_device.started.wait()
    queued = await client.post(
        f"/api/v1/hitl/proposals/{queued_id}/decide", json={"approve": True}, headers=auth_headers
    )
    await queue.shutdown()

    assert queued.status_code == 202, queued.text
    assert len(blocking_device.calls) == 1
    assert await _status(db_session, running_id) == "UNKNOWN"
    assert await _status(db_session, queued_id) == "APPROVED"


async def test_execution_request_is_persisted_without_the_password(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    grant_permissions,
    blocking_device: BlockingDevice,
) -> None:
    """执行请求只记元数据。动态密码出现在设备调用里，不出现在请求行。"""
    await grant_permissions(test_user, "agent:hitl_approve")
    _, proposal_id = await _pending_proposal(
        db_session,
        test_user.id,
        action_type="device_query",
        payload={"command_name": "show_version"},
        credential_type="dynamic",
    )
    secret = "Once-Only!Pass 2026"
    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide",
        json={"approve": True, "dynamic_credential_password": secret},
        headers=auth_headers,
    )
    await blocking_device.started.wait()
    blocking_device.release.set()
    await hitl_execution_queue.drain()

    assert response.status_code == 202, response.text
    from app.models.hitl_execution_request import HitlExecutionRequest

    row = (
        await db_session.execute(
            select(HitlExecutionRequest).where(HitlExecutionRequest.proposal_id == proposal_id)
        )
    ).scalar_one()
    assert row.credential_kind == "dynamic"
    assert row.status == "finished"
    assert secret not in str(row.request_id)
    dumped = f"{row.status} {row.credential_kind} {row.attempt}"
    assert secret not in dumped


async def test_recovery_restarts_static_and_waits_for_dynamic_password(
    db_engine: object,
    db_session: AsyncSession,
    test_user: User,
    grant_permissions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重启后只恢复有执行请求、且仍是 APPROVED 的静态任务。动态密码丢了就不执行。"""
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

    from app.agent.executors import NotifyExecutor
    from app.agent.hitl_executor import recover_persisted_execution_requests
    from app.models.hitl_execution_request import HitlExecutionRequest

    assert isinstance(db_engine, AsyncEngine)
    await grant_permissions(test_user, "agent:hitl_approve")
    _, static_id = await _pending_proposal(
        db_session, test_user.id, action_type="notify", payload={"message": "恢复静态"}
    )
    _, dynamic_id = await _pending_proposal(
        db_session,
        test_user.id,
        action_type="device_query",
        payload={"command_name": "show_version"},
        credential_type="dynamic",
    )
    _, untouched_id = await _pending_proposal(
        db_session, test_user.id, action_type="notify", payload={"message": "没有执行请求"}
    )
    for proposal_id, kind in ((static_id, "none"), (dynamic_id, "dynamic")):
        proposal = await db_session.get(HitlProposal, proposal_id)
        assert proposal is not None
        proposal.status = "APPROVED"
        db_session.add(
            HitlExecutionRequest(
                request_id=f"req-{proposal_id}",
                proposal_id=proposal_id,
                attempt=1,
                actor_user_id=test_user.id,
                status="queued",
                credential_kind=kind,
            )
        )
    untouched = await db_session.get(HitlProposal, untouched_id)
    assert untouched is not None
    untouched.status = "APPROVED"
    await db_session.commit()

    calls: list[int] = []
    device_calls: list[str] = []

    async def execute(
        self: NotifyExecutor,
        db: AsyncSession,
        *,
        proposal_id: int,
        payload: dict[str, object],
        actor_user_id: int | None,
    ) -> ExecutionResult:
        calls.append(proposal_id)
        return ExecutionResult(ok=True, message="ok", detail={})

    async def device_execute(self: DeviceQueryExecutor, db: object, **kwargs: object) -> ExecutionResult:
        device_calls.append(str(kwargs.get("command_name")))
        return ExecutionResult(ok=True, message="ok", detail={"output": "ok", "truncated": False})

    monkeypatch.setattr(NotifyExecutor, "execute", execute)
    monkeypatch.setattr(DeviceQueryExecutor, "execute", device_execute)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
    await recover_persisted_execution_requests(session_factory)
    await hitl_execution_queue.drain()

    assert calls == [static_id]
    assert device_calls == []
    assert await _status(db_session, static_id) == "EXECUTED"
    assert await _status(db_session, dynamic_id) == "APPROVED"
    assert await _status(db_session, untouched_id) == "APPROVED"
    db_session.expire_all()
    dynamic_row = (
        await db_session.execute(
            select(HitlExecutionRequest).where(HitlExecutionRequest.proposal_id == dynamic_id)
        )
    ).scalar_one()
    assert dynamic_row.status == "awaiting_credential"


async def test_two_open_execution_requests_for_one_proposal_are_rejected(
    db_session: AsyncSession,
    test_user: User,
    grant_permissions,
) -> None:
    """同一提案不能同时有两条未结束的执行请求，避免恢复和点击各发一次命令。"""
    from sqlalchemy.exc import IntegrityError

    from app.models.hitl_execution_request import HitlExecutionRequest

    await grant_permissions(test_user, "agent:hitl_approve")
    _, proposal_id = await _pending_proposal(
        db_session, test_user.id, action_type="notify", payload={"message": "唯一"}
    )
    db_session.add(
        HitlExecutionRequest(
            request_id="open-a",
            proposal_id=proposal_id,
            attempt=1,
            actor_user_id=test_user.id,
            status="queued",
            credential_kind="none",
        )
    )
    await db_session.commit()
    db_session.add(
        HitlExecutionRequest(
            request_id="open-b",
            proposal_id=proposal_id,
            attempt=2,
            actor_user_id=test_user.id,
            status="running",
            credential_kind="none",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_device_control_writes_a_conclusion_into_the_chat(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    grant_permissions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开端口这类变更没有配置摘要，执行完要往对话里写一条结论，不能只剩工具调用。"""
    from app.crud.agent_message import agent_message_crud

    await grant_permissions(test_user, "agent:hitl_approve")
    session_id, proposal_id = await _pending_proposal(
        db_session,
        test_user.id,
        action_type="device_control",
        payload={"command_name": "port_enable", "interface_names": ["GigabitEthernet1/0/15"]},
    )

    async def execute(self: DeviceQueryExecutor, db: object, **kwargs: Any) -> ExecutionResult:
        return ExecutionResult(
            ok=True,
            message="命令执行完成",
            detail={"output": "[SW-ASYNC-01-GigabitEthernet1/0/15]", "truncated": False},
            dispatched=True,
        )

    monkeypatch.setattr(DeviceQueryExecutor, "execute", execute)
    response = await client.post(
        f"/api/v1/hitl/proposals/{proposal_id}/decide", json={"approve": True}, headers=auth_headers
    )
    assert response.status_code == 202, response.text
    await hitl_execution_queue.drain()

    assert await _status(db_session, proposal_id) == "EXECUTED"
    messages, _ = await agent_message_crud.list_root_before_id(
        db_session, session_id, before_id=None, limit=20
    )
    conclusions = [m for m in messages if m.role == "assistant" and "开启端口" in m.content]
    assert len(conclusions) == 1
    assert "SW-ASYNC-01" in conclusions[0].content
    assert "GigabitEthernet1/0/15" in conclusions[0].content
    assert "设备已接受命令" in conclusions[0].content
