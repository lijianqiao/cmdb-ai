"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl.py
@DateTime: 2026-08-12 11:36
@Docs: HITL 提案查询与人工审批 HTTP API。

实现流程：
1. 审批/重试/核实与列表端点以 agent:hitl_approve 门控，审批人可看到完整 action_payload；
   单个提案详情另允许审计员（audit:read）查看，用于按审计项里的提案 ID 查证据（R4）。
2. 列表与详情直接复用 hitl_proposal_crud 的会话查询与按 ID 读取。
3. 批准只提交审批并把执行请求写入同一事务（agent/hitl_executor），立刻返回 202
   （R8 第二阶段）：先占队列名额，排不进就 503 且审批不提交；拒绝不排队，照旧 200。
4. 重试同样排进后台队列返回 202；该提案已在排队/执行时直接返回已有的执行请求，不重复入队。
   动态密码在进程重启后丢失的，请求停在 awaiting_credential，用户重新输入后才再次认领。
5. 所有提案响应带 execution_state（queued / running / awaiting_credential / null）与
   execution_request_id。内存里的任务优先；进程重启后从执行请求表恢复。
6. 设备查询执行成功后的总结由后台任务单独生成并广播；总结失败不改变设备执行结果。
7. decide/resolve 注入 BufferedWsHitlEventPublisher，在 db.commit() 之后 flush，
   避免前端收到 hitl_* 事件后立刻 GET 提案却读不到未提交的行。
8. 异常映射：缺失 404、非法迁移 409、提案校验拒绝 400；事务内审计后统一 commit。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.hitl import HitlProposalRejectedError, decide_proposal
from app.agent.hitl_executor import ExecutionTicket, hitl_execution_queue
from app.agent.ws_hub import BufferedWsHitlEventPublisher
from app.core.database import get_db
from app.core.deps import get_client_ip, get_current_user, require_permission
from app.crud import hitl_execution_request as execution_request_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import InvalidHitlTransitionError, hitl_proposal_crud
from app.crud.user import user_crud
from app.models.hitl_execution_request import HitlExecutionRequest
from app.models.hitl_proposal import HitlProposal
from app.models.user import User
from app.schemas.common import ResponseEnvelope, success_response
from app.schemas.hitl import (
    HitlDecideRequest,
    HitlProposalResponse,
    HitlRetryRequest,
    HitlUnknownResolutionRequest,
)
from app.utils.audit import log_audit

logger = logging.getLogger(__name__)

router = APIRouter()

# 执行队列满时建议多久后再批：一条设备命令一般十几秒到一分钟
_EXECUTION_RETRY_AFTER_SECONDS = 15


def _reveal_password(secret: SecretStr | None) -> str | None:
    """在调用执行链之前解开一次性口令。

    明文只在这一个位置产生，从这里往下（后台执行任务 → 执行器 → Netmiko）
    是短暂的内存传递，不落库、不进日志、不进 ExecutionResult.detail。
    集中成一个函数是为了让「明文从哪来」在代码里只有一个可搜的答案。
    """
    return secret.get_secret_value() if secret is not None else None


def _session_factory_for(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """后台执行任务用的会话工厂：与请求同一引擎，但不依赖已经结束的请求会话。"""
    return async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)


def _start_execution(
    ticket: ExecutionTicket,
    *,
    db: AsyncSession,
    actor_user_id: int,
    dynamic_password: str | None,
    actor_ip: str,
    credential_kind: str,
) -> None:
    """提交之后立刻启动。这个函数本身不 await，避免名额占上了任务却没起来。"""
    hitl_execution_queue.start(
        ticket,
        session_factory=_session_factory_for(db),
        actor_user_id=actor_user_id,
        dynamic_password=dynamic_password,
        actor_ip=actor_ip,
        credential_kind=credential_kind,
    )


def _queue_full_error(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
        headers={"Retry-After": str(_EXECUTION_RETRY_AFTER_SECONDS)},
    )


