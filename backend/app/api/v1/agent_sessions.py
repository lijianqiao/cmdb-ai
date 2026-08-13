"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: agent_sessions.py
@DateTime: 2026-08-13
@Docs: Agent 会话 REST API：创建、列表、详情、硬删除、历史与发消息触发 chat turn。

实现流程：
1. 全部端点走 require_permission("agent:use")：会话校验与权限判定合并一次查询，
   超管自动放行；没有这个权限的用户完全用不了运维助手（旧版本只做登录校验，
   任何登录用户都能用，属于遗留的权限缺口，这里补上）。
2. 创建会话时写入当前用户 user_id，status 固定为 active；列表复用 list_for_user 分页。
3. 详情 / 删除 / 消息历史先查会话，非所有者或不存在一律 404，避免枚举他人会话 ID。
4. DELETE 为物理删除；消息、HITL、registry、trace 依赖库级 ON DELETE CASCADE。
5. 消息历史优先用 list_for_agent(..., agent_id=None) 只返回根 transcript，按 id 升序。
6. POST messages：归属校验后调用 run_chat_turn（复用 run_loop + root dispatcher + WS 推送），
   整轮结束后一次 commit；HITL 事件经 BufferedWsHitlEventPublisher 在 commit 之后再广播。
   异常时仍尽量 commit 已写入的用户消息。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.chat_turn import run_chat_turn
from app.agent.ws_hub import BufferedWsHitlEventPublisher
from app.core.database import get_db
from app.core.deps import require_permission
from app.crud.agent_message import agent_message_crud
from app.crud.agent_session import agent_session_crud
from app.models.agent_session import AgentSession
from app.models.user import User
from app.schemas.agent_session import (
    AgentChatTurnResponse,
    AgentMessageCreate,
    AgentMessageResponse,
    AgentSessionApprovalUpdate,
    AgentSessionCreate,
    AgentSessionResponse,
)
from app.schemas.common import PaginatedData, ResponseEnvelope, paginated_response, success_response
from app.utils.audit import log_audit

router = APIRouter()


async def _owned_session_or_404(
    db: AsyncSession,
    session_id: int,
    user_id: int,
) -> AgentSession:
    """
    返回当前用户拥有的会话；不存在或非所有者时抛 404。

    Args:
        db: 数据库会话
        session_id: 会话主键
        user_id: 当前登录用户 ID

    Returns:
        归属校验通过的 AgentSession

    Raises:
        HTTPException: 会话不存在或不属于当前用户时 404
    """
    session = await agent_session_crud.get(db, session_id)
    if session is None or session.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在",
        )
    return session


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
    """更新会话审批模式；非所有者返回 404；相同档位不写审计。"""
    session = await _owned_session_or_404(db, session_id, current_user.id)
    old_mode = session.approval_mode
    if old_mode != body.approval_mode:
        session = await agent_session_crud.update(
            db,
            session_id,
            {"approval_mode": body.approval_mode},
        )
        await log_audit(
            db,
            user_id=current_user.id,
            action="update_session_approval_mode",
            target=f"agent_session:{session_id}",
            detail=f"{old_mode}→{body.approval_mode}",
        )
    await db.commit()
    await db.refresh(session)
    return success_response(AgentSessionResponse.model_validate(session))


@router.delete(
    "/sessions/{session_id}",
    response_model=ResponseEnvelope[None],
)
async def delete_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[None]:
    """硬删除当前用户拥有的会话；非所有者或不存在返回 404。"""
    await _owned_session_or_404(db, session_id, current_user.id)
    deleted = await agent_session_crud.hard_delete(db, session_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在",
        )
    await db.commit()
    return success_response(None, message="删除成功")


@router.get(
    "/sessions/{session_id}/messages",
    response_model=ResponseEnvelope[list[AgentMessageResponse]],
)
async def list_session_messages(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:use")),
) -> ResponseEnvelope[list[AgentMessageResponse]]:
    """返回根 Agent 的会话历史（不含子 Agent 私有消息）。"""
    await _owned_session_or_404(db, session_id, current_user.id)
    messages = await agent_message_crud.list_for_agent(
        db,
        session_id,
        agent_id=None,
    )
    return success_response([AgentMessageResponse.model_validate(item) for item in messages])


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
    """
    await _owned_session_or_404(db, session_id, current_user.id)
    # HITL 事件缓冲到 commit 之后再广播：提案行在本轮事务内创建，如果事件
    # 提前发出，前端收到后立即用另一个 DB 会话拉提案详情会拿到 404。
    hitl_publisher = BufferedWsHitlEventPublisher()
    try:
        outcome = await run_chat_turn(
            db,
            session_id=session_id,
            actor_user_id=current_user.id,
            content=body.content,
            publisher=hitl_publisher,
        )
    except Exception as exc:
        # 用户消息已在 turn 内写入；先提交再返回 500，避免整轮回滚丢原话
        await db.commit()
        await hitl_publisher.flush()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="本轮对话处理失败，请稍后重试",
        ) from exc

    await db.commit()
    await hitl_publisher.flush()
    return success_response(
        AgentChatTurnResponse(
            reason=outcome.reason,
            final_answer=outcome.final_answer,
            control=outcome.control,
        ),
        message="处理完成",
    )
