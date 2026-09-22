"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: agent_sessions.py
@DateTime: 2026-08-13
@Docs: Agent 会话 REST API：创建、列表、详情、归档、历史与发消息触发 chat turn。

实现流程：
1. 全部端点走 require_permission("agent:use")：会话校验与权限判定合并一次查询，
   超管自动放行；没有这个权限的用户完全用不了运维助手（旧版本只做登录校验，
   任何登录用户都能用，属于遗留的权限缺口，这里补上）。
2. 创建会话时写入当前用户 user_id，status 固定为 active；列表复用 list_for_user 分页。
3. 详情 / 归档 / 消息历史先查会话，非所有者、不存在或已归档一律 404，避免枚举他人会话 ID。
4. DELETE 只归档不销毁（R4）：会话从所有者视野里收起，提案与执行结果作为证据保留；
   还有 turn、子 Agent 或待执行/执行中/结果不确定的提案时返回 409 说明原因；
   还没审批的提案随归档撤回，逐条写 hitl_withdrawn 审计。
5. 消息历史优先用 list_for_agent(..., agent_id=None) 只返回根 transcript，按 id 升序。
6. POST messages：归属校验后 claim turn 租约 → 落库用户消息 → run_chat_turn；
   整轮对话拿会话工厂自己开短会话（等模型、等工具时不占连接，R5），本轮消息在结束时
   一次写库并校验租约令牌；请求自己的会话在提交用户消息后就不再持有连接。
   HITL 事件经 BufferedWsHitlEventPublisher 在提交之后再广播。同会话并发请求返回 409；
   本轮租约被接管（旧轮次超时）也返回 409 且不保存结果；finally 释放租约。
   认领租约之前先占并发准入名额（全局 / 单用户同时在跑的轮数），满了 503 / 429 +
   Retry-After，请求结束时归还。
7. 设备查询完整结果只经会话归属专用端点按需返回，且要求当前持有 cmdb:read；总结恢复
   只处理已保存正文，复用幂等总结服务且在消息提交后广播，不触发设备执行或再次使用动态凭据。
8. 快照包含可恢复态提案及已执行查询，但只暴露 payload 中的预览；完整结果存在性
   用一次批量 ID 查询计算，正文绝不进入快照。
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.chat_turn import TurnSupersededError, run_chat_turn
from app.agent.device_result_summary import (
    SUMMARY_STALE_AFTER,
    DeviceQueryResultNotFoundError,
    SummaryDelivery,
    SummaryInProgressError,
    deliver_device_query_summary,
)
from app.agent.hitl_executor import hitl_execution_queue
from app.agent.loop import LoopOutcome
from app.agent.permissions import AUTO_EXECUTE, CMDB_READ
from app.agent.session import append_user_message
from app.agent.turn_admission import turn_admission
from app.agent.turn_registry import turn_registry
from app.agent.ws_hub import BufferedWsHitlEventPublisher, hub
from app.core.database import get_db
from app.core.deps import require_permission
from app.crud import hitl_execution_request as execution_request_crud
from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.crud.agent_session import agent_session_crud
from app.crud.hitl_execution_result import hitl_execution_result_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.crud.user import user_crud
from app.models.agent_message import AgentMessage
from app.models.agent_registry import AgentRegistry
from app.models.agent_session import AgentSession
from app.models.hitl_execution_result import HitlExecutionResult
from app.models.hitl_proposal import HitlProposal
from app.models.user import User
from app.schemas.agent_session import (
    AgentChatTurnResponse,
    AgentMessageCreate,
    AgentMessageResponse,
    AgentSessionApprovalUpdate,
    AgentSessionCreate,
    AgentSessionResponse,
    AgentSessionSnapshotResponse,
    AgentTurnCancelResponse,
    ChildAgentSnapshotResponse,
    DeviceQueryResultResponse,
    HitlProposalSafeResponse,
)
from app.schemas.agent_ws import AgentWsServerMessage
from app.schemas.common import PaginatedData, ResponseEnvelope, paginated_response, success_response
from app.utils.audit import log_audit

logger = logging.getLogger(__name__)

router = APIRouter()