async def _credential_kind(db: AsyncSession, proposal: HitlProposal) -> str:
    """设备动作记 static 或 dynamic；其它动作是 none。不读取密码本身。"""
    if proposal.action_type not in ("device_query", "device_control"):
        return "none"
    raw_asset_id = proposal.action_payload.get("asset_id")
    asset = (
        await cmdb_asset_crud.get(db, raw_asset_id) if isinstance(raw_asset_id, int) else None
    )
    if asset is not None and asset.credential_type == "dynamic":
        return "dynamic"
    return "static"


async def _dynamic_credential_missing(
    db: AsyncSession, existing: HitlProposal, password: SecretStr | None
) -> bool:
    """动态凭据资产却没带本次登录密码；在占队列名额、提交审批之前检查。"""
    if password:
        return False
    return await _credential_kind(db, existing) == "dynamic"


def _execution_view(
    proposal_id: int, persisted: HitlExecutionRequest | None
) -> tuple[str | None, str | None]:
    """内存中的任务优先；没有任务时用库里还没结束的执行请求。"""
    ticket = hitl_execution_queue.active(proposal_id)
    if ticket is not None:
        return ticket.state, ticket.request_id
    if persisted is None:
        return None, None
    return persisted.status, persisted.request_id


async def _to_response(db: AsyncSession, proposal: HitlProposal) -> HitlProposalResponse:
    """将 ORM 提案转为审批人 DTO，并附带执行摘要、资产凭据类型与后台执行状态。"""
    payload = proposal.action_payload if isinstance(proposal.action_payload, dict) else {}

    raw_result_excerpt = payload.get("last_result_excerpt")
    result_excerpt = raw_result_excerpt if isinstance(raw_result_excerpt, str) else None

    asset_credential_type: str | None = None
    raw_asset_id = payload.get("asset_id")
    if isinstance(raw_asset_id, int) and not isinstance(raw_asset_id, bool):
        asset = await cmdb_asset_crud.get(db, raw_asset_id)
        if asset is not None:
            asset_credential_type = asset.credential_type

    persisted = await execution_request_crud.get_open_request(db, proposal.id)
    execution_state, request_id = _execution_view(proposal.id, persisted)
    base = HitlProposalResponse.model_validate(proposal)
    return base.model_copy(
        update={
            "result_excerpt": result_excerpt,
            "asset_credential_type": asset_credential_type,
            "execution_state": execution_state,
            "execution_request_id": request_id,
        },
    )


def _decision_http_error(exc: BaseException) -> HTTPException | None:
    """审批阶段的异常映射成 HTTP 错误；None 表示原样抛出。"""
    if isinstance(exc, InvalidHitlTransitionError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, HitlProposalRejectedError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, ValueError):
        message = str(exc)
        if "not found" in message.lower() or "不存在" in message:
            return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")
    return None


