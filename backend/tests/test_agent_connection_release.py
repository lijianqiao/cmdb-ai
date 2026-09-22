"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_connection_release.py
@DateTime: 2026-09-22
@Docs: R5：等模型、等工具、等子 Agent 时，Agent 不占数据库连接。

实现流程：
1. 用临时文件 SQLite + 普通连接池，pool.checkedout() 就是「此刻借出去的连接数」。
   内存库的 StaticPool 让所有会话共用一条连接，多个会话同时打开时计数不可靠，这里不用它。
2. 模型/工具替身在被调用的那一刻记下借出数，断言为 0：说明循环在调用外部服务之前
   已经关掉了读历史、查权限用的短会话。连接池小的时候，这正是登录、审批还能用的前提。
3. 本轮的 assistant/tool 消息留在内存，整轮结束才一次写库：跑到一半时库里只有用户消息，
   出错或被取消时本轮输出自然不留痕迹（与原来整轮回滚的语义一致）。
4. 压缩：读输入 → 不带会话调摘要模型 → 短事务保存；保存时发现边界已被别人推进就放弃。
5. 最终写库前校验 turn 租约令牌：被接管的旧轮次不能把消息写进新一轮。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agent import chat_turn as chat_turn_module
from app.agent import compaction as compaction_module
from app.agent import device_result_summary as device_result_summary_module
from app.agent import knowledge_tools as knowledge_tools_module
from app.agent.budget import Budget
from app.agent.chat_turn import TurnSupersededError, run_chat_turn
from app.agent.compaction import ensure_root_compaction
from app.agent.device_result_summary import deliver_device_query_summary
from app.agent.executors import ExecutionResult
from app.agent.hitl_execution import execute_approved_proposal
from app.agent.hitl_executor import deliver_executed_query_summary
from app.agent.loop import ToolResult, run_loop
from app.agent.session import append_user_message
from app.agent.spawn import SpawnManager
from app.agent.tool_dispatch import build_tool_dispatcher
from app.agent.ws_hub import AgentWsHub
from app.core import llm as llm_module
from app.core.database import get_db
from app.core.llm import ChatMessage, ChatResult, EmbeddingResult, ToolCall
from app.core.security import hash_password
from app.crud.agent_message import agent_message_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_execution_result import hitl_execution_result_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.main import app
from app.models import Base
from app.models.agent_session import AgentSession
from app.models.user import User

pytestmark = pytest.mark.asyncio

_PASSWORD = "r5-password-123"


@dataclass(slots=True)
class PooledDatabase:
    """带真实连接池的临时库；user 是超管，业务权限不会挡住工具调用。"""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    user_id: int
    username: str
    session_id: int

    def checked_out(self) -> int:
        """此刻从连接池借出、尚未归还的连接数。"""
        return int(self.engine.pool.checkedout())  # type: ignore[attr-defined]

    async def roles(self, *, agent_id: str | None = None) -> list[str]:
        async with self.session_factory() as db:
            rows = await agent_message_crud.list_for_agent(
                db, self.session_id, agent_id=agent_id
            )
        return [row.role for row in rows]


@pytest_asyncio.fixture
async def pooled_db(tmp_path: Path) -> AsyncIterator[PooledDatabase]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r5.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with session_factory() as db:
        user = User(
            username="r5-user",
            email="r5@example.com",
            hashed_password=hash_password(_PASSWORD),
            nickname="R5",
            is_active=True,
            is_superuser=True,
        )
        db.add(user)
        await db.flush()
        session = AgentSession(user_id=user.id, title="r5", status="active")
        db.add(session)
        await db.flush()
        await append_user_message(db, session.id, "10.0.0.5 在线吗")
        await db.commit()
        user_id, session_id = user.id, session.id
    try:
        yield PooledDatabase(engine, session_factory, user_id, "r5-user", session_id)
    finally:
        await engine.dispose()


async def _read_config_like_real_chat(model_key: str, db: Any) -> None:
    """真正的 llm.chat 发请求前会先用传进来的 db 读模型配置；替身照做，
    才测得出「读完配置没还连接、就去等模型」。"""
    await llm_module._resolve_model_config(model_key, db)


def _final(content: str) -> ChatResult:
    return ChatResult(
        content=content, tool_calls=[], finish_reason="stop", prompt_tokens=1, completion_tokens=1
    )