async def _owned_session_or_404(
    db: AsyncSession,
    session_id: int,
    user_id: int,
) -> AgentSession:
    """
    返回当前用户拥有的未归档会话；不存在、非所有者或已归档时抛 404。

    归档的会话对所有者也表现为不存在：不能再发消息、读快照或改档位，
    提案证据改由审批/审计权限按提案 ID 查询。

    Args:
        db: 数据库会话
        session_id: 会话主键
        user_id: 当前登录用户 ID

    Returns:
        归属校验通过的 AgentSession

    Raises:
        HTTPException: 会话不存在、不属于当前用户或已归档时 404
    """
    session = await agent_session_crud.get(db, session_id)
    if session is None or session.user_id != user_id or session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在",
        )
    return session


def _safe_proposal_response(
    proposal: HitlProposal,
    full_result_proposal_ids: set[int],
    execution_state: str | None,
) -> HitlProposalSafeResponse:
    """从白名单字段组装 HITL 安全摘要，避免泄露 action_payload。"""
    payload = proposal.action_payload if isinstance(proposal.action_payload, dict) else {}
    raw_asset_id = payload.get("asset_id")
    asset_id = (
        raw_asset_id if isinstance(raw_asset_id, int) and not isinstance(raw_asset_id, bool) else None
    )
    raw_reason = payload.get("proposal_reason")
    reason = raw_reason if isinstance(raw_reason, str) else ""
    raw_result_excerpt = payload.get("last_result_excerpt")
    result_excerpt = raw_result_excerpt if isinstance(raw_result_excerpt, str) else None
    ticket = hitl_execution_queue.active(proposal.id)
    visible_state = cast(
        Literal["queued", "running", "awaiting_credential"] | None,
        ticket.state if ticket is not None else execution_state,
    )
    return HitlProposalSafeResponse(
        proposal_id=proposal.id,
        action_type=proposal.action_type,
        status=proposal.status,
        status_reason=proposal.status_reason,
        reason=reason,
        asset_id=asset_id,
        created_at=proposal.created_at,
        execution_started_at=proposal.execution_started_at,
        resolved_at=proposal.resolved_at,
        executed_at=proposal.executed_at,
        result_excerpt=result_excerpt,
        has_full_result=proposal.id in full_result_proposal_ids,
        execution_state=visible_state,
    )


def _safe_child_response(child: AgentRegistry) -> ChildAgentSnapshotResponse:
    """从白名单字段组装子 Agent 安全摘要。"""
    return ChildAgentSnapshotResponse(
        child_id=child.child_id,
        role=child.role,
        task_brief=child.task_brief,
        status=child.status,
        result_summary=child.result_summary,
        created_at=child.created_at,
        status_changed_at=child.status_changed_at,
    )


def _snapshot_response(
    messages: list[AgentMessage],
    proposals: list[HitlProposal],
    children: list[AgentRegistry],
    has_more: bool,
    next_before: int | None,
    full_result_proposal_ids: set[int],
    execution_states: dict[int, str],
) -> AgentSessionSnapshotResponse:
    """组装会话快照响应。"""
    return AgentSessionSnapshotResponse(
        messages=[AgentMessageResponse.model_validate(item) for item in messages],
        proposals=[
            _safe_proposal_response(
                item, full_result_proposal_ids, execution_states.get(item.id)
            )
            for item in proposals
        ],
        children=[_safe_child_response(item) for item in children],
        has_more_messages=has_more,
        next_before_message_id=next_before,
    )


def _public_summary_status(
    result_row: HitlExecutionResult,
) -> Literal["pending", "generating", "completed", "fallback"]:
    """把超过五分钟的 generating 仅在公开响应中归一化为 pending。"""
    raw_status = result_row.summary_status
    if raw_status == "generating" and result_row.summary_started_at is not None:
        started_at = result_row.summary_started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        if started_at < datetime.now(UTC) - SUMMARY_STALE_AFTER:
            return "pending"
    return cast(
        Literal["pending", "generating", "completed", "fallback"],
        raw_status,
    )


def _device_query_result_response(
    result_row: HitlExecutionResult,
) -> DeviceQueryResultResponse:
    """从白名单字段组装完整结果响应，不返回总结正文或动作载荷。"""
    return DeviceQueryResultResponse(
        proposal_id=result_row.proposal_id,
        content=result_row.content,
        content_length=result_row.content_length,
        summary_status=_public_summary_status(result_row),
        created_at=result_row.created_at,
    )


