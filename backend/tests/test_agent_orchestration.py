"""Tests for the bounded parallel classification/root-cause workflows.

A `FakeSpawnController` test double drives the workflows without any
database or SpawnManager involvement — it records every spawn/wait/close call
in order and lets each test script per-child behavior (result JSON, wait
failure, close failure, spawn failure) purely in-memory, deterministically.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import pytest
from pydantic import ValidationError

from app.agent.orchestration import (
    CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD,
    DEFAULT_ROOT_CAUSE_BRANCHES,
    ClassificationDocument,
    ClassificationResult,
    ClassificationReview,
    InvestigationFinding,
    ReviewSynthesis,
    RootCauseBranch,
    SpawnRequest,
    WorkflowCleanupError,
    classify_documents,
    investigate_root_cause,
)
from app.agent.spawn import ChildBudgetSnapshot, ChildReceipt, SpawnRejectedError


def _make_receipt(
    *,
    child_id: str,
    session_id: int,
    role: str,
    task_brief: str,
    status: str,
    result_summary: str | None = None,
) -> ChildReceipt:
    now = datetime.now(UTC)
    return ChildReceipt(
        child_id=child_id,
        trace_id="trace-1",
        session_id=session_id,
        parent_agent_id=None,
        agent_path=f"root/{child_id}",
        role=role,
        role_version="t09-v1",
        model="local-chat",
        tools_allowlist=(),
        sandbox_mode="read-only",
        task_brief=task_brief,
        budget=ChildBudgetSnapshot(max_steps=20, max_cost_usd=1.0, max_wall_time_seconds=120.0),
        status=status,
        result_summary=result_summary,
        artifacts=(),
        created_at=now,
        status_changed_at=now,
        closed_at=now if status == "CLOSED" else None,
        force_closed=False,
    )


@dataclass
class _ChildScript:
    """Scripted behavior for the Nth spawned child (N = spawn call order)."""

    result_summary: str | None = None
    status: Literal["COMPLETED", "FAILED"] = "COMPLETED"
    spawn_error: bool = False
    wait_error: bool = False
    close_error: bool = False


@dataclass
class FakeSpawnController:
    """Records every call; scripts each spawned child's terminal behavior."""

    scripts: list[_ChildScript]
    max_concurrent_children: int = 5
    spawn_calls: list[SpawnRequest] = field(default_factory=list)
    wait_calls: list[str] = field(default_factory=list)
    close_calls: list[str] = field(default_factory=list)
    close_count_at_spawn: list[int] = field(default_factory=list)
    _receipts: dict[str, ChildReceipt] = field(default_factory=dict)
    _next_index: int = 0

    async def spawn_agent(
        self,
        *,
        session_id: int,
        role: str,
        task_brief: str,
        trace_id: str | None = None,
        parent_agent_id: str | None = None,
        model: str | None = None,
        tools_allowlist: Iterable[str] | None = None,
        budget: ChildBudgetSnapshot | None = None,
        fork_mode: str = "none",
    ) -> ChildReceipt:
        request = SpawnRequest(
            session_id=session_id,
            role=role,
            task_brief=task_brief,
            trace_id=trace_id or "",
            parent_agent_id=parent_agent_id,
            model=model,
            tools_allowlist=tuple(tools_allowlist) if tools_allowlist is not None else None,
            budget=budget,
        )
        self.spawn_calls.append(request)
        self.close_count_at_spawn.append(len(self.close_calls))
        index = self._next_index
        self._next_index += 1
        script = self.scripts[index]
        if script.spawn_error:
            raise SpawnRejectedError("fake_spawn_rejected")
        child_id = f"child-{index}"
        receipt = _make_receipt(
            child_id=child_id, session_id=session_id, role=role, task_brief=task_brief, status="RUNNING"
        )
        self._receipts[child_id] = receipt
        return receipt

    async def wait_agent(self, child_id: str, *, timeout_ms: int | None = None) -> ChildReceipt:
        self.wait_calls.append(child_id)
        index = int(child_id.removeprefix("child-"))
        script = self.scripts[index]
        if script.wait_error:
            raise RuntimeError("fake wait failure")
        receipt = self._receipts[child_id]
        terminal = _make_receipt(
            child_id=child_id,
            session_id=receipt.session_id,
            role=receipt.role,
            task_brief=receipt.task_brief,
            status=script.status,
            result_summary=script.result_summary,
        )
        self._receipts[child_id] = terminal
        return terminal

    async def close_agent(self, child_id: str) -> ChildReceipt:
        index = int(child_id.removeprefix("child-"))
        script = self.scripts[index]
        if script.close_error:
            raise RuntimeError("fake close failure")
        self.close_calls.append(child_id)
        receipt = self._receipts[child_id]
        closed = _make_receipt(
            child_id=child_id,
            session_id=receipt.session_id,
            role=receipt.role,
            task_brief=receipt.task_brief,
            status="CLOSED",
            result_summary=receipt.result_summary,
        )
        self._receipts[child_id] = closed
        return closed