@router.get(
    "/proposals",
    response_model=ResponseEnvelope[list[HitlProposalResponse]],
)
async def list_proposals(
    session_id: int = Query(..., gt=0, description="Agent 会话 ID"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        min_length=1,
        max_length=20,
        description="可选状态过滤，例如 PENDING",
    ),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[list[HitlProposalResponse]]:
    """按会话列出 HITL 提案（含完整 action_payload）。"""
    proposals = await hitl_proposal_crud.list_for_session(
        db,
        session_id,
        status=status_filter,
    )
    responses: list[HitlProposalResponse] = []
    for item in proposals:
        responses.append(await _to_response(db, item))
    return success_response(responses)


@router.get(
    "/proposals/{proposal_id}",
    response_model=ResponseEnvelope[HitlProposalResponse],
)
async def get_proposal(
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseEnvelope[HitlProposalResponse]:
    """获取单个 HITL 提案（含申请人、审批方式与当时快照）；不存在时返回 404。

    审批人或审计员可查：审计员从审计项里的 hitl_proposal:<id> 定位到提案，不必持有
    审批权限；会话归档后提案照样可查。先判权限再查提案，避免借 404 枚举提案 ID。
    """
    if not (
        await user_crud.has_permission_or_superuser(db, current_user, "agent:hitl_approve")
        or await user_crud.has_permission_or_superuser(db, current_user, "audit:read")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权限查看提案（需要权限：agent:hitl_approve 或 audit:read）",
        )
    proposal = await hitl_proposal_crud.get(db, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")
    return success_response(await _to_response(db, proposal))


@router.post(
    "/proposals/{proposal_id}/decide",
    response_model=ResponseEnvelope[HitlProposalResponse],
)
async def decide_hitl_proposal(
    proposal_id: int,
    body: HitlDecideRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[HitlProposalResponse]:
    """批准或拒绝提案。批准只提交审批并排进后台执行，返回 202；拒绝返回 200。

    **审批与执行是两件事**：设备命令可能要跑几十秒，同步等执行完会超过浏览器的等待
    时间，把「其实已批准、正在执行」误报成失败。所以批准后立即返回 202 和
    execution_state，执行结果经 WS 事件推送，前端也按提案 ID 查询兜底。

    队列名额在提交审批**之前**占好：排不进就 503，审批没有生效、提案仍待审批——
    不会出现「报错了但其实已经批准」的歧义。
    """
    actor_ip = get_client_ip(request)
    existing = await hitl_proposal_crud.get(db, proposal_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")

    if body.approve and await _dynamic_credential_missing(
        db, existing, body.dynamic_credential_password
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="该资产使用动态凭据，批准时必须提供本次登录密码",
        )

    credential_kind = await _credential_kind(db, existing) if body.approve else "none"
    if body.approve and hitl_execution_queue.active(proposal_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该提案已在执行队列中，本次批准未提交",
        )
    ticket = hitl_execution_queue.reserve(proposal_id) if body.approve else None
    if body.approve and ticket is None:
        raise _queue_full_error("执行队列已满，本次批准未提交，请稍后再批准")

    publisher = BufferedWsHitlEventPublisher()
    try:
        await decide_proposal(
            db,
            proposal_id=proposal_id,
            approve=body.approve,
            reviewed_by_user_id=current_user.id,
            publisher=publisher,
            actor_ip=actor_ip,
        )
        if ticket is not None:
            await execution_request_crud.create_open_request(
                db,
                proposal_id=proposal_id,
                request_id=ticket.request_id,
                actor_user_id=current_user.id,
                credential_kind=credential_kind,
            )
        # 响应在提交前组装：此刻就是批准后、开始执行前的样子（APPROVED + queued）
        decided = await hitl_proposal_crud.get(db, proposal_id)
        if decided is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")
        result = await _to_response(db, decided)
        await db.commit()
    except BaseException as exc:
        if ticket is not None:
            hitl_execution_queue.release(ticket)
        if isinstance(exc, IntegrityError):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该提案已有未完成的执行请求，本次批准未提交",
            ) from exc
        mapped = _decision_http_error(exc)
        if mapped is None:
            raise
        raise mapped from exc

    if ticket is not None:
        # 提交与启动之间没有 await：客户端这时断开也不会留下占着名额却没启动的任务
        _start_execution(
            ticket,
            db=db,
            actor_user_id=current_user.id,
            dynamic_password=_reveal_password(body.dynamic_credential_password),
            actor_ip=actor_ip,
            credential_kind=credential_kind,
        )
    # 提交后再广播 HITL 事件，避免前端收到事件后读不到未提交的行。
    await publisher.flush()
    if ticket is None:
        return success_response(result, message="审批完成")
    response.status_code = status.HTTP_202_ACCEPTED
    return success_response(result, message="已批准，正在后台执行")


@router.post(
    "/proposals/{proposal_id}/retry",
    response_model=ResponseEnvelope[HitlProposalResponse],
)
async def retry_hitl_proposal(
    proposal_id: int,
    body: HitlRetryRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[HitlProposalResponse]:
    """把一个已批准但未执行成功的提案重新排进后台执行，返回 202。

    该提案已在排队或执行时直接返回已有的执行请求（同一个 execution_request_id），
    重复点击不会重复入队、不会重复连接设备。

    **真正入队的重试先写一条审计**：重试可能在预检阶段就失败（命令不存在、动态凭据
    缺失、资产被删），此时提案保持 APPROVED、执行阶段的审计一条都不会写。如果这里也
    不写，管理员就能对同一条提案反复尝试直到某次成功，而日志里只留下最后成功的那条。
    """
    actor_ip = get_client_ip(request)
    existing = await hitl_proposal_crud.get(db, proposal_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")

    if hitl_execution_queue.active(proposal_id) is not None:
        response.status_code = status.HTTP_202_ACCEPTED
        return success_response(await _to_response(db, existing), message="该提案已在执行队列中")

    if existing.status != "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="只有已批准但未执行成功的提案可以重试",
        )

    if await _dynamic_credential_missing(db, existing, body.dynamic_credential_password):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="该资产使用动态凭据，重试时必须提供本次登录密码",
        )

    credential_kind = await _credential_kind(db, existing)
    open_request = await execution_request_crud.get_open_request(db, proposal_id)
    if open_request is not None and open_request.status == "awaiting_credential":
        await execution_request_crud.resume_awaiting_request(
            db, open_request, actor_user_id=current_user.id
        )
    ticket = (
        hitl_execution_queue.adopt(proposal_id, open_request.request_id)
        if open_request is not None
        else hitl_execution_queue.reserve(proposal_id)
    )
    if ticket is None:
        if hitl_execution_queue.active(proposal_id) is not None:
            response.status_code = status.HTTP_202_ACCEPTED
            return success_response(await _to_response(db, existing), message="该提案已在执行队列中")
        raise _queue_full_error("执行队列已满，本次重试未提交，请稍后再试")

    try:
        if open_request is None:
            await execution_request_crud.create_open_request(
                db,
                proposal_id=proposal_id,
                request_id=ticket.request_id,
                actor_user_id=current_user.id,
                credential_kind=credential_kind,
            )
        await log_audit(
            db,
            current_user.id,
            "hitl_retry_requested",
            target=f"hitl_proposal:{proposal_id}",
            detail=f"动作类型：{existing.action_type}",
            ip=actor_ip,
        )
        result = await _to_response(db, existing)
        await db.commit()
    except BaseException as exc:
        if ticket.task is None:
            hitl_execution_queue.release(ticket)
        if isinstance(exc, IntegrityError):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该提案已有未完成的执行请求，本次重试未提交",
            ) from exc
        raise

    _start_execution(
        ticket,
        db=db,
        actor_user_id=current_user.id,
        dynamic_password=_reveal_password(body.dynamic_credential_password),
        actor_ip=actor_ip,
        credential_kind=credential_kind,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return success_response(result, message="已提交重试，正在后台执行")


@router.post(
    "/proposals/{proposal_id}/resolve-unknown",
    response_model=ResponseEnvelope[HitlProposalResponse],
)
async def resolve_unknown_proposal(
    proposal_id: int,
    body: HitlUnknownResolutionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[HitlProposalResponse]:
    """人工处置 UNKNOWN 提案：确认已执行或允许重试。"""
    actor_ip = get_client_ip(request)
    existing = await hitl_proposal_crud.get(db, proposal_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")

    publisher = BufferedWsHitlEventPublisher()
    try:
        await hitl_proposal_crud.resolve_unknown(
            db,
            proposal_id,
            resolution=body.resolution,
            resolved_by_user_id=current_user.id,
        )
    except InvalidHitlTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower() or "不存在" in message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="HITL 提案不存在",
            ) from exc
        raise

    await log_audit(
        db,
        user_id=current_user.id,
        action="hitl_unknown_confirmed"
        if body.resolution == "confirm_executed"
        else "hitl_unknown_retry_authorized",
        target=f"hitl_proposal:{proposal_id}",
        detail=body.resolution,
        ip=actor_ip,
    )
    await db.commit()

    refreshed = await hitl_proposal_crud.get(db, proposal_id)
    if refreshed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")

    payload_dict = refreshed.action_payload if isinstance(refreshed.action_payload, dict) else {}
    await publisher.publish(
        session_id=refreshed.session_id,
        event_type="hitl_resolved",
        payload={
            "proposal_id": refreshed.id,
            "action_type": refreshed.action_type,
            "status": refreshed.status,
            "status_reason": refreshed.status_reason,
            "reason": str(payload_dict.get("proposal_reason", "")),
            "asset_id": payload_dict.get("asset_id"),
            "resolved_at": refreshed.resolved_at,
        },
    )
    await publisher.flush()
    return success_response(await _to_response(db, refreshed))
