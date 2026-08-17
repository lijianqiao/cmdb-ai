"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_turn_cancellation.py
@DateTime: 2026-08-17
@Docs: 验证「撤回请求」：中止正在跑的一轮对话、丢弃本轮产出、立即释放租约。

覆盖的关键行为：
1. 取消后 HTTP 返回 200 且 reason="cancelled"，租约立刻可被下一条消息抢到。
2. C2 语义：本轮未提交的助手消息全部丢弃，用户自己那条提问保留。
3. 幂等：没有正在跑的 turn 时返回 cancelled=false 而不是报错。
4. 归属校验：非所有者取消别人的会话返回 404。
5. **非用户发起的取消必须原样抛出**——客户端断开、进程关停时吞掉 CancelledError
   会让关停挂住，这是 asyncio 里最容易写错的一处。
6. 重复取消只真正 cancel 一次：HITL 执行在 except CancelledError 里还要写一次
   数据库把提案置成 UNKNOWN，再 cancel 一遍会把那次写入也打断。
"""

import asyncio

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import LoopOutcome
from app.agent.session import append_assistant_message
from app.agent.turn_registry import TurnRegistry
from app.api.v1 import agent_sessions as agent_sessions_api
from app.core.security import hash_password
from app.crud.agent_session import agent_session_crud
from app.models.permission import Permission
from app.models.role import Role, role_permissions
from app.models.user import User
from app.schemas.agent_session import AgentMessageCreate

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]


@pytest_asyncio.fixture(autouse=True)
async def _grant_agent_use(db_session: AsyncSession, test_role: Role) -> None:
    """给 test_role 挂上 agent:use（与 test_agent_sessions_api.py 同一模式）。"""
    permission = Permission(name="使用运维助手", code="agent:use", module="Agent")
    db_session.add(permission)
    await db_session.flush()
    await db_session.execute(
        role_permissions.insert().values(role_id=test_role.id, permission_id=permission.id)
    )
    await db_session.commit()


async def _wait_entered(entered: asyncio.Event, turn: asyncio.Task) -> None:
    """等 turn 真正跑起来；turn 先结束就说明它挂了，直接把结果暴露出来。

    单纯 `await entered.wait()` 在 turn 内部抛异常时会永久挂住，看不到原因。
    """
    waiter = asyncio.ensure_future(entered.wait())
    done, _ = await asyncio.wait({waiter, turn}, return_when=asyncio.FIRST_COMPLETED)
    if waiter not in done:
        waiter.cancel()
        raise AssertionError(f"turn 在进入前就结束了：{turn.result()!r}")


@pytest_asyncio.fixture
async def session_id(client: AsyncClient, auth_headers: Headers) -> int:
    """创建一条测试会话并返回其 ID。"""
    create_resp = await client.post(
        "/api/v1/agent/sessions",
        json={"title": "取消测试"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    return int(create_resp.json()["data"]["id"])


async def test_cancel_stops_turn_discards_output_and_frees_lease(
    client: AsyncClient,
    auth_headers: Headers,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消后：200 + cancelled、本轮助手消息被丢弃、租约立即可再用。"""
    entered = asyncio.Event()

    async def slow_turn(db, *, session_id: int, **kwargs):
        # 模拟真实 turn：先写一条未提交的助手消息，再停在一个可被取消的 await 上
        await append_assistant_message(db, session_id, "只写了一半的回答")
        entered.set()
        await asyncio.Event().wait()
        return LoopOutcome(reason="final_answer", final_answer="不会走到这里")

    monkeypatch.setattr(agent_sessions_api, "run_chat_turn", slow_turn)

    turn = asyncio.create_task(
        client.post(
            f"/api/v1/agent/sessions/{session_id}/messages",
            json={"content": "帮我查一下核心交换机"},
            headers=auth_headers,
        )
    )
    await _wait_entered(entered, turn)

    cancel_resp = await client.post(
        f"/api/v1/agent/sessions/{session_id}/turn/cancel",
        headers=auth_headers,
    )
    assert cancel_resp.status_code == 200, cancel_resp.text
    assert cancel_resp.json()["data"]["cancelled"] is True

    turn_resp = await turn
    assert turn_resp.status_code == 200, turn_resp.text
    assert turn_resp.json()["data"]["reason"] == "cancelled"
    assert turn_resp.json()["data"]["final_answer"] is None

    # C2：助手消息全部丢弃，只剩用户自己那条提问
    history = await client.get(
        f"/api/v1/agent/sessions/{session_id}/messages",
        headers=auth_headers,
    )
    assert history.status_code == 200, history.text
    roles = [message["role"] for message in history.json()["data"]]
    assert roles == ["user"]

    # 租约已释放：下一条消息不该撞 409
    monkeypatch.setattr(
        agent_sessions_api,
        "run_chat_turn",
        lambda *a, **k: _immediate_outcome(),
    )
    again = await client.post(
        f"/api/v1/agent/sessions/{session_id}/messages",
        json={"content": "再问一次"},
        headers=auth_headers,
    )
    assert again.status_code == 200, again.text