def _classification_json(document_id: int, *, confidence: float = 0.95, needs_review: bool = False,
                          category: str = "网络") -> str:
    return (
        f'{{"document_id":{document_id},"recommended_category":"{category}",'
        f'"confidence":{confidence},"needs_review":{str(needs_review).lower()},"reason":"证据充分"}}'
    )


def _review_json() -> str:
    return '{"summary":"复核完成","accepted_document_ids":[1],"disputed_document_ids":[],"recommended_actions":[]}'


def _finding_json(branch: str) -> str:
    return (
        f'{{"branch":"{branch}","hypothesis":"网络抖动","confidence":0.6,'
        '"evidence":["探测记录"],"gaps":["缺少变更日志"],"next_checks":["复查拓扑"]}'
    )


def _synthesis_json() -> str:
    return '{"summary":"综合结论","likely_causes":["网络抖动"],"evidence_gaps":["变更日志"],"recommended_next_steps":["观察"]}'


# ---------------------------------------------------------------------------
# Strict-schema parse tests
# ---------------------------------------------------------------------------


def test_classification_result_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate_json(
            '{"document_id":1,"recommended_category":"网络","confidence":0.9,'
            '"needs_review":false,"reason":"ok","extra":"nope"}'
        )


def test_classification_result_rejects_numeric_string_confidence() -> None:
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate_json(
            '{"document_id":1,"recommended_category":"网络","confidence":"0.9",'
            '"needs_review":false,"reason":"ok"}'
        )


def test_classification_result_rejects_missing_keys() -> None:
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate_json('{"document_id":1,"recommended_category":"网络"}')


def test_classification_result_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate_json(
            '{"document_id":1,"recommended_category":"网络","confidence":1.5,'
            '"needs_review":false,"reason":"ok"}'
        )


def test_classification_result_rejects_non_object_json() -> None:
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate_json("[1,2,3]")


def test_classification_review_rejects_wrong_shape() -> None:
    with pytest.raises(ValidationError):
        ClassificationReview.model_validate_json('{"summary":"ok"}')


def test_investigation_finding_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        InvestigationFinding.model_validate_json(
            '{"branch":"a","hypothesis":"h","confidence":0.5,"evidence":[],'
            '"gaps":[],"next_checks":[],"extra":true}'
        )


def test_review_synthesis_rejects_numeric_string_field() -> None:
    with pytest.raises(ValidationError):
        ReviewSynthesis.model_validate_json(
            '{"summary":123,"likely_causes":[],"evidence_gaps":[],"recommended_next_steps":[]}'
        )


def test_classification_document_round_trips() -> None:
    doc = ClassificationDocument(document_id=1, title="t", file_path="p", current_category=None)
    assert doc.document_id == 1


def test_root_cause_branch_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        RootCauseBranch.model_validate_json('{"name":"a","objective":"o","extra":1}')


# ---------------------------------------------------------------------------
# classify_documents
# ---------------------------------------------------------------------------


