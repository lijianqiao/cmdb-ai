"""有界并行子 Agent 编排范式：批量文档归类与根因排查。

实现流程：
1. workflow 只通过 SpawnController 协议（spawn_agent/wait_agent/close_agent +
   max_concurrent_children）驱动子 Agent，不触碰 SpawnManager 内部的任务表、
   信号量或 ORM 行——这样单测可以用一个假 controller 完全脱离数据库运行，
   而 T09 Task 9 的集成测试可以把真实 SpawnManager 原样传进来，因为它的方法
   签名天然满足这个 Protocol。
2. `_run_wave` 是两个 workflow 共用的核心：按 `controller.max_concurrent_children`
   分波 spawn（绝不提前 spawn 第 6 个再等槽位空出来），用
   `return_exceptions=True` 的 wait 保证一个子 Agent 的运行时异常不会拖累其它
   兄弟节点，每一波结束后立刻把这一波 spawn 出来的子 Agent 全部 close 掉再进
   入下一波，避免终态回执长期占用并发槽位。close 失败会被包成
   `WorkflowCleanupError` 直接向上抛出（不会被静默吞掉伪装成一次"成功"的
   workflow 结果）；如果调用方所在的 task 被取消，清理会在一个被
   `asyncio.shield` 保护的任务里跑完，再把 CancelledError 重新抛出去。
3. 两个 workflow 各自负责：把领域输入转成 task_brief，把子 Agent 返回的
   `result_summary` 用严格 Pydantic 模型（`extra="forbid"`、`strict=True`）解析
   成结构化结果——解析失败或字段不匹配一律记成"待复核"而不是当作成功；再按
   各自的触发条件决定要不要再 spawn 一个 reviewer 做复核。reviewer 只拿到
   经过裁剪的结构化摘要（不是完整子 transcript），复核结果同样严格解析。最终
   拼成一个不可变的 Outcome dataclass 返回——本模块只读只建议，绝不写
   KnowledgeDocument.category_id、CMDB 或监控表，写入始终是调用方自己的决定。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.spawn import ChildBudgetSnapshot, ChildReceipt

logger = logging.getLogger(__name__)

CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD = 0.80
_RAW_SUMMARY_TRUNCATE_LIMIT = 500


class SpawnController(Protocol):
    """The only surface a workflow may use — never manager internals or ORM rows."""

    @property
    def max_concurrent_children(self) -> int: ...

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
    ) -> ChildReceipt: ...

    async def wait_agent(self, child_id: str, *, timeout_ms: int | None = None) -> ChildReceipt: ...

    async def close_agent(self, child_id: str) -> ChildReceipt: ...


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    """One typed spawn request; never an untyped `**kwargs` bag."""

    session_id: int
    role: str
    task_brief: str
    trace_id: str
    parent_agent_id: str | None = None
    model: str | None = None
    tools_allowlist: tuple[str, ...] | None = None
    budget: ChildBudgetSnapshot | None = None


type WaveFailureKind = Literal["wait_failed"]


@dataclass(frozen=True, slots=True)
class WaveFailure:
    """One request whose child could not be waited on successfully."""

    child_id: str
    failure_kind: WaveFailureKind


@dataclass(frozen=True, slots=True)
class WaveResult:
    """Per-request outcome of one bounded wave, index-aligned with the input requests.

    `receipts[i] is None` iff that request's child failed to wait successfully;
    its child_id and failure kind are then recorded in `failures`.

    `open_final_receipts` holds the last wave's spawned receipts that were waited
    on successfully but intentionally not closed — the workflow must close them
    after deciding whether the reviewer nests under a surviving worker.
    """

    receipts: tuple[ChildReceipt | None, ...]
    failures: tuple[WaveFailure, ...]
    open_final_receipts: tuple[ChildReceipt, ...] = ()


class WorkflowCleanupError(RuntimeError):
    """One or more spawned children could not be closed during cleanup."""

    def __init__(self, failed_child_ids: tuple[str, ...]) -> None:
        self.failed_child_ids = failed_child_ids
        super().__init__(f"failed to close {len(failed_child_ids)} child agent(s) during cleanup")


@dataclass(frozen=True, slots=True)
class ParseFailure:
    """A child produced output that failed strict-schema parsing."""

    child_id: str
    raw_summary: str


def _truncate(text: str, *, limit: int = _RAW_SUMMARY_TRUNCATE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…(截断)"


class _StrictWorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ClassificationDocument(_StrictWorkflowModel):
    document_id: int
    title: str
    file_path: str
    current_category: str | None = None


class ClassificationResult(_StrictWorkflowModel):
    document_id: int
    recommended_category: str
    confidence: float = Field(ge=0, le=1)
    needs_review: bool
    reason: str


class ClassificationReview(_StrictWorkflowModel):
    summary: str
    accepted_document_ids: list[int]
    disputed_document_ids: list[int]
    recommended_actions: list[str]


class RootCauseBranch(_StrictWorkflowModel):
    name: str
    objective: str


class InvestigationFinding(_StrictWorkflowModel):
    branch: str
    hypothesis: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str]
    gaps: list[str]
    next_checks: list[str]


class ReviewSynthesis(_StrictWorkflowModel):
    summary: str
    likely_causes: list[str]
    evidence_gaps: list[str]
    recommended_next_steps: list[str]


@dataclass(frozen=True, slots=True)
class BatchClassificationOutcome:
    """Advisory batch-classification result. Never applies a category itself."""

    trace_id: str
    suggestions: tuple[ClassificationResult, ...]
    child_ids: tuple[str, ...]
    failed_child_ids: tuple[str, ...]
    parse_failures: tuple[ParseFailure, ...]
    review: ClassificationReview | None = None
    workflow_failure: str | None = None


@dataclass(frozen=True, slots=True)
class RootCauseOutcome:
    """Advisory root-cause investigation result. Never creates a remediation proposal."""

    trace_id: str
    findings: tuple[InvestigationFinding, ...]
    child_ids: tuple[str, ...]
    failed_child_ids: tuple[str, ...]
    parse_failures: tuple[ParseFailure, ...]
    review: ReviewSynthesis | None = None
    workflow_failure: str | None = None


DEFAULT_ROOT_CAUSE_BRANCHES: tuple[RootCauseBranch, ...] = (
    RootCauseBranch(
        name="monitor_history",
        objective="核查涉事目标近期的监控状态历史，判断故障窗口与探测记录是否吻合。",
    ),
    RootCauseBranch(
        name="cmdb_topology",
        objective=(
            "核查涉事资产的 CMDB 归属与依赖拓扑，识别可能受影响的上下游资产；"
            "当前只读工具没有变更日志查询能力，查不到的变更历史必须作为证据"
            "缺口列出，不得编造变更记录，也不得绕过只读工具直接查数据库。"
        ),
    ),
    RootCauseBranch(
        name="peer_scope",
        objective="核查同业务系统或同网段范围内的其它资产是否有相同现象，评估影响面。",
    ),
)


async def _close_all(controller: SpawnController, receipts: Sequence[ChildReceipt]) -> None:
    """Attempt to close every receipt; raise WorkflowCleanupError if any close failed."""
    if not receipts:
        return
    results = await asyncio.gather(
        *(controller.close_agent(receipt.child_id) for receipt in receipts),
        return_exceptions=True,
    )
    failed_ids = tuple(
        receipt.child_id
        for receipt, result in zip(receipts, results, strict=True)
        if isinstance(result, BaseException)
    )
    if failed_ids:
        raise WorkflowCleanupError(failed_ids)


async def _close_all_shielded(
    controller: SpawnController, receipts: Sequence[ChildReceipt]
) -> None:
    """Close every receipt to completion even while the awaiting task is being cancelled.

    Only used from a CancelledError handler: a cleanup failure here is logged
    (not raised) so the original CancelledError remains what the caller sees —
    per this module's cancellation-cleanup contract, no raw exception detail
    is attached to that log line beyond the safe child-id list.
    """
    if not receipts:
        return
    task: asyncio.Task[None] = asyncio.ensure_future(_close_all(controller, receipts))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    exc = task.exception()
    if exc is None:
        return
    if isinstance(exc, WorkflowCleanupError):
        logger.warning("取消清理时未能关闭部分子 Agent: %s", exc.failed_child_ids)
        return
    raise exc


async def _close_remaining(controller: SpawnController, receipts: Sequence[ChildReceipt]) -> None:
    """Close every receipt still held open by the workflow, even during cancellation."""
    if not receipts:
        return
    try:
        await _close_all(controller, receipts)
    except asyncio.CancelledError:
        await _close_all_shielded(controller, receipts)
        raise


def _select_completed_parent_id(
    wave: WaveResult,
    open_receipts: Sequence[ChildReceipt],
) -> str | None:
    """Pick the last successfully completed worker from the final wave still open."""
    open_ids = {receipt.child_id for receipt in open_receipts}
    for receipt in reversed(wave.receipts):
        if receipt is None:
            continue
        if receipt.child_id not in open_ids:
            continue
        if receipt.status == "COMPLETED":
            return receipt.child_id
    return None


async def _run_wave(
    controller: SpawnController,
    spawn_requests: Sequence[SpawnRequest],
) -> WaveResult:
    """Spawn every request in bounded waves; close non-final waves before the next."""
    receipts: list[ChildReceipt | None] = [None] * len(spawn_requests)
    failures: list[WaveFailure] = []
    unclosed: list[ChildReceipt] = []
    open_final_receipts: list[ChildReceipt] = []
    chunk_size = max(1, controller.max_concurrent_children)
    total = len(spawn_requests)

    try:
        for start in range(0, total, chunk_size):
            is_final_wave = start + chunk_size >= total
            indices = range(start, min(start + chunk_size, total))
            chunk_receipts: list[ChildReceipt] = []
            for index in indices:
                request = spawn_requests[index]
                receipt = await controller.spawn_agent(
                    session_id=request.session_id,
                    role=request.role,
                    task_brief=request.task_brief,
                    trace_id=request.trace_id,
                    parent_agent_id=request.parent_agent_id,
                    model=request.model,
                    tools_allowlist=request.tools_allowlist,
                    budget=request.budget,
                )
                unclosed.append(receipt)
                chunk_receipts.append(receipt)

            wait_results = await asyncio.gather(
                *(controller.wait_agent(receipt.child_id) for receipt in chunk_receipts),
                return_exceptions=True,
            )
            for index, receipt, result in zip(indices, chunk_receipts, wait_results, strict=True):
                if isinstance(result, BaseException):
                    failures.append(
                        WaveFailure(child_id=receipt.child_id, failure_kind="wait_failed")
                    )
                else:
                    receipts[index] = result

            pending = tuple(unclosed)
            unclosed.clear()
            if is_final_wave:
                open_final_receipts.extend(pending)
            else:
                await _close_all(controller, pending)
    except asyncio.CancelledError:
        pending = tuple(unclosed)
        unclosed.clear()
        await _close_all_shielded(controller, (*open_final_receipts, *pending))
        raise
    except BaseException:
        pending = tuple(unclosed)
        unclosed.clear()
        still_open = (*open_final_receipts, *pending)
        if still_open:
            await _close_all(controller, still_open)
        raise

    return WaveResult(
        receipts=tuple(receipts),
        failures=tuple(failures),
        open_final_receipts=tuple(open_final_receipts),
    )


def _classification_task_brief(
    document: ClassificationDocument, allowed_categories: Sequence[str]
) -> str:
    categories_text = (
        "、".join(allowed_categories)
        if allowed_categories
        else "（未提供候选分类，可在 recommended_category 里建议新分类）"
    )
    current = document.current_category or "无"
    return (
        f"只处理这一份文档，不要涉及其它文档。\n"
        f"document_id={document.document_id}\n"
        f"标题: {document.title}\n"
        f"路径: {document.file_path}\n"
        f"当前分类: {current}\n"
        f"候选分类(allowed_categories): {categories_text}\n"
        "请先读取正文再给出分类建议。"
    )


def _classification_review_task_brief(
    suggestions: Sequence[ClassificationResult],
    failed_child_ids: Sequence[str],
    parse_failures: Sequence[ParseFailure],
) -> str:
    lines = [
        f"- document_id={item.document_id} 建议分类={item.recommended_category} "
        f"置信度={item.confidence:.2f} 需要复核={item.needs_review} 理由={item.reason}"
        for item in suggestions
    ]
    if failed_child_ids:
        lines.append(f"- 有 {len(failed_child_ids)} 个分类子 Agent 未产出结果或执行失败")
    if parse_failures:
        lines.append(f"- 有 {len(parse_failures)} 个分类子 Agent 输出解析失败")
    return (
        "以下是批量文档分类的结果摘要，请复核是否有冲突、置信度不足或需要人工确认的条目：\n"
        + "\n".join(lines)
        + "\n\n最终回答必须是一个 JSON 对象，不要 Markdown 代码围栏：\n"
        '{"summary":"...","accepted_document_ids":[...],'
        '"disputed_document_ids":[...],"recommended_actions":["..."]}'
    )


async def _run_reviewer(
    controller: SpawnController,
    *,
    session_id: int,
    trace_id: str,
    task_brief: str,
    parent_agent_id: str | None = None,
    spawned_out: list[ChildReceipt] | None = None,
) -> ChildReceipt | None:
    """Spawn and wait one reviewer; the workflow finally closes its terminal receipt."""
    try:
        receipt = await controller.spawn_agent(
            session_id=session_id,
            role="reviewer",
            task_brief=task_brief,
            trace_id=trace_id,
            parent_agent_id=parent_agent_id,
        )
    except Exception:
        return None

    if spawned_out is not None:
        spawned_out.append(receipt)

    try:
        return await controller.wait_agent(receipt.child_id)
    except asyncio.CancelledError:
        await _close_all_shielded(controller, (receipt,))
        raise
    except Exception:
        await _close_all(controller, (receipt,))
        return None


async def _spawn_nested_or_root_reviewer(
    controller: SpawnController,
    *,
    session_id: int,
    trace_id: str,
    task_brief: str,
    wave: WaveResult,
    open_receipts: list[ChildReceipt],
    spawned_reviewers: list[ChildReceipt],
) -> tuple[ChildReceipt | None, list[ChildReceipt]]:
    """Close siblings, spawn nested reviewer when possible, else fall back to root-level."""
    parent_id = _select_completed_parent_id(wave, open_receipts)
    if parent_id is not None and controller.max_concurrent_children >= 2:
        siblings = [receipt for receipt in open_receipts if receipt.child_id != parent_id]
        await _close_all(controller, siblings)
        open_receipts[:] = [receipt for receipt in open_receipts if receipt.child_id == parent_id]
        reviewer_receipt = await _run_reviewer(
            controller,
            session_id=session_id,
            trace_id=trace_id,
            task_brief=task_brief,
            parent_agent_id=parent_id,
            spawned_out=spawned_reviewers,
        )
        return reviewer_receipt, open_receipts

    await _close_all(controller, open_receipts)
    open_receipts.clear()
    reviewer_receipt = await _run_reviewer(
        controller,
        session_id=session_id,
        trace_id=trace_id,
        task_brief=task_brief,
        parent_agent_id=None,
        spawned_out=spawned_reviewers,
    )
    return reviewer_receipt, open_receipts


def _dedupe_receipts(receipts: Sequence[ChildReceipt]) -> tuple[ChildReceipt, ...]:
    seen: set[str] = set()
    unique: list[ChildReceipt] = []
    for receipt in receipts:
        if receipt.child_id in seen:
            continue
        seen.add(receipt.child_id)
        unique.append(receipt)
    return tuple(unique)


async def classify_documents(
    controller: SpawnController,
    *,
    session_id: int,
    documents: Sequence[ClassificationDocument],
    allowed_categories: Sequence[str] = (),
) -> BatchClassificationOutcome:
    """Classify two or more documents in bounded parallel waves; advisory only."""
    if len(documents) < 2:
        raise ValueError(
            "classify_documents requires at least two documents; classify a single "
            "document directly from the root Agent instead of spawning a child"
        )
    seen_ids: set[int] = set()
    for document in documents:
        if document.document_id in seen_ids:
            raise ValueError(
                f"duplicate document_id in classify_documents input: {document.document_id}"
            )
        seen_ids.add(document.document_id)

    trace_id = str(uuid4())
    allowed = tuple(allowed_categories)
    spawn_requests = [
        SpawnRequest(
            session_id=session_id,
            role="classifier",
            task_brief=_classification_task_brief(document, allowed),
            trace_id=trace_id,
        )
        for document in documents
    ]

    wave = await _run_wave(controller, spawn_requests)

    open_receipts: list[ChildReceipt] = list(wave.open_final_receipts)
    reviewer_receipt: ChildReceipt | None = None
    spawned_reviewers: list[ChildReceipt] = []

    try:
        suggestions: list[ClassificationResult] = []
        parse_failures: list[ParseFailure] = []
        child_ids: list[str] = []
        failed_child_ids = [failure.child_id for failure in wave.failures]
        needs_review = False

        for document, receipt in zip(documents, wave.receipts, strict=True):
            if receipt is None:
                needs_review = True
                continue
            child_ids.append(receipt.child_id)
            if receipt.status != "COMPLETED" or receipt.result_summary is None:
                failed_child_ids.append(receipt.child_id)
                needs_review = True
                continue
            try:
                result = ClassificationResult.model_validate_json(receipt.result_summary)
            except ValidationError:
                parse_failures.append(
                    ParseFailure(
                        child_id=receipt.child_id, raw_summary=_truncate(receipt.result_summary)
                    )
                )
                needs_review = True
                continue
            if result.document_id != document.document_id:
                parse_failures.append(
                    ParseFailure(
                        child_id=receipt.child_id, raw_summary=_truncate(receipt.result_summary)
                    )
                )
                needs_review = True
                continue
            suggestions.append(result)
            if (
                result.needs_review
                or result.confidence < CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD
                or (allowed and result.recommended_category not in allowed)
            ):
                needs_review = True

        if not suggestions:
            return BatchClassificationOutcome(
                trace_id=trace_id,
                suggestions=(),
                child_ids=tuple(child_ids),
                failed_child_ids=tuple(failed_child_ids),
                parse_failures=tuple(parse_failures),
                review=None,
                workflow_failure="所有分类子 Agent 均未产出可用结果",
            )

        review: ClassificationReview | None = None
        review_failure: str | None = None
        if needs_review:
            review_brief = _classification_review_task_brief(
                suggestions, failed_child_ids, parse_failures
            )
            reviewer_receipt, _ = await _spawn_nested_or_root_reviewer(
                controller,
                session_id=session_id,
                trace_id=trace_id,
                task_brief=review_brief,
                wave=wave,
                open_receipts=open_receipts,
                spawned_reviewers=spawned_reviewers,
            )
            if reviewer_receipt is None:
                review_failure = "复核子 Agent 未产出结果"
            elif reviewer_receipt.status != "COMPLETED" or reviewer_receipt.result_summary is None:
                review_failure = "复核子 Agent 未产出结果"
            else:
                try:
                    review = ClassificationReview.model_validate_json(reviewer_receipt.result_summary)
                except ValidationError:
                    review_failure = "复核子 Agent 输出解析失败"

        return BatchClassificationOutcome(
            trace_id=trace_id,
            suggestions=tuple(suggestions),
            child_ids=tuple(child_ids),
            failed_child_ids=tuple(failed_child_ids),
            parse_failures=tuple(parse_failures),
            review=review,
            workflow_failure=review_failure,
        )
    finally:
        remaining = _dedupe_receipts(
            (*open_receipts, *spawned_reviewers, *(tuple([reviewer_receipt]) if reviewer_receipt else ()))
        )
        await _close_remaining(controller, remaining)


def _investigation_task_brief(branch: RootCauseBranch, incident_context: str) -> str:
    return (
        f"事故背景: {incident_context}\n"
        f"分支: {branch.name}\n"
        f"目标: {branch.objective}\n"
        "只验证这一个分支假设，不要提前综合其它分支的结论。"
    )


def _root_cause_review_task_brief(
    incident_context: str,
    findings: Sequence[InvestigationFinding],
    failed_child_ids: Sequence[str],
    parse_failures: Sequence[ParseFailure],
) -> str:
    lines = [
        f"- 分支={item.branch} 假设={item.hypothesis} 置信度={item.confidence:.2f} "
        f"证据={item.evidence} 缺口={item.gaps}"
        for item in findings
    ]
    if failed_child_ids:
        lines.append(f"- 有 {len(failed_child_ids)} 个调查分支未产出结果或执行失败")
    if parse_failures:
        lines.append(f"- 有 {len(parse_failures)} 个调查分支输出解析失败")
    return (
        f"事故背景: {incident_context}\n"
        "以下是各根因排查分支的发现，请综合判断并指出证据缺口：\n"
        + "\n".join(lines)
        + "\n\n最终回答必须是一个 JSON 对象，不要 Markdown 代码围栏：\n"
        '{"summary":"...","likely_causes":["..."],'
        '"evidence_gaps":["..."],"recommended_next_steps":["..."]}'
    )


async def investigate_root_cause(
    controller: SpawnController,
    *,
    session_id: int,
    incident_context: str,
    branches: Sequence[RootCauseBranch] = DEFAULT_ROOT_CAUSE_BRANCHES,
) -> RootCauseOutcome:
    """Investigate two or more root-cause hypothesis branches in parallel; read-only."""
    if not incident_context.strip():
        raise ValueError("incident_context must not be blank")
    if len(branches) < 2:
        raise ValueError("investigate_root_cause requires at least two branches")
    seen_names: set[str] = set()
    for branch in branches:
        if not branch.name.strip():
            raise ValueError("root-cause branch name must not be blank")
        if not branch.objective.strip():
            raise ValueError("root-cause branch objective must not be blank")
        if branch.name in seen_names:
            raise ValueError(f"duplicate root-cause branch name: {branch.name}")
        seen_names.add(branch.name)

    trace_id = str(uuid4())
    spawn_requests = [
        SpawnRequest(
            session_id=session_id,
            role="investigator",
            task_brief=_investigation_task_brief(branch, incident_context),
            trace_id=trace_id,
        )
        for branch in branches
    ]

    wave = await _run_wave(controller, spawn_requests)

    open_receipts: list[ChildReceipt] = list(wave.open_final_receipts)
    reviewer_receipt: ChildReceipt | None = None
    spawned_reviewers: list[ChildReceipt] = []

    try:
        findings: list[InvestigationFinding] = []
        parse_failures: list[ParseFailure] = []
        child_ids: list[str] = []
        failed_child_ids = [failure.child_id for failure in wave.failures]

        for branch, receipt in zip(branches, wave.receipts, strict=True):
            if receipt is None:
                continue
            child_ids.append(receipt.child_id)
            if receipt.status != "COMPLETED" or receipt.result_summary is None:
                failed_child_ids.append(receipt.child_id)
                continue
            try:
                finding = InvestigationFinding.model_validate_json(receipt.result_summary)
            except ValidationError:
                parse_failures.append(
                    ParseFailure(
                        child_id=receipt.child_id, raw_summary=_truncate(receipt.result_summary)
                    )
                )
                continue
            if finding.branch != branch.name:
                parse_failures.append(
                    ParseFailure(
                        child_id=receipt.child_id, raw_summary=_truncate(receipt.result_summary)
                    )
                )
                continue
            findings.append(finding)

        if not findings:
            return RootCauseOutcome(
                trace_id=trace_id,
                findings=(),
                child_ids=tuple(child_ids),
                failed_child_ids=tuple(failed_child_ids),
                parse_failures=tuple(parse_failures),
                review=None,
                workflow_failure="所有根因排查分支均未产出可用结果",
            )

        review: ReviewSynthesis | None = None
        review_failure: str | None = None
        review_brief = _root_cause_review_task_brief(
            incident_context, findings, failed_child_ids, parse_failures
        )
        reviewer_receipt, _ = await _spawn_nested_or_root_reviewer(
            controller,
            session_id=session_id,
            trace_id=trace_id,
            task_brief=review_brief,
            wave=wave,
            open_receipts=open_receipts,
            spawned_reviewers=spawned_reviewers,
        )
        if reviewer_receipt is None:
            review_failure = "复核子 Agent 未产出结果"
        elif reviewer_receipt.status != "COMPLETED" or reviewer_receipt.result_summary is None:
            review_failure = "复核子 Agent 未产出结果"
        else:
            try:
                review = ReviewSynthesis.model_validate_json(reviewer_receipt.result_summary)
            except ValidationError:
                review_failure = "复核子 Agent 输出解析失败"

        return RootCauseOutcome(
            trace_id=trace_id,
            findings=tuple(findings),
            child_ids=tuple(child_ids),
            failed_child_ids=tuple(failed_child_ids),
            parse_failures=tuple(parse_failures),
            review=review,
            workflow_failure=review_failure,
        )
    finally:
        remaining = _dedupe_receipts(
            (*open_receipts, *spawned_reviewers, *(tuple([reviewer_receipt]) if reviewer_receipt else ()))
        )
        await _close_remaining(controller, remaining)