async def _immediate_outcome() -> LoopOutcome:
    """立刻结束的 turn，用于验证租约已经释放。"""
    return LoopOutcome(reason="final_answer", final_answer="好的")


async def test_cancel_without_running_turn_is_idempotent(
    client: AsyncClient,
    auth_headers: Headers,
    session_id: int,
) -> None:
    """没有正在跑的 turn 时返回 200 + cancelled=false，连点两次不报错。"""
    response = await client.post(
        f"/api/v1/agent/sessions/{session_id}/turn/cancel",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cancelled"] is False


async def test_cancel_other_users_session_returns_404(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    test_role: Role,
    login_user,
) -> None:
    """非所有者取消别人的会话应 404，避免枚举他人会话 ID。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "我的会话", "status": "active"},
    )
    other = User(
        username="other_cancel_user",
        email="other_cancel@example.com",
        hashed_password=hash_password("testpassword123"),
        nickname="其他用户",
        is_active=True,
        is_superuser=False,
        roles=[test_role],
    )
    db_session.add(other)
    await db_session.commit()

    other_headers = await login_user(other.username, "testpassword123")
    response = await client.post(
        f"/api/v1/agent/sessions/{session.id}/turn/cancel",
        headers=other_headers,
    )
    assert response.status_code == 404, response.text


async def test_non_user_cancellation_propagates(
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不是用户发起的取消（客户端断开 / 进程关停）必须原样抛出。

    吞掉这种 CancelledError 会让进程关停挂住，或者留下一个永远返回不了的请求。
    这里直接 cancel 注册表里的 task 而不经过取消端点，正是那个场景。

    **不走 HTTP 客户端**：从外部打断一个进行中的 ASGI 请求会让依赖注入的 teardown
    也一起被取消，那条数据库连接就再也回不了连接池；测试用的 StaticPool 只有一条
    连接，它的终结器之后会在随便哪个测试里炸出来。直接 await 这个端点协程能测到
    同一个分支，又不会留下一个没人收尾的请求。
    """
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "会被外部打断", "status": "active"},
    )
    await db_session.commit()

    entered = asyncio.Event()

    async def slow_turn(db, *, session_id: int, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        return LoopOutcome(reason="final_answer", final_answer="不会走到这里")

    monkeypatch.setattr(agent_sessions_api, "run_chat_turn", slow_turn)

    turn = asyncio.create_task(
        agent_sessions_api.post_session_message(
            session.id,
            AgentMessageCreate(content="会被外部打断"),
            db=db_session,
            current_user=test_user,
        )
    )
    await _wait_entered(entered, turn)

    # 绕过取消端点，所以 cancel_requested_by 保持 None
    running = agent_sessions_api.turn_registry._running[session.id]
    running.task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await turn


async def test_registry_cancels_task_only_once() -> None:
    """重复取消不重复 cancel：HITL 执行要在 except CancelledError 里写完 UNKNOWN。"""
    registry = TurnRegistry()
    cancel_calls = 0

    class _FakeTask:
        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            nonlocal cancel_calls
            cancel_calls += 1

    registry.register(1, "token-a", _FakeTask())  # type: ignore[arg-type]

    first = registry.request_cancel(1, by_user_id=7)
    second = registry.request_cancel(1, by_user_id=7)

    assert first.cancelled is True
    assert second.cancelled is True
    assert cancel_calls == 1


async def test_registry_unregister_ignores_foreign_token() -> None:
    """token 不匹配时不删条目，避免上一轮的 finally 误删下一轮的登记。"""
    registry = TurnRegistry()

    class _FakeTask:
        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            pass

    registry.register(1, "token-a", _FakeTask())  # type: ignore[arg-type]
    registry.unregister(1, "token-b")
    assert registry.request_cancel(1, by_user_id=7).cancelled is True

    registry.unregister(1, "token-a")
    assert registry.request_cancel(1, by_user_id=7).cancelled is False