async def test_single_document_is_rejected_without_spawn() -> None:
    controller = FakeSpawnController(scripts=[])

    with pytest.raises(ValueError):
        await classify_documents(
            controller,
            session_id=1,
            documents=[ClassificationDocument(document_id=1, title="a", file_path="a.md")],
        )

    assert controller.spawn_calls == []


async def test_classify_documents_rejects_duplicate_document_ids() -> None:
    controller = FakeSpawnController(scripts=[])

    with pytest.raises(ValueError):
        await classify_documents(
            controller,
            session_id=1,
            documents=[
                ClassificationDocument(document_id=1, title="a", file_path="a.md"),
                ClassificationDocument(document_id=1, title="b", file_path="b.md"),
            ],
        )

    assert controller.spawn_calls == []


async def test_two_documents_spawn_in_parallel_without_unneeded_reviewer() -> None:
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_classification_json(1, confidence=0.95)),
            _ChildScript(result_summary=_classification_json(2, confidence=0.9)),
        ]
    )

    outcome = await classify_documents(
        controller,
        session_id=1,
        documents=[
            ClassificationDocument(document_id=1, title="a", file_path="a.md"),
            ClassificationDocument(document_id=2, title="b", file_path="b.md"),
        ],
    )

    assert len(outcome.suggestions) == 2
    assert outcome.review is None
    assert outcome.workflow_failure is None
    assert len(controller.spawn_calls) == 2
    assert all(call.role == "classifier" for call in controller.spawn_calls)


async def test_more_than_five_documents_run_in_bounded_waves() -> None:
    controller = FakeSpawnController(
        scripts=[_ChildScript(result_summary=_classification_json(i)) for i in range(6)],
        max_concurrent_children=5,
    )
    documents = [
        ClassificationDocument(document_id=i, title=f"doc-{i}", file_path=f"{i}.md") for i in range(6)
    ]

    outcome = await classify_documents(controller, session_id=1, documents=documents)

    assert len(outcome.suggestions) == 6
    assert len(controller.spawn_calls) == 6
    # The 6th spawn (index 5) must not happen until wave 1's 5 children were closed.
    assert controller.close_count_at_spawn[5] == 5


async def test_confidence_below_point_eight_triggers_reviewer() -> None:
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_classification_json(1, confidence=0.79)),
            _ChildScript(result_summary=_classification_json(2, confidence=0.9)),
            _ChildScript(result_summary=_review_json()),
        ]
    )

    outcome = await classify_documents(
        controller,
        session_id=1,
        documents=[
            ClassificationDocument(document_id=1, title="a", file_path="a.md"),
            ClassificationDocument(document_id=2, title="b", file_path="b.md"),
        ],
    )

    assert outcome.review is not None
    assert [call.role for call in controller.spawn_calls] == ["classifier", "classifier", "reviewer"]
    reviewer_call = controller.spawn_calls[-1]
    assert reviewer_call.role == "reviewer"
    assert reviewer_call.parent_agent_id == "child-1"
    assert controller.close_count_at_spawn[-1] >= 1


async def test_confidence_equal_to_point_eight_does_not_trigger_reviewer() -> None:
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_classification_json(1, confidence=CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD)),
            _ChildScript(result_summary=_classification_json(2, confidence=0.9)),
        ]
    )

    outcome = await classify_documents(
        controller,
        session_id=1,
        documents=[
            ClassificationDocument(document_id=1, title="a", file_path="a.md"),
            ClassificationDocument(document_id=2, title="b", file_path="b.md"),
        ],
    )

    assert outcome.review is None
    assert len(controller.spawn_calls) == 2