def _tool_call(name: str, arguments: str) -> ChatResult:
    return ChatResult(
        content=None,
        tool_calls=[ToolCall(id="call_1", name=name, arguments=arguments)],
        finish_reason="tool_calls",
        prompt_tokens=1,
        completion_tokens=1,
    )


async def test_root_loop_holds_no_connection_while_waiting_for_model_or_tools(
    pooled_db: PooledDatabase,
) -> None:
    at_model: list[int] = []
    at_tool: list[int] = []
    committed_mid_turn: list[list[str]] = []

    async def fake_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        at_model.append(pooled_db.checked_out())
        committed_mid_turn.append(await pooled_db.roles())
        if len(at_model) == 1:
            return _tool_call("query_monitor_status", "{}")
        # 上一步的 assistant/tool 来自内存，照样出现在模型历史里
        assert [message.role for message in messages] == ["system", "user", "assistant", "tool"]
        return _final("在线")

    async def fake_dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        at_tool.append(pooled_db.checked_out())
        return ToolResult(control="ok", content="10.0.0.5 状态: up")

    outcome = await run_loop(
        pooled_db.session_factory,
        session_id=pooled_db.session_id,
        model_key="fake",
        dispatch_tool=fake_dispatch,
        chat_fn=fake_chat,
        system_prompt="你是运维助手",
    )

    assert outcome.reason == "final_answer"
    assert at_model == [0, 0]
    assert at_tool == [0]
    # 跑的过程中本轮消息只在内存里，库里一直只有用户消息；结束时一次写入
    assert committed_mid_turn == [["user"], ["user"]]
    assert await pooled_db.roles() == ["user", "assistant", "tool", "assistant"]
    assert pooled_db.checked_out() == 0


async def test_failed_turn_leaves_no_partial_output(pooled_db: PooledDatabase) -> None:
    """出错（被取消同理）时内存里的本轮输出直接丢弃，不会留下半截工具调用。"""
    calls = {"n": 0}

    async def failing_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return _tool_call("query_monitor_status", "{}")
        raise RuntimeError("model crashed")

    async def fake_dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(control="ok", content="up")

    with pytest.raises(RuntimeError):
        await run_loop(
            pooled_db.session_factory,
            session_id=pooled_db.session_id,
            model_key="fake",
            dispatch_tool=fake_dispatch,
            chat_fn=failing_chat,
        )

    assert await pooled_db.roles() == ["user"]
    assert pooled_db.checked_out() == 0


async def test_chat_turn_real_tools_use_their_own_short_sessions(
    pooled_db: PooledDatabase,
) -> None:
    async with pooled_db.session_factory() as db:
        await cmdb_asset_crud.create(
            db, {"asset_type": "switch", "hostname": "SW-R5-01", "ip_address": "10.5.0.1"}
        )
        await db.commit()
    at_model: list[int] = []

    async def fake_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        at_model.append(pooled_db.checked_out())
        if len(at_model) == 1:
            return _tool_call("query_cmdb", '{"ip": "10.5.0.1"}')
        assert "SW-R5-01" in (messages[-1].content or "")
        return _final("查到了 SW-R5-01")

    outcome = await run_chat_turn(
        pooled_db.session_factory,
        session_id=pooled_db.session_id,
        actor_user_id=pooled_db.user_id,
        chat_fn=fake_chat,
        hub_instance=AgentWsHub(),
    )

    assert outcome.reason == "final_answer"
    assert at_model == [0, 0]
    assert await pooled_db.roles() == ["user", "assistant", "tool", "assistant"]
    assert pooled_db.checked_out() == 0


