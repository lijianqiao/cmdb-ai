"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: agent_sessions.py
@DateTime: 2026-08-12 12:43
@Docs: Agent 会话 REST API：创建、列表、详情与根 transcript 历史。

实现流程：
1. 全部端点走 get_current_user 登录校验；Chat 页面对登录用户开放，无额外权限码。
2. 创建会话时写入当前用户 user_id，status 固定为 active；列表复用 list_for_user 分页。
3. 详情与消息历史先查会话，非所有者或不存在一律 404，避免枚举他人会话 ID。
4. 消息历史优先用 list_for_agent(..., agent_id=None) 只返回根 transcript，按 id 升序。
5. 本文件不含 POST messages / chat turn（留给后续任务）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.crud.agent_message import agent_message_crud
from app.crud.agent_session import agent_session_crud
from app.models.agent_session import AgentSession
from app.models.user import User
from app.schemas.agent_session import (
    AgentMessageResponse,
    AgentSessionCreate,
    AgentSessionResponse,
)
from app.schemas.common import PaginatedData, ResponseEnvelope, paginated_response, success_response

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
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
) -> ResponseEnvelope[AgentSessionResponse]:
    """获取会话详情；非所有者返回 404。"""
    session = await _owned_session_or_404(db, session_id, current_user.id)
    return success_response(AgentSessionResponse.model_validate(session))


@router.get(
    "/sessions/{session_id}/messages",
    response_model=ResponseEnvelope[list[AgentMessageResponse]],
)
async def list_session_messages(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseEnvelope[list[AgentMessageResponse]]:
    """返回根 Agent 的会话历史（不含子 Agent 私有消息）。"""
    await _owned_session_or_404(db, session_id, current_user.id)
    messages = await agent_message_crud.list_for_agent(
        db,
        session_id,
        agent_id=None,
    )
    return success_response(
        [AgentMessageResponse.model_validate(item) for item in messages]
    )