async def test_needs_review_new_category_parse_failure_or_child_failure_triggers_reviewer() -> None:
    # needs_review=True flag
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_classification_json(1, needs_review=True)),
            _ChildScript(result_summary=_classification_json(2)),
            _ChildScript(result_summary=_review_json()),
        ]
    )
    outcome = await classify_documents(
        controller,
        session_id=1,
        documents=[
            ClassificationDocument(document_id=1, title="a", file_path="a.md"),
            ClassificationDocument(document_id=2, title="b", file_path="b.md"),
        ],
    )
    assert outcome.review is not None

    # category outside allowed_categories
    controller2 = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_classification_json(1, category="未知分类")),
            _ChildScript(result_summary=_classification_json(2, category="网络")),
            _ChildScript(result_summary=_review_json()),
        ]
    )
    outcome2 = await classify_documents(
        controller2,
        session_id=1,
        documents=[
            ClassificationDocument(document_id=1, title="a", file_path="a.md"),
            ClassificationDocument(document_id=2, title="b", file_path="b.md"),
        ],
        allowed_categories=["网络"],
    )
    assert outcome2.review is not None

    # parse failure on one child
    controller3 = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary="not json"),
            _ChildScript(result_summary=_classification_json(2)),
            _ChildScript(result_summary=_review_json()),
        ]
    )
    outcome3 = await classify_documents(
        controller3,
        session_id=1,
        documents=[
            ClassificationDocument(document_id=1, title="a", file_path="a.md"),
            ClassificationDocument(document_id=2, title="b", file_path="b.md"),
        ],
    )
    assert outcome3.review is not None
    assert len(outcome3.parse_failures) == 1

    # child failure (FAILED status)
    controller4 = FakeSpawnController(
        scripts=[
            _ChildScript(status="FAILED", result_summary=None),
            _ChildScript(result_summary=_classification_json(2)),
            _ChildScript(result_summary=_review_json()),
        ]
    )
    outcome4 = await classify_documents(
        controller4,
        session_id=1,
        documents=[
            ClassificationDocument(document_id=1, title="a", file_path="a.md"),
            ClassificationDocument(document_id=2, title="b", file_path="b.md"),
        ],
    )
    assert outcome4.review is not None
    assert len(outcome4.failed_child_ids) == 1


async def test_all_classifiers_failed_returns_workflow_failure_without_reviewer() -> None:
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(status="FAILED", result_summary=None),
            _ChildScript(status="FAILED", result_summary=None),
        ]
    )

    outcome = await classify_documents(
        controller,
        session_id=1,
        documents=[
            ClassificationDocument(document_id=1, title="a", file_path="a.md"),
            ClassificationDocument(document_id=2, title="b", file_path="b.md"),
        ],
    )

    assert outcome.suggestions == ()
    assert outcome.workflow_failure is not None
    assert outcome.review is None
    assert all(call.role == "classifier" for call in controller.spawn_calls)


async def test_classification_closes_every_spawned_child_when_wait_or_parse_fails() -> None:
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(wait_error=True),
            _ChildScript(result_summary="not json"),
        ]
    )

    await classify_documents(
        controller,
        session_id=1,
        documents=[
            ClassificationDocument(document_id=1, title="a", file_path="a.md"),
            ClassificationDocument(document_id=2, title="b", file_path="b.md"),
        ],
    )

    # child-0's wait failed but it must still be closed; child-1 waited fine and closed too.
    assert set(controller.close_calls) >= {"child-0", "child-1"}


async def test_close_failure_still_closes_siblings_and_prevents_success() -> None:
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_classification_json(1), close_error=True),
            _ChildScript(result_summary=_classification_json(2)),
        ]
    )

    with pytest.raises(WorkflowCleanupError) as exc_info:
        await classify_documents(
            controller,
            session_id=1,
            documents=[
                ClassificationDocument(document_id=1, title="a", file_path="a.md"),
                ClassificationDocument(document_id=2, title="b", file_path="b.md"),
            ],
        )

    assert "child-0" in exc_info.value.failed_child_ids
    # child-1's close was still attempted despite child-0's close failing.
    assert "child-1" in controller.close_calls


# ---------------------------------------------------------------------------
# investigate_root_cause
# ---------------------------------------------------------------------------


