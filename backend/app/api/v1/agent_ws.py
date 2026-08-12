"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: agent_ws.py
@DateTime: 2026-08-12 12:32
@Docs: Agent WebSocket 路由：JWT 鉴权、会话归属校验、Hub 注册/注销。

实现流程：
1. 浏览器默认用 ``?access_token=<jwt>`` 连接；若无 query token，则等待首帧
   ``{"type":"auth","access_token":"..."}``（超时未认证则关闭）。
2. 鉴权复用 HTTP 路径的 decode_token + get_active_session_user（校验用户与 refresh family），
   再查 agent_session：必须存在、归属当前用户、status=active。
3. 失败用 WS close code（4401/4403/4404）+ 中文 reason；成功则注册到 AgentWsHub。
4. 数据库会话仅用于鉴权/归属校验，鉴权结束后立即释放，避免长连接占住连接池
   （测试里 StaticPool 单连接时尤其会死锁）。
5. 连接保持接收循环；任意退出路径都在 finally 里 disconnect，避免泄漏。
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ws_hub import hub
from app.core.database import get_db
from app.core.security import decode_token
from app.crud.agent_session import agent_session_crud
from app.models.user import User
from app.schemas.agent_ws import AgentWsClientAuth
from app.services.auth import get_active_session_user

logger = logging.getLogger(__name__)

router = APIRouter()

# 无 query token 时等待首帧鉴权的秒数
_AUTH_FRAME_TIMEOUT_SECONDS = 5.0

_CLOSE_UNAUTHORIZED = 4401
_CLOSE_FORBIDDEN = 4403
_CLOSE_NOT_FOUND = 4404


@asynccontextmanager
async def _auth_db_session(websocket: WebSocket) -> AsyncIterator[AsyncSession]:
    """
    打开短生命周期 DB 会话，并尊重 ``app.dependency_overrides[get_db]``。

    Args:
        websocket: 当前 WebSocket（用于读取 FastAPI app 上的依赖覆盖）

    Yields:
        可用于鉴权查询的 AsyncSession；退出上下文后立即关闭
    """
    dep: Callable[[], AsyncIterator[AsyncSession]] = websocket.app.dependency_overrides.get(
        get_db,
        get_db,
    )
    # dependency_overrides 与 get_db 均为异步生成器，需 aclose 以归还会话
    agen = cast(AsyncGenerator[AsyncSession], dep())
    session = await agen.__anext__()
    try:
        yield session
    finally:
        await agen.aclose()


async def _close_ws(websocket: WebSocket, code: int, reason: str) -> None:
    """关闭 WebSocket，忽略已断开时的二次关闭错误。"""
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        logger.debug("关闭 WebSocket 时忽略异常 code=%s", code, exc_info=True)


async def _resolve_access_token(
    websocket: WebSocket,
    query_token: str | None,
) -> str | None:
    """
    从 query 或首帧取得 access_token。

    Args:
        websocket: 已 accept 的连接
        query_token: URL 查询参数中的 token，可为空

    Returns:
        非空 token 字符串；超时或首帧非法时返回 None（调用方应发 4401）
    """
    if query_token:
        return query_token

    try:
        raw: dict[str, Any] = await asyncio.wait_for(
            websocket.receive_json(),
            timeout=_AUTH_FRAME_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return None
    except WebSocketDisconnect:
        return None
    except Exception:
        return None

    try:
        auth = AgentWsClientAuth.model_validate(raw)
    except ValidationError:
        return None
    return auth.access_token


async def _authenticate_user(db: AsyncSession, access_token: str) -> User | None:
    """
    校验 JWT 并加载活跃会话用户（与 get_current_user 同语义）。

    Args:
        db: 数据库会话
        access_token: JWT 字符串

    Returns:
        通过校验的用户；失败返回 None
    """
    try:
        payload = decode_token(access_token)
        if payload.type != "access":
            return None
        user_id = payload.user_id
    except (jwt.InvalidTokenError, ValueError):
        return None

    return await get_active_session_user(
        db,
        user_id=user_id,
        family_id=payload.sid,
        token_version=payload.ver,
    )


@router.websocket("/agent/{session_id}")
async def agent_session_ws(
    websocket: WebSocket,
    session_id: int,
    access_token: str | None = Query(default=None),
) -> None:
    """
    Agent 会话实时通道：鉴权通过后注册到 Hub 并保持连接。

    Args:
        websocket: WebSocket 连接
        session_id: Agent 会话主键
        access_token: 可选 query JWT（前端默认路径）
    """
    await websocket.accept()
    registered = False

    try:
        token = await _resolve_access_token(websocket, access_token)
        if not token:
            await _close_ws(websocket, _CLOSE_UNAUTHORIZED, "未提供有效认证凭据")
            return

        async with _auth_db_session(websocket) as db:
            user = await _authenticate_user(db, token)
            if user is None:
                await _close_ws(websocket, _CLOSE_UNAUTHORIZED, "Token 无效或已过期")
                return

            session = await agent_session_crud.get(db, session_id)
            if session is None:
                await _close_ws(websocket, _CLOSE_NOT_FOUND, "会话不存在")
                return
            if session.user_id != user.id or session.status != "active":
                await _close_ws(websocket, _CLOSE_FORBIDDEN, "无权访问该会话")
                return

        await hub.connect(session_id, websocket)
        registered = True

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        if registered:
            await hub.disconnect(session_id, websocket)