async def _owned_device_query_result_or_404(
    db: AsyncSession,
    *,
    session_id: int,
    proposal_id: int,
    user_id: int,
) -> HitlExecutionResult:
    """按会话归属、提案归属和动作类型收敛查询失败为统一 404。"""
    await _owned_session_or_404(db, session_id, user_id)
    proposal = await hitl_proposal_crud.get(db, proposal_id)
    if (
        proposal is None
        or proposal.session_id != session_id
        or proposal.action_type != "device_query"
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="设备查询完整结果不存在",
        )
    result_row = await hitl_execution_result_crud.get_by_proposal(db, proposal_id)
    if result_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="设备查询完整结果不存在",
        )
    return result_row


async def _require_cmdb_read(db: AsyncSession, user: User) -> None:
    """设备命令结果属于资产数据：按当前 cmdb:read 放行。

    历史结果接口不能成为绕过撤销的后门——权限被收回后，完整正文与总结恢复一并不可用。
    """
    if not await user_crud.has_permission_or_superuser(db, user, CMDB_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"无权查看设备命令结果（需要权限：{CMDB_READ}）",
        )


async def _broadcast_summary_delivery(delivery: SummaryDelivery) -> None:
    """只广播本次新建且已提交的总结消息；失败不改变持久化结果。"""
    if not delivery.created_message:
        return
    try:
        await hub.broadcast(
            delivery.session_id,
            AgentWsServerMessage(
                type="assistant_delta",
                payload={"text": delivery.content, "done": True},
            ),
        )
    except Exception as exc:
        logger.warning(
            "设备查询总结广播失败 proposal_id=%s exc_type=%s",
            delivery.proposal_id,
            type(exc).__name__,
        )


