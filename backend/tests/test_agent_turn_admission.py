"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_turn_admission.py
@DateTime: 2026-09-22
@Docs: R5：根 turn 并发准入——全进程与单个用户同时在跑的对话轮数有上限。

实现流程：
1. 单会话租约只能挡住「同一会话连发」，挡不住一个人开很多会话、很多人同时提问；
   模型端并发能力和进程内存才是等模型时的真实瓶颈。
2. 满了立即拒绝、不排队：用户太多返回 429，整体过载返回 503，都带 Retry-After，
   前端据此提示稍后再试；排队只会让请求挂住、用户以为卡死。
3. 名额在请求结束时归还，无论本轮成功、失败还是被同会话 409 拒绝。
"""

import asyncio
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import LoopOutcome
from app.agent.turn_admission import TurnAdmission
from app.api.v1 import agent_sessions as agent_sessions_api
from app.models.permission import Permission
from app.models.role import Role, role_permissions

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


async def test_per_user_limit_is_checked_before_global_limit() -> None:
    admission = TurnAdmission(max_total=3, max_per_user=2)

    assert admission.try_acquire(1) == "ok"
    assert admission.try_acquire(1) == "ok"
    assert admission.try_acquire(1) == "user_limit"
    assert admission.try_acquire(2) == "ok"
    assert admission.try_acquire(3) == "global_limit"


async def test_release_frees_the_slot_for_the_same_user() -> None:
    admission = TurnAdmission(max_total=1, max_per_user=1)
    assert admission.try_acquire(1) == "ok"

    admission.release(1)

    assert admission.try_acquire(1) == "ok"


@pytest_asyncio.fixture(autouse=True)
async def _grant_agent_use(db_session: AsyncSession, test_role: Role) -> None:
    permission = Permission(name="使用运维助手", code="agent:use", module="Agent")
    db_session.add(permission)
    await db_session.flush()
    await db_session.execute(
        role_permissions.insert().values(role_id=test_role.id, permission_id=permission.id)
    )
    await db_session.commit()


async def _new_session(client: AsyncClient, headers: Headers) -> int:
    response = await client.post("/api/v1/agent/sessions", json={"title": "准入"}, headers=headers)
    assert response.status_code == 201, response.text
    return int(response.json()["data"]["id"])


async def _post(client: AsyncClient, session_id: int, headers: Headers) -> Response:
    return await client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "在吗"},
        headers=headers,
    )


@pytest.mark.parametrize(
    ("max_total", "max_per_user", "expected_status"),
    [(5, 1, 429), (1, 5, 503)],
)
async def test_turn_rejected_with_retry_after_when_admission_is_full(
    client: AsyncClient,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    max_total: int,
    max_per_user: int,
    expected_status: int,
) -> None:
    monkeypatch.setattr(
        agent_sessions_api,
        "turn_admission",
        TurnAdmission(max_total=max_total, max_per_user=max_per_user),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_turn(*args: Any, **kwargs: Any) -> LoopOutcome:
        entered.set()
        await release.wait()
        return LoopOutcome(reason="final_answer", final_answer="ok")

    monkeypatch.setattr(agent_sessions_api, "run_chat_turn", slow_turn)
    first_session = await _new_session(client, auth_headers)
    second_session = await _new_session(client, auth_headers)

    first = asyncio.create_task(_post(client, first_session, auth_headers))
    await entered.wait()
    rejected = await _post(client, second_session, auth_headers)
    release.set()
    finished = await first

    assert finished.status_code == 200, finished.text
    assert rejected.status_code == expected_status, rejected.text
    assert int(rejected.headers["Retry-After"]) > 0
    assert "稍后" in rejected.json()["message"]


async def test_slot_is_returned_after_each_turn_even_when_rejected_by_lease(
    client: AsyncClient,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同会话连发被租约 409 拒绝时，那次请求占的名额也要还回去。"""
    monkeypatch.setattr(
        agent_sessions_api, "turn_admission", TurnAdmission(max_total=5, max_per_user=2)
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_turn(*args: Any, **kwargs: Any) -> LoopOutcome:
        entered.set()
        await release.wait()
        return LoopOutcome(reason="final_answer", final_answer="ok")

    monkeypatch.setattr(agent_sessions_api, "run_chat_turn", slow_turn)
    session_id = await _new_session(client, auth_headers)

    first = asyncio.create_task(_post(client, session_id, auth_headers))
    await entered.wait()
    duplicate = await _post(client, session_id, auth_headers)
    release.set()
    await first

    assert duplicate.status_code == 409
    for _ in range(3):
        again = await _post(client, session_id, auth_headers)
        assert again.status_code == 200, again.text