async def test_default_root_cause_branches_are_parallel_and_read_only() -> None:
    assert [branch.name for branch in DEFAULT_ROOT_CAUSE_BRANCHES] == [
        "monitor_history",
        "cmdb_topology",
        "peer_scope",
    ]
    cmdb_branch = next(b for b in DEFAULT_ROOT_CAUSE_BRANCHES if b.name == "cmdb_topology")
    assert "变更" in cmdb_branch.objective

    controller = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_finding_json(branch.name)) for branch in DEFAULT_ROOT_CAUSE_BRANCHES
        ]
        + [_ChildScript(result_summary=_synthesis_json())]
    )

    outcome = await investigate_root_cause(controller, session_id=1, incident_context="核心交换机抖动")

    assert len(outcome.findings) == 3
    assert [call.role for call in controller.spawn_calls[:3]] == ["investigator"] * 3
    assert all("绕过只读工具" in call.task_brief or "变更" in call.task_brief for call in controller.spawn_calls[:3] if call.role == "investigator" and "cmdb_topology" in call.task_brief)


async def test_custom_workflow_requires_at_least_two_branches() -> None:
    controller = FakeSpawnController(scripts=[])

    with pytest.raises(ValueError):
        await investigate_root_cause(
            controller,
            session_id=1,
            incident_context="故障",
            branches=[RootCauseBranch(name="only_one", objective="x")],
        )

    assert controller.spawn_calls == []


async def test_investigate_root_cause_rejects_blank_incident_context() -> None:
    controller = FakeSpawnController(scripts=[])

    with pytest.raises(ValueError):
        await investigate_root_cause(controller, session_id=1, incident_context="   ")

    assert controller.spawn_calls == []


async def test_partial_branch_failure_still_spawns_reviewer() -> None:
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(status="FAILED", result_summary=None),
            _ChildScript(result_summary=_finding_json("branch-b")),
            _ChildScript(result_summary=_synthesis_json()),
        ]
    )

    outcome = await investigate_root_cause(
        controller,
        session_id=1,
        incident_context="故障",
        branches=[
            RootCauseBranch(name="branch-a", objective="x"),
            RootCauseBranch(name="branch-b", objective="y"),
        ],
    )

    assert len(outcome.findings) == 1
    assert len(outcome.failed_child_ids) == 1
    assert outcome.review is not None
    assert [call.role for call in controller.spawn_calls] == ["investigator", "investigator", "reviewer"]
    reviewer_call = controller.spawn_calls[-1]
    assert reviewer_call.role == "reviewer"
    assert reviewer_call.parent_agent_id == "child-1"
    assert controller.close_count_at_spawn[-1] >= 1


async def test_all_branches_failed_skips_reviewer_and_reports_failure() -> None:
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(status="FAILED", result_summary=None),
            _ChildScript(status="FAILED", result_summary=None),
        ]
    )

    outcome = await investigate_root_cause(
        controller,
        session_id=1,
        incident_context="故障",
        branches=[
            RootCauseBranch(name="branch-a", objective="x"),
            RootCauseBranch(name="branch-b", objective="y"),
        ],
    )

    assert outcome.findings == ()
    assert outcome.workflow_failure is not None
    assert outcome.review is None
    assert all(call.role == "investigator" for call in controller.spawn_calls)


async def test_malformed_reviewer_result_is_an_explicit_workflow_failure() -> None:
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_finding_json("branch-a")),
            _ChildScript(result_summary=_finding_json("branch-b")),
            _ChildScript(result_summary="not json"),
        ]
    )

    outcome = await investigate_root_cause(
        controller,
        session_id=1,
        incident_context="故障",
        branches=[
            RootCauseBranch(name="branch-a", objective="x"),
            RootCauseBranch(name="branch-b", objective="y"),
        ],
    )

    assert outcome.review is None
    assert outcome.workflow_failure is not None
    # Findings from the two successful branches must still be preserved.
    assert len(outcome.findings) == 2