@router.post(
    "/sessions",
    response_model=ResponseEnvelope[AgentSessionResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    body: AgentSessionCreate | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[AgentSessionResponse]:
    """为当前用户创建一条 Agent 会话。"""
    payload = body or AgentSessionCreate()
    session = await agent_session_crud.create(
        db,
        {
            "user_id": current_user.id,
            "title": payload.title,
            "status": "active",
        },
    )
    await db.commit()
    # created_at/updated_at 由 server_default=func.now() 生成，必须回读一次拿真实值。
    await db.refresh(session)
    return success_response(
        AgentSessionResponse.model_validate(session),
        message="创建成功",
        code=status.HTTP_201_CREATED,
    )


@router.get(
    "/sessions",
    response_model=ResponseEnvelope[PaginatedData[AgentSessionResponse]],
)
async def list_sessions(
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[PaginatedData[AgentSessionResponse]]:
    """分页列出当前用户的会话（最新在前）。"""
    sessions, total = await agent_session_crud.list_for_user(
        db,
        current_user.id,
        skip=(page - 1) * page_size,
        limit=page_size,
    )
    items = [AgentSessionResponse.model_validate(item) for item in sessions]
    return paginated_response(items, total, page, page_size)


@router.get(
    "/sessions/{session_id}",
    response_model=ResponseEnvelope[AgentSessionResponse],
)
async def get_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[AgentSessionResponse]:
    """获取会话详情；非所有者返回 404。"""
    session = await _owned_session_or_404(db, session_id, current_user.id)
    return success_response(AgentSessionResponse.model_validate(session))


@router.patch(
    "/sessions/{session_id}",
    response_model=ResponseEnvelope[AgentSessionResponse],
)
async def patch_session_approval_mode(
    session_id: int,
    body: AgentSessionApprovalUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[AgentSessionResponse]:
    """更新会话审批模式；非所有者返回 404；相同档位不写审计。

    切到 assist/full 需要独立的 agent:auto_execute 权限：这两个档位会让门控自动批准
    并执行设备命令，是和「使用运维助手」「人工审批」不同的责任。前端的确认弹窗
    只是提醒，不是授权。切回 ask 永远放行，不能因为缺权限把人困在自动档位里。
    """
    session = await _owned_session_or_404(db, session_id, current_user.id)
    if body.approval_mode != "ask" and not await user_crud.has_permission_or_superuser(
        db, current_user, AUTO_EXECUTE
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"无权开启自动执行档位（需要权限：{AUTO_EXECUTE}）",
        )
    old_mode = session.approval_mode
    if old_mode != body.approval_mode:
        updated = await agent_session_crud.update(
            db,
            session_id,
            {"approval_mode": body.approval_mode},
        )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent 会话不存在",
            )
        session = updated
        await log_audit(
            db,
            user_id=current_user.id,
            action="update_session_approval_mode",
            target=f"agent_session:{session_id}",
            detail=f"{old_mode}→{body.approval_mode}",
        )
    # sessionmaker 配了 expire_on_commit=False，且本端点只改 approval_mode
    # （updated_at 走 Python 侧 onupdate），commit 后对象属性依然可读，
    # 不需要再发一条 SELECT 回读。
    await db.commit()
    return success_response(AgentSessionResponse.model_validate(session))


# 准入满时建议客户端多久后重试：普通一轮对话十几秒到一两分钟，给个短间隔让用户很快能再试
_TURN_RETRY_AFTER_SECONDS = 10

# 拒绝归档时告诉用户还差什么；聊天一收起就没人能继续核实这些事了
_ARCHIVE_REFUSAL_MESSAGES: dict[str, str] = {
    "active_turn": "这个会话还有一轮对话正在进行，请等它结束或先停止后再归档",
    "active_children": "这个会话还有子 Agent 在运行，请等它们结束后再归档",
    "unsettled_proposals": (
        "这个会话还有已批准待执行、执行中或结果不确定的提案，"
        "请先重试执行或人工核实结果后再归档"
    ),
}


@router.delete(
    "/sessions/{session_id}",
    response_model=ResponseEnvelope[None],
)
async def delete_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[None]:
    """归档当前用户拥有的会话（聊天收起，证据保留）；非所有者或不存在返回 404。"""
    await _owned_session_or_404(db, session_id, current_user.id)
    result = await agent_session_crud.archive(db, session_id)
    if result.outcome == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在",
        )
    if result.outcome != "archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_ARCHIVE_REFUSAL_MESSAGES[result.outcome],
        )
    for proposal_id in result.withdrawn_proposal_ids:
        await log_audit(
            db,
            user_id=current_user.id,
            action="hitl_withdrawn",
            target=f"hitl_proposal:{proposal_id}",
            detail="会话归档时撤回待审批提案",
        )
    await db.commit()
    return success_response(None, message="已归档")


@router.get(
    "/sessions/{session_id}/messages",
    response_model=ResponseEnvelope[list[AgentMessageResponse]],
)
async def list_session_messages(
    session_id: int,
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[list[AgentMessageResponse]]:
    """返回根 Agent 的会话历史（不含子 Agent 私有消息）。"""
    await _owned_session_or_404(db, session_id, current_user.id)
    messages = await agent_message_crud.list_for_agent(
        db,
        session_id,
        agent_id=None,
        limit=limit,
    )
    return success_response([AgentMessageResponse.model_validate(item) for item in messages])


@router.get(
    "/sessions/{session_id}/device-query-results/{proposal_id}",
    response_model=ResponseEnvelope[DeviceQueryResultResponse],
)
async def get_device_query_result(
    session_id: int,
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[DeviceQueryResultResponse]:
    """让当前会话所有者按需读取设备查询完整正文。"""
    await _owned_session_or_404(db, session_id, current_user.id)
    await _require_cmdb_read(db, current_user)
    result_row = await _owned_device_query_result_or_404(
        db,
        session_id=session_id,
        proposal_id=proposal_id,
        user_id=current_user.id,
    )
    return success_response(_device_query_result_response(result_row))


@router.post(
    "/sessions/{session_id}/device-query-results/{proposal_id}/summary",
    response_model=ResponseEnvelope[DeviceQueryResultResponse],
)
async def recover_device_query_summary(
    session_id: int,
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[DeviceQueryResultResponse]:
    """只对已保存正文恢复总结，不重新连接设备。"""
    await _owned_session_or_404(db, session_id, current_user.id)
    await _require_cmdb_read(db, current_user)
    await _owned_device_query_result_or_404(
        db,
        session_id=session_id,
        proposal_id=proposal_id,
        user_id=current_user.id,
    )
    engine = db.bind
    if engine is None:
        raise RuntimeError("数据库会话未绑定 AsyncEngine")
    await db.rollback()
    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )
    try:
        delivery = await deliver_device_query_summary(
            session_factory=session_factory,
            proposal_id=proposal_id,
        )
    except SummaryInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="设备查询结果正在生成总结",
        ) from exc
    except DeviceQueryResultNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="设备查询完整结果不存在",
        ) from exc

    await _broadcast_summary_delivery(delivery)
    result_row = await hitl_execution_result_crud.get_by_proposal(db, proposal_id)
    if result_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="设备查询完整结果不存在",
        )
    return success_response(_device_query_result_response(result_row))


