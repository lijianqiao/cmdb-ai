"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: agent_ws.py
@DateTime: 2026-08-12 12:32
@Docs: Agent WebSocket 路由：JWT 鉴权、agent:use 权限、会话归属校验、Hub 注册/注销。

实现流程：
1. 浏览器默认用 ``?access_token=<jwt>`` 连接；若无 query token，则等待首帧
   ``{"type":"auth","access_token":"..."}``（超时未认证则关闭）。
2. 鉴权复用 HTTP 路径 require_permission 同款的 get_authorized_session_user
   （校验用户与 refresh family，并在同一次查询里判定 agent:use 权限，超管自动
   放行），再查 agent_session：必须存在、归属当前用户、status=active。
3. 失败用 WS close code（4401/4403/4404）+ 中文 reason；成功则注册到 AgentWsHub。
4. 连接建立后不是"鉴权一次就永久信任"：receive 循环之外并发跑一个周期性复查
   任务，定期重新核对 token/权限/会话状态，一旦被撤销就主动关闭连接，不让
   旧连接在权限收回后继续收事件。
5. 数据库会话仅用于鉴权/归属校验，每次校验结束后立即释放，避免长连接占住
   连接池（测试里 StaticPool 单连接时尤其会死锁）。
6. 任意退出路径都在 finally 里取消后台任务、disconnect，避免泄漏。
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ws_hub import hub
from app.core.database import get_db
from app.core.security import decode_token
from app.crud.agent_session import agent_session_crud
from app.schemas.agent_ws import AgentWsClientAuth
from app.schemas.auth import TokenPayload
from app.services.auth import AuthorizedUser, get_authorized_session_user

logger = logging.getLogger(__name__)

router = APIRouter()

# 无 query token 时等待首帧鉴权的秒数
_AUTH_FRAME_TIMEOUT_SECONDS = 5.0
# 连接建立后周期性复查 token/权限/会话状态的间隔
_REAUTH_INTERVAL_SECONDS = 60.0
_AGENT_USE_PERMISSION = "agent:use"
_MONITOR_READ_PERMISSION = "monitor:read"

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


async def _authenticate_and_authorize_user(
    db: AsyncSession, access_token: str
) -> tuple[AuthorizedUser, TokenPayload] | None:
    """
    校验 JWT、加载活跃会话用户，并在同一次查询里核对 agent:use 权限。

    与 HTTP 路径的 require_permission 同语义：会话失效返回 None（调用方发 401）；
    会话有效但既非超管又没有 agent:use 时，has_permission 为 False（调用方发 403）。

    Args:
        db: 数据库会话
        access_token: JWT 字符串

    Returns:
        (授权结果, 解码后的 token payload)；token 本身无效或用户/会话不可用时返回 None
    """
    try:
        payload = decode_token(access_token)
        if payload.type != "access":
            return None
    except (jwt.InvalidTokenError, ValueError):
        return None

    authorized = await get_authorized_session_user(
        db,
        user_id=payload.user_id,
        family_id=payload.sid,
        token_version=payload.ver,
        permission_code=_AGENT_USE_PERMISSION,
    )
    if authorized is None:
        return None
    return authorized, payload


async def _can_read_monitor(
    db: AsyncSession,
    *,
    user_id: int,
    family_id: str,
    token_version: int,
) -> bool:
    """
    判定用户当前是否具备 monitor:read 权限（超管自动放行）。

    Args:
        db: 数据库会话
        user_id: 用户主键
        family_id: refresh token family
        token_version: token 版本号

    Returns:
        具备监控读权限时为 True；会话失效或无权限时为 False
    """
    authorized = await get_authorized_session_user(
        db,
        user_id=user_id,
        family_id=family_id,
        token_version=token_version,
        permission_code=_MONITOR_READ_PERMISSION,
    )
    return bool(
        authorized is not None
        and (authorized.user.is_superuser or authorized.has_permission)
    )


async def _drain_until_disconnect(websocket: WebSocket) -> None:
    """阻塞直到客户端断开；不关心入站消息的具体内容。"""
    while True:
        try:
            message = await websocket.receive()
        except WebSocketDisconnect:
            return
        if message["type"] == "websocket.disconnect":
            return


async def _periodic_reauth(
    websocket: WebSocket,
    *,
    user_id: int,
    family_id: str,
    token_version: int,
    session_id: int,
    interval_seconds: float,
) -> None:
    """
    周期性复查 token 有效性、agent:use 权限与会话归属，任一失效就主动关闭连接。

    连接建立时的鉴权只证明"当时"有效；账号被禁用、权限被收回或会话被关闭后，
    已建立的连接不应该继续收事件。
    """
    while True:
        await asyncio.sleep(interval_seconds)
        async with _auth_db_session(websocket) as db:
            authorized = await get_authorized_session_user(
                db,
                user_id=user_id,
                family_id=family_id,
                token_version=token_version,
                permission_code=_AGENT_USE_PERMISSION,
            )
            if authorized is None:
                await _close_ws(websocket, _CLOSE_UNAUTHORIZED, "Token 已撤销或用户不可用")
                return
            if not authorized.user.is_superuser and not authorized.has_permission:
                await _close_ws(websocket, _CLOSE_FORBIDDEN, "权限已被收回")
                return
            session = await agent_session_crud.get(db, session_id)
            if session is None or session.user_id != user_id or session.status != "active":
                await _close_ws(websocket, _CLOSE_FORBIDDEN, "无权访问该会话")
                return
            can_read_monitor = await _can_read_monitor(
                db,
                user_id=user_id,
                family_id=family_id,
                token_version=token_version,
            )
            hub.update_monitor_access(
                session_id,
                websocket,
                can_read_monitor=can_read_monitor,
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
            resolved = await _authenticate_and_authorize_user(db, token)
            if resolved is None:
                await _close_ws(websocket, _CLOSE_UNAUTHORIZED, "Token 无效或已过期")
                return
            authorized, payload = resolved
            if not authorized.user.is_superuser and not authorized.has_permission:
                await _close_ws(
                    websocket,
                    _CLOSE_FORBIDDEN,
                    f"无权限执行此操作（需要权限：{_AGENT_USE_PERMISSION}）",
                )
                return
            user = authorized.user

            session = await agent_session_crud.get(db, session_id)
            if session is None:
                await _close_ws(websocket, _CLOSE_NOT_FOUND, "会话不存在")
                return
            if session.user_id != user.id or session.status != "active":
                await _close_ws(websocket, _CLOSE_FORBIDDEN, "无权访问该会话")
                return
            can_read_monitor = await _can_read_monitor(
                db,
                user_id=user.id,
                family_id=payload.sid,
                token_version=payload.ver,
            )

        await hub.connect(session_id, websocket, can_read_monitor=can_read_monitor)
        registered = True

        receive_task = asyncio.create_task(_drain_until_disconnect(websocket))
        reauth_task = asyncio.create_task(
            _periodic_reauth(
                websocket,
                user_id=payload.user_id,
                family_id=payload.sid,
                token_version=payload.ver,
                session_id=session_id,
                interval_seconds=_REAUTH_INTERVAL_SECONDS,
            )
        )
        try:
            await asyncio.wait({receive_task, reauth_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            receive_task.cancel()
            reauth_task.cancel()
            with suppress(asyncio.CancelledError):
                await receive_task
            with suppress(asyncio.CancelledError):
                await reauth_task
    except WebSocketDisconnect:
        pass
    finally:
        if registered:
            await hub.disconnect(session_id, websocket)