async def test_root_cause_closes_investigators_and_reviewer_on_cancellation() -> None:
    release = asyncio.Event()

    controller = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_finding_json("branch-a")),
            _ChildScript(result_summary=_finding_json("branch-b")),
        ]
    )

    real_wait_agent = controller.wait_agent

    async def blocking_wait_agent(child_id: str, *, timeout_ms: int | None = None) -> ChildReceipt:
        if child_id == "child-1":
            await release.wait()
        return await real_wait_agent(child_id, timeout_ms=timeout_ms)

    controller.wait_agent = blocking_wait_agent  # type: ignore[method-assign]

    task = asyncio.create_task(
        investigate_root_cause(
            controller,
            session_id=1,
            incident_context="故障",
            branches=[
                RootCauseBranch(name="branch-a", objective="x"),
                RootCauseBranch(name="branch-b", objective="y"),
            ],
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert set(controller.close_calls) == {"child-0", "child-1"}


async def test_single_concurrency_reviewer_falls_back_to_root_level() -> None:
    controller = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_classification_json(1, confidence=0.79)),
            _ChildScript(result_summary=_classification_json(2, confidence=0.9)),
            _ChildScript(result_summary=_review_json()),
        ],
        max_concurrent_children=1,
    )

    outcome = await classify_documents(
        controller,
        session_id=1,
        documents=[
            ClassificationDocument(document_id=1, title="a", file_path="a.md"),
            ClassificationDocument(document_id=2, title="b", file_path="b.md"),
        ],
    )

    assert outcome.review is not None
    reviewer_call = controller.spawn_calls[-1]
    assert reviewer_call.role == "reviewer"
    assert reviewer_call.parent_agent_id is None
    # 单并发下 reviewer spawn 前最后一波 worker 必须已 close
    assert controller.close_count_at_spawn[-1] >= 1
    assert "child-1" in controller.close_calls
    assert controller.close_calls.index("child-1") < controller.spawn_calls.index(reviewer_call)


async def test_cancellation_closes_workers_reviewer_and_reserved_parent() -> None:
    release = asyncio.Event()

    controller = FakeSpawnController(
        scripts=[
            _ChildScript(result_summary=_classification_json(1, confidence=0.79)),
            _ChildScript(result_summary=_classification_json(2, confidence=0.9)),
            _ChildScript(result_summary=_review_json()),
        ]
    )

    real_wait_agent = controller.wait_agent

    async def blocking_wait_agent(child_id: str, *, timeout_ms: int | None = None) -> ChildReceipt:
        if child_id == "child-2":
            await release.wait()
        return await real_wait_agent(child_id, timeout_ms=timeout_ms)

    controller.wait_agent = blocking_wait_agent  # type: ignore[method-assign]

    reviewer_spawned = asyncio.Event()
    real_spawn_agent = controller.spawn_agent

    async def spawn_and_signal_reviewer(
        *,
        session_id: int,
        role: str,
        task_brief: str,
        trace_id: str | None = None,
        parent_agent_id: str | None = None,
        model: str | None = None,
        tools_allowlist: Iterable[str] | None = None,
        budget: ChildBudgetSnapshot | None = None,
        fork_mode: str = "none",
    ) -> ChildReceipt:
        receipt = await real_spawn_agent(
            session_id=session_id,
            role=role,
            task_brief=task_brief,
            trace_id=trace_id,
            parent_agent_id=parent_agent_id,
            model=model,
            tools_allowlist=tools_allowlist,
            budget=budget,
            fork_mode=fork_mode,
        )
        if role == "reviewer":
            reviewer_spawned.set()
        return receipt

    controller.spawn_agent = spawn_and_signal_reviewer  # type: ignore[method-assign]

    task = asyncio.create_task(
        classify_documents(
            controller,
            session_id=1,
            documents=[
                ClassificationDocument(document_id=1, title="a", file_path="a.md"),
                ClassificationDocument(document_id=2, title="b", file_path="b.md"),
            ],
        )
    )
    await reviewer_spawned.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert set(controller.close_calls) == {"child-0", "child-1", "child-2"}