@router.get(
    "/sessions/{session_id}/snapshot",
    response_model=ResponseEnvelope[AgentSessionSnapshotResponse],
)
async def get_session_snapshot(
    session_id: int,
    before_message_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[AgentSessionSnapshotResponse]:
    """返回可恢复的会话安全快照：根消息 cursor 分页 + 提案与子 Agent 摘要。"""
    await _owned_session_or_404(db, session_id, current_user.id)
    messages, has_more = await agent_message_crud.list_root_before_id(
        db, session_id, before_id=before_message_id, limit=limit
    )
    proposals = await hitl_proposal_crud.list_snapshot_for_session(db, session_id)
    full_result_proposal_ids = await hitl_execution_result_crud.existing_proposal_ids(
        db, [proposal.id for proposal in proposals]
    )
    children = await agent_registry_crud.list_snapshot_for_session(db, session_id)
    open_requests = await execution_request_crud.open_requests_for(
        db, [proposal.id for proposal in proposals]
    )
    execution_states = {proposal_id: row.status for proposal_id, row in open_requests.items()}
    next_before = messages[0].id if has_more and messages else None
    return success_response(
        _snapshot_response(
            messages,
            proposals,
            children,
            has_more,
            next_before,
            full_result_proposal_ids,
            execution_states,
        )
    )


async def _finalize_turn(db: AsyncSession, session_id: int, turn_token: str) -> None:
    """丢弃本轮未提交内容并释放 turn 租约，任何失败都不让异常盖掉响应。

    正常路径就是原来的 rollback → release → commit 三步。

    多一层兜底是因为取消可能正好打断一次数据库操作，SQLAlchemy 明确说明这种情况下
    连接状态不可预期，`db` 后续可能整个不可用。租约释放不了的话，用户要一直等到
    AGENT_TURN_LEASE_TIMEOUT_SECONDS 超时才能再发消息——而「停下来马上重问」正是
    点停止的人想做的事，所以这里换一个独立会话再试一次。
    """
    try:
        await db.rollback()
        await agent_session_crud.release_turn(db, session_id, turn_token)
        await db.commit()
        return
    except Exception:
        logger.warning(
            "会话 %s 释放租约时原会话不可用，改用独立会话重试", session_id, exc_info=True
        )

    engine = db.bind
    if engine is None:
        return
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as fresh_db:
            await agent_session_crud.release_turn(fresh_db, session_id, turn_token)
            await fresh_db.commit()
    except Exception:
        logger.exception("会话 %s 释放租约彻底失败，只能等租约超时兜底", session_id)


@router.post(
    "/sessions/{session_id}/messages",
    response_model=ResponseEnvelope[AgentChatTurnResponse],
)
async def post_session_message(
    session_id: int,
    body: AgentMessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[AgentChatTurnResponse]:
    """
    发送用户消息并触发一轮 Agent turn。

    实时事件经 WebSocket 推送；本接口返回 turn 摘要。失败时尽量保留用户消息。
    同时在跑的轮数超过并发准入上限时立即返回 429（本用户）/ 503（全局），不排队。
    """
    await _owned_session_or_404(db, session_id, current_user.id)
    decision = turn_admission.try_acquire(current_user.id)
    if decision != "ok":
        user_limit = decision == "user_limit"
        raise HTTPException(
            status_code=(
                status.HTTP_429_TOO_MANY_REQUESTS
                if user_limit
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=(
                "你同时进行的对话太多，请等其中一个结束后稍后再试"
                if user_limit
                else "运维助手当前繁忙，请稍后再试"
            ),
            headers={"Retry-After": str(_TURN_RETRY_AFTER_SECONDS)},
        )
    try:
        return await _run_turn(db, session_id=session_id, body=body, current_user=current_user)
    finally:
        turn_admission.release(current_user.id)


async def _run_turn(
    db: AsyncSession,
    *,
    session_id: int,
    body: AgentMessageCreate,
    current_user: User,
) -> ResponseEnvelope[AgentChatTurnResponse]:
    """已占到准入名额后的一轮对话：认领租约 → 落库用户消息 → 跑 turn → 释放租约。"""
    turn_token = str(uuid4())
    if not await agent_session_crud.claim_turn(db, session_id, turn_token):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该会话正在处理上一条消息",
        )
    await db.commit()

    hitl_publisher = BufferedWsHitlEventPublisher()
    outcome = None
    try:
        await append_user_message(db, session_id, body.content)
        await db.commit()
        # 包成 task 才能被取消端点拿到句柄。await task 本身被取消时（客户端断开、
        # 进程关停）asyncio 会把取消传进 task，所以这层包装不改变断开时的既有行为。
        #
        # 整轮对话不用请求的 db：它拿同一引擎的会话工厂，每段读写各开短会话，等模型、
        # 等工具时不占连接（R5）；本轮消息在结束时由它自己一次提交。请求的 db 在上面
        # 提交之后已经归还了连接，之后只在 finally 里释放租约时再用一下。
        turn_task = asyncio.create_task(
            run_chat_turn(
                async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False),
                session_id=session_id,
                actor_user_id=current_user.id,
                publisher=hitl_publisher,
                turn_token=turn_token,
            ),
            name=f"turn:{session_id}",
        )
        turn_registry.register(session_id, turn_token, turn_task)
        outcome = await turn_task
    except asyncio.CancelledError:
        # 只有用户主动点「停止」才转成正常响应；客户端断开或进程关停必须原样传播，
        # 吞掉那种取消会让关停挂住、或留下一个永远返回不了的请求。
        if not turn_registry.was_cancelled_by_user(session_id, turn_token):
            raise
        # C2：本轮已产出的 assistant/tool 消息只在被取消的 turn 任务内存里，随任务一起
        # 丢弃，不保留、不写取消标记（项目所有者决定，见 design-turn-cancellation.md §10）。
        outcome = LoopOutcome(reason="cancelled", final_answer=None)
        # 先 flush 再播 turn_done：取消如果正好打断一次自动批准的执行，
        # execute_approved_proposal 会在它自己的短会话里把提案落成 UNKNOWN 并
        # 经同一个 publisher 发事件，前端要先收到那张卡片的更新再收到「本轮结束」。
        await hitl_publisher.flush()
        await hub.broadcast(
            session_id,
            AgentWsServerMessage(
                type="turn_done",
                payload={"reason": "cancelled", "control": None},
            ),
        )
    except TurnSupersededError as exc:
        await hitl_publisher.flush()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="本轮对话耗时过长，已被后来的请求接管，结果没有保存",
        ) from exc
    except Exception as exc:
        await db.rollback()
        await hitl_publisher.flush()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="本轮对话处理失败，请稍后重试",
        ) from exc
    finally:
        turn_registry.unregister(session_id, turn_token)
        await _finalize_turn(db, session_id, turn_token)

    await hitl_publisher.flush()
    return success_response(
        AgentChatTurnResponse(
            reason=outcome.reason,
            final_answer=outcome.final_answer,
            control=outcome.control,
        ),
        message="处理完成",
    )


@router.post(
    "/sessions/{session_id}/turn/cancel",
    response_model=ResponseEnvelope[AgentTurnCancelResponse],
)
async def cancel_session_turn(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[AgentTurnCancelResponse]:
    """
    撤回本次提问，立即中止正在跑的这一轮回答。

    本轮已经生成但未提交的助手消息与工具结果全部丢弃，会话回到发消息之前的样子；
    租约随即释放，用户可以马上重新提问。

    幂等：没有正在跑的 turn 时返回 200 + cancelled=false，不报错。
    """
    await _owned_session_or_404(db, session_id, current_user.id)
    outcome = turn_registry.request_cancel(session_id, by_user_id=current_user.id)
    return success_response(
        AgentTurnCancelResponse(cancelled=outcome.cancelled),
        message="已停止本轮回答" if outcome.cancelled else "本轮回答已经结束",
    )
