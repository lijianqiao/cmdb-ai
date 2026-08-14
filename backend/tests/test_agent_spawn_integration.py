"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_spawn_integration.py
@DateTime: 2026-08-12
@Docs: T09 跨组件不变量验收：真实 SpawnManager + 默认 runner + fake ChatFn。

实现流程：
1. 用临时 SQLite 与真实 ORM / SpawnManager / 默认 child runner，注入确定性 fake ChatFn。
2. 批量归类：6 份文档分两波，用 asyncio.Event 观察并发上限，低置信触发 reviewer。
3. 根因排查：三分支中一失败两成功，reviewer 综合；二次 close 证明幂等。
4. 断言 registry CLOSED、活跃槽清空、root/child/sibling 消息隔离、trace 与预算落盘。
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agent.budget import Budget
from app.agent.orchestration import (
    ClassificationDocument,
    RootCauseBranch,
    classify_documents,
    investigate_root_cause,
)
from app.agent.spawn import ChildReceipt, ChildRunResult, SpawnManager
from app.core.llm import ChatMessage, ChatResult, LlmRequestError
from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_trace_event import agent_trace_event_crud
from app.models import Base
from app.models.agent_session import AgentSession
from app.models.agent_trace_event import AgentTraceEvent
from app.models.user import User

_DOCUMENT_ID_RE = re.compile(r"document_id=(\d+)")
_BRANCH_RE = re.compile(r"分支:\s*(\S+)")
_PATH_RE = re.compile(r"路径:\s*(.+)")


@dataclass(slots=True)
class IntegrationDatabase:
    """独立 SQLite 连接池，供并发 child 会话使用。"""

    session_factory: async_sessionmaker[AsyncSession]
    session_id: int