async def test_child_agent_holds_no_connection_while_waiting_for_model(
    pooled_db: PooledDatabase,
) -> None:
    at_model: list[int] = []

    async def fake_chat(
        model_key: str,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        at_model.append(pooled_db.checked_out())
        return _final("文档里没有相关内容")

    manager = SpawnManager(pooled_db.session_factory, chat_fn=fake_chat)
    receipt = await manager.spawn_agent(
        session_id=pooled_db.session_id, role="kb_explorer", task_brief="查一下备份策略"
    )
    finished = await manager.wait_agent(receipt.child_id, timeout_ms=5000)

    assert finished.status == "COMPLETED"
    assert at_model == [0]
    assert await pooled_db.roles(agent_id=receipt.child_id) == ["user", "assistant"]
    await manager.shutdown()
    assert pooled_db.checked_out() == 0


async def _seed_long_history(pooled_db: PooledDatabase) -> None:
    async with pooled_db.session_factory() as db:
        for index in range(30):
            await append_user_message(db, pooled_db.session_id, f"巡检记录{index}：" + "端口正常" * 600)
        await db.commit()


async def test_compaction_calls_summarizer_without_holding_a_connection(
    pooled_db: PooledDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_long_history(pooled_db)
    at_model: list[int] = []

    async def fake_summarizer(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        at_model.append(pooled_db.checked_out())
        return _final("早期巡检都正常")

    monkeypatch.setattr(compaction_module, "chat", fake_summarizer)

    await ensure_root_compaction(
        pooled_db.session_factory, pooled_db.session_id, budget=Budget(), system_prompt=""
    )

    assert at_model == [0]
    async with pooled_db.session_factory() as db:
        session = await db.get(AgentSession, pooled_db.session_id)
    assert session is not None
    assert session.memory_summary == "早期巡检都正常"
    assert session.compacted_through_message_id is not None
    assert pooled_db.checked_out() == 0


async def test_late_compaction_result_does_not_overwrite_newer_summary(
    pooled_db: PooledDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """摘要模型慢的时候，别的请求可能已经推进了压缩边界；迟到的结果不能把它盖回去。"""
    await _seed_long_history(pooled_db)

    async def racing_summarizer(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        async with pooled_db.session_factory() as other:
            session = await other.get(AgentSession, pooled_db.session_id)
            assert session is not None
            session.memory_summary = "更新的摘要"
            session.compacted_through_message_id = 5
            await other.commit()
        return _final("迟到的摘要")

    monkeypatch.setattr(compaction_module, "chat", racing_summarizer)

    await ensure_root_compaction(
        pooled_db.session_factory, pooled_db.session_id, budget=Budget(), system_prompt=""
    )

    async with pooled_db.session_factory() as db:
        session = await db.get(AgentSession, pooled_db.session_id)
    assert session is not None
    assert session.memory_summary == "更新的摘要"
    assert session.compacted_through_message_id == 5


async def test_semantic_search_releases_connection_before_calling_embedding_model(
    pooled_db: PooledDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """调度器先查权限、工具再调向量模型：查权限用的会话必须在调模型之前关掉。"""
    at_embedding: list[int] = []

    async def fake_embed(model_key: str, texts: list[str], **kwargs: Any) -> EmbeddingResult:
        at_embedding.append(pooled_db.checked_out())
        return EmbeddingResult(vectors=[[0.1, 0.2]], prompt_tokens=1)

    async def fake_search(db: AsyncSession, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(knowledge_tools_module, "embed", fake_embed)
    monkeypatch.setattr(knowledge_tools_module.knowledge_chunk_crud, "search_similar", fake_search)
    dispatch = build_tool_dispatcher(
        pooled_db.session_factory, ("kb_semantic_search",), user_id=pooled_db.user_id
    )

    result = await dispatch("kb_semantic_search", {"query": "备份策略"})

    assert result.control == "ok", result.content
    assert at_embedding == [0]
    assert pooled_db.checked_out() == 0


async def test_superseded_turn_does_not_write_into_the_new_turn(
    pooled_db: PooledDatabase,
) -> None:
    """本轮卡住期间租约超时、被新一轮接管：旧轮次结束时不能再把消息写进去。"""
    async with pooled_db.session_factory() as db:
        await db.execute(
            update(AgentSession)
            .where(AgentSession.id == pooled_db.session_id)
            .values(active_turn_token="turn-old")
        )
        await db.commit()

    async def stalled_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        async with pooled_db.session_factory() as other:
            await other.execute(
                update(AgentSession)
                .where(AgentSession.id == pooled_db.session_id)
                .values(active_turn_token="turn-new")
            )
            await other.commit()
        return _final("迟到的回答")

    with pytest.raises(TurnSupersededError):
        await run_chat_turn(
            pooled_db.session_factory,
            session_id=pooled_db.session_id,
            actor_user_id=pooled_db.user_id,
            chat_fn=stalled_chat,
            hub_instance=AgentWsHub(),
            turn_token="turn-old",
        )

    assert await pooled_db.roles() == ["user"]


async def test_message_endpoint_holds_no_connection_while_model_runs(
    pooled_db: PooledDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """从 HTTP 入口走一遍：请求自己的会话也要在等模型之前归还连接。"""

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with pooled_db.session_factory() as request_session:
            try:
                yield request_session
            finally:
                if request_session.in_transaction():
                    await request_session.rollback()

    at_model: list[int] = []

    async def fake_default_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        await _read_config_like_real_chat(model_key, kwargs["db"])
        at_model.append(pooled_db.checked_out())
        return _final("在线")

    monkeypatch.setattr(chat_turn_module, "chat", fake_default_chat)
    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Sec-Fetch-Site": "same-origin"},
        ) as client:
            login = await client.post(
                "/api/v1/auth/login",
                data={"username": pooled_db.username, "password": _PASSWORD},
            )
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

            response = await client.post(
                f"/api/v1/agent/sessions/{pooled_db.session_id}/messages",
                json={"content": "再确认一次"},
                headers=headers,
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert at_model == [0]
    assert await pooled_db.roles() == ["user", "user", "assistant"]


async def _executed_device_query(pooled_db: PooledDatabase, *, status: str) -> int:
    async with pooled_db.session_factory() as db:
        asset = await cmdb_asset_crud.create(
            db,
            {
                "asset_type": "switch",
                "hostname": "SW-R5-02",
                "ip_address": "10.5.0.2",
                "vendor": "cisco_iosxe",
                "credential_type": "static",
                "credential_username": "admin",
                "credential_password_encrypted": "placeholder",
            },
        )
        proposal = await hitl_proposal_crud.create(
            db,
            session_id=pooled_db.session_id,
            proposed_by_agent_id=None,
            action_type="device_query",
            action_payload={"asset_id": asset.id, "command_name": "show_version"},
        )
        proposal.status = status
        await db.flush()
        if status == "EXECUTED":
            await hitl_execution_result_crud.create_for_proposal(
                db, proposal_id=proposal.id, content="Cisco IOS XE Software, Version 17.9"
            )
        await db.commit()
        return proposal.id


async def test_device_execution_holds_no_connection_while_device_runs(
    pooled_db: PooledDatabase,
) -> None:
    """设备命令最长要等几十秒（连接超时 + 读超时），这期间不能占着连接。"""
    proposal_id = await _executed_device_query(pooled_db, status="APPROVED")
    at_device: list[int] = []

    class FakeDeviceExecutor:
        async def execute(self, db: AsyncSession, **kwargs: Any) -> ExecutionResult:
            at_device.append(pooled_db.checked_out())
            return ExecutionResult(ok=True, message="ok", detail={"output": "IOS XE", "truncated": False})

    summary = await execute_approved_proposal(
        session_factory=pooled_db.session_factory,
        proposal_id=proposal_id,
        actor_user_id=pooled_db.user_id,
        device_executor=FakeDeviceExecutor(),
    )

    assert summary.status == "EXECUTED"
    assert at_device == [0]
    assert pooled_db.checked_out() == 0


async def test_device_result_summary_holds_no_connection_while_model_runs(
    pooled_db: PooledDatabase,
) -> None:
    proposal_id = await _executed_device_query(pooled_db, status="EXECUTED")
    at_model: list[int] = []

    async def fake_summary_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        await _read_config_like_real_chat(model_key, kwargs["db"])
        at_model.append(pooled_db.checked_out())
        return _final("设备型号 C9300，版本 17.9。可在审批卡片查看原文。")

    delivery = await deliver_device_query_summary(
        session_factory=pooled_db.session_factory,
        proposal_id=proposal_id,
        chat_fn=fake_summary_chat,
    )

    assert delivery.created_message
    assert at_model == [0]
    assert pooled_db.checked_out() == 0


async def test_approval_summary_delivery_holds_no_connection_while_model_runs(
    pooled_db: PooledDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """人工批准后由后台任务交付总结：查提案、生成总结都不能在等总结模型时占着连接。"""
    proposal_id = await _executed_device_query(pooled_db, status="EXECUTED")
    at_model: list[int] = []

    async def fake_summary_chat(
        model_key: str, messages: list[ChatMessage], **kwargs: Any
    ) -> ChatResult:
        await _read_config_like_real_chat(model_key, kwargs["db"])
        at_model.append(pooled_db.checked_out())
        return _final("设备型号 C9300，版本 17.9。可在审批卡片查看原文。")

    monkeypatch.setattr(device_result_summary_module, "chat", fake_summary_chat)
    delivery = await deliver_executed_query_summary(pooled_db.session_factory, proposal_id)

    assert delivery is not None and delivery.created_message
    assert at_model == [0]