@dataclass
class GatedChatRecorder:
    """记录每次模型历史，并用事件门闩观察分类器并发。"""

    histories: list[list[ChatMessage]] = field(default_factory=list)
    classifier_running: int = 0
    max_classifier_running: int = 0
    five_classifiers_running: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    fail_branch: str | None = None

    async def __call__(
        self,
        model_key: str,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResult:
        del model_key, tools
        snapshot = list(messages)
        self.histories.append(snapshot)
        user = next(message.content for message in snapshot if message.role == "user")

        if "只处理这一份文档" in user:
            async with self._lock:
                self.classifier_running += 1
                self.max_classifier_running = max(
                    self.max_classifier_running, self.classifier_running
                )
                if self.classifier_running == 5:
                    self.five_classifiers_running.set()
            await self.five_classifiers_running.wait()
            await asyncio.sleep(0)
            match = _DOCUMENT_ID_RE.search(user)
            assert match is not None
            document_id = int(match.group(1))
            confidence = 0.5 if document_id == 1 else 0.95
            payload = {
                "document_id": document_id,
                "recommended_category": "sop",
                "confidence": confidence,
                "needs_review": False,
                "reason": f"integration-doc-{document_id}",
            }
            async with self._lock:
                self.classifier_running -= 1
            return ChatResult(
                content=json.dumps(payload, ensure_ascii=False),
                tool_calls=[],
                finish_reason="stop",
                prompt_tokens=8,
                completion_tokens=12,
                cost_usd=0.02,
            )

        if "批量文档分类的结果摘要" in user:
            accepted = [
                int(match.group(1))
                for line in user.splitlines()
                if (match := _DOCUMENT_ID_RE.search(line)) is not None
            ]
            payload = {
                "summary": "低置信条目已复核",
                "accepted_document_ids": accepted,
                "disputed_document_ids": [1],
                "recommended_actions": ["人工确认 document_id=1"],
            }
            return ChatResult(
                content=json.dumps(payload, ensure_ascii=False),
                tool_calls=[],
                finish_reason="stop",
                prompt_tokens=6,
                completion_tokens=10,
                cost_usd=0.03,
            )

        if "事故背景:" in user and "分支:" in user and "目标:" in user:
            branch_match = _BRANCH_RE.search(user)
            assert branch_match is not None
            branch = branch_match.group(1)
            if self.fail_branch is not None and branch == self.fail_branch:
                raise LlmRequestError("injected investigator failure")
            payload = {
                "branch": branch,
                "hypothesis": f"{branch} hypothesis",
                "confidence": 0.7,
                "evidence": [f"{branch}-evidence"],
                "gaps": ["变更日志不可用，已记为缺口"],
                "next_checks": [f"复测 {branch}"],
            }
            return ChatResult(
                content=json.dumps(payload, ensure_ascii=False),
                tool_calls=[],
                finish_reason="stop",
                prompt_tokens=7,
                completion_tokens=11,
                cost_usd=0.025,
            )

        if "各根因排查分支的发现" in user:
            payload = {
                "summary": "部分分支失败，综合其余证据",
                "likely_causes": ["拓扑依赖异常"],
                "evidence_gaps": ["monitor_history 分支失败", "变更日志不可用"],
                "recommended_next_steps": ["人工复核失败分支"],
            }
            return ChatResult(
                content=json.dumps(payload, ensure_ascii=False),
                tool_calls=[],
                finish_reason="stop",
                prompt_tokens=5,
                completion_tokens=9,
                cost_usd=0.04,
            )

        raise AssertionError(f"unexpected chat task brief: {user[:200]}")


@pytest_asyncio.fixture
async def integration_db(tmp_path: Path) -> AsyncIterator[IntegrationDatabase]:
    """创建可供并发 child 使用的独立 SQLite 库。"""
    database_path = tmp_path / "spawn-integration.sqlite3"
    engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as db:
        user = User(
            username="integration-user",
            email="integration@example.com",
            hashed_password="not-used",
            nickname="Integration",
        )
        db.add(user)
        await db.flush()
        session = AgentSession(user_id=user.id, title="integration", status="active")
        db.add(session)
        await db.commit()
        session_id = session.id

    try:
        yield IntegrationDatabase(session_factory=session_factory, session_id=session_id)
    finally:
        await engine.dispose()


async def test_batch_classification_wave_invariants(
    integration_db: IntegrationDatabase,
) -> None:
    """六文档分波归类：并发不超过 5，低置信触发 reviewer，槽位与消息隔离。"""
    recorder = GatedChatRecorder()
    manager = SpawnManager(integration_db.session_factory, chat_fn=recorder)
    documents = [
        ClassificationDocument(
            document_id=index,
            title=f"Doc {index}",
            file_path=f"knowledge/sop/doc_{index}.md",
            current_category=None,
        )
        for index in range(1, 7)
    ]

    outcome = await classify_documents(
        manager,
        session_id=integration_db.session_id,
        documents=documents,
        allowed_categories=("sop", "runbook"),
    )

    assert len(outcome.suggestions) == 6
    assert outcome.review is not None
    assert outcome.review.disputed_document_ids == [1]
    assert outcome.workflow_failure is None
    assert recorder.max_classifier_running == 5
    assert recorder.five_classifiers_running.is_set()

    receipts = await manager.list_agents(integration_db.session_id)
    assert len(receipts) == 7
    assert all(receipt.status == "CLOSED" for receipt in receipts)
    reviewer = next(receipt for receipt in receipts if receipt.role == "reviewer")
    assert reviewer.parent_agent_id in {
        receipt.child_id for receipt in receipts if receipt.role == "classifier"
    }
    assert reviewer.agent_path.count("/") == 3

    async with integration_db.session_factory() as db:
        assert await agent_registry_crud.list_active_children(db, integration_db.session_id) == []
        root_messages = await agent_message_crud.list_for_agent(
            db, integration_db.session_id, agent_id=None
        )
        root_text = "\n".join(message.content for message in root_messages)
        assert "只处理这一份文档" not in root_text
        assert "document_id=" not in root_text
        assert "integration-doc-" not in root_text

        classifier_receipts = [receipt for receipt in receipts if receipt.role == "classifier"]
        assert len(classifier_receipts) == 6
        sibling_paths = {
            path
            for receipt in classifier_receipts
            if (path := next(
                (
                    match.group(1).strip()
                    for match in [_PATH_RE.search(receipt.task_brief)]
                    if match is not None
                ),
                None,
            ))
            is not None
        }
        assert len(sibling_paths) == 6

        for receipt in classifier_receipts:
            child_messages = await agent_message_crud.list_for_agent(
                db, integration_db.session_id, agent_id=receipt.child_id
            )
            child_text = "\n".join(message.content for message in child_messages)
            assert receipt.task_brief in child_text
            own_path_match = _PATH_RE.search(receipt.task_brief)
            assert own_path_match is not None
            own_path = own_path_match.group(1).strip()
            for sibling_path in sibling_paths:
                if sibling_path != own_path:
                    assert sibling_path not in child_text

            events = [
                event
                for event in await agent_trace_event_crud.list_for_trace(db, receipt.trace_id)
                if event.agent_id == receipt.child_id
            ]
            assert [event.span_type for event in events] == ["spawn", "agent", "close"]
            assert receipt.budget.steps_used >= 1
            assert receipt.budget.cost_used_usd > 0

        # fake ChatFn 捕获的历史同样证明 sibling brief 未串味
        classifier_histories = [
            history
            for history in recorder.histories
            if any(
                message.role == "user" and "只处理这一份文档" in message.content
                for message in history
            )
        ]
        assert len(classifier_histories) == 6
        for history in classifier_histories:
            user = next(message.content for message in history if message.role == "user")
            own_path_match = _PATH_RE.search(user)
            assert own_path_match is not None
            own_path = own_path_match.group(1).strip()
            for sibling_path in sibling_paths:
                if sibling_path != own_path:
                    assert sibling_path not in user


async def test_root_cause_partial_failure_and_idempotent_close(
    integration_db: IntegrationDatabase,
) -> None:
    """一根因分支失败时仍能复核，全部 CLOSED，二次 close 幂等。"""
    recorder = GatedChatRecorder(fail_branch="monitor_history")
    manager = SpawnManager(integration_db.session_factory, chat_fn=recorder)
    branches = (
        RootCauseBranch(name="monitor_history", objective="核对监控翻转时间线"),
        RootCauseBranch(name="cmdb_topology", objective="核对上下游依赖"),
        RootCauseBranch(name="peer_scope", objective="核对同业务范围同伴状态"),
    )

    outcome = await investigate_root_cause(
        manager,
        session_id=integration_db.session_id,
        incident_context="机房 A 网关间歇不可达",
        branches=branches,
    )

    assert len(outcome.findings) == 2
    assert {finding.branch for finding in outcome.findings} == {
        "cmdb_topology",
        "peer_scope",
    }
    assert len(outcome.failed_child_ids) == 1
    assert outcome.review is not None
    assert "monitor_history" in " ".join(outcome.review.evidence_gaps)
    assert outcome.workflow_failure is None

    # reviewer brief 只含结构化发现/失败标记，不含兄弟完整 transcript 废话
    review_histories = [
        history
        for history in recorder.histories
        if any(
            message.role == "user" and "各根因排查分支的发现" in message.content
            for message in history
        )
    ]
    assert len(review_histories) == 1
    review_brief = next(
        message.content for message in review_histories[0] if message.role == "user"
    )
    assert "cmdb_topology hypothesis" in review_brief
    assert "peer_scope hypothesis" in review_brief
    assert "有 1 个调查分支未产出结果或执行失败" in review_brief
    assert "完整 transcript" not in review_brief

    receipts = await manager.list_agents(integration_db.session_id)
    assert len(receipts) == 4
    assert all(receipt.status == "CLOSED" for receipt in receipts)
    failed_receipt = next(
        receipt for receipt in receipts if receipt.child_id in outcome.failed_child_ids
    )
    assert failed_receipt.status == "CLOSED"
    assert failed_receipt.role == "investigator"

    async with integration_db.session_factory() as db:
        assert await agent_registry_crud.list_active_children(db, integration_db.session_id) == []
        trace_count_before = len(
            (
                await db.execute(
                    select(AgentTraceEvent).where(
                        AgentTraceEvent.session_id == integration_db.session_id
                    )
                )
            )
            .scalars()
            .all()
        )

    # 二次幂等 close：不新增 trace，不二次释放槽位（仍可继续 spawn）
    for receipt in receipts:
        closed_again = await manager.close_agent(receipt.child_id)
        assert closed_again.status == "CLOSED"

    async with integration_db.session_factory() as db:
        trace_count_after = len(
            (
                await db.execute(
                    select(AgentTraceEvent).where(
                        AgentTraceEvent.session_id == integration_db.session_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert trace_count_after == trace_count_before

    extra = await manager.spawn_agent(
        session_id=integration_db.session_id,
        role="ops_explorer",
        task_brief="幂等 close 后槽位可复用",
    )
    await manager.wait_agent(extra.child_id)
    await manager.close_agent(extra.child_id)


async def test_spawn_tool_dispatcher_hides_internal_receipt_fields(
    integration_db: IntegrationDatabase,
) -> None:
    """Spawn 工具回执不得向根 Agent 泄露预算、工具白名单或 artifacts。"""
    from app.agent.spawn_tools import build_spawn_tool_dispatcher

    manager = SpawnManager(integration_db.session_factory, child_runner=_completed_runner)
    dispatch = build_spawn_tool_dispatcher(manager, session_id=integration_db.session_id)
    result = await dispatch(
        "spawn_agent",
        {"role": "ops_explorer", "task_brief": "检查资产 42 监控状态"},
    )
    assert result.control == "ok"
    assert "tools_allowlist" not in result.content
    assert "budget" not in result.content
    assert "model" not in result.content
    assert "trace_id" not in result.content
    assert "检查资产 42 监控状态" in result.content
    child_id = re.search(r"child_id:\s*(\S+)", result.content).group(1)
    await dispatch("close_agent", {"child_id": child_id})


async def _completed_runner(
    _db: AsyncSession,
    _receipt: ChildReceipt,
    _budget: Budget,
) -> ChildRunResult:
    return ChildRunResult(status="COMPLETED", result_summary="integration-done")
