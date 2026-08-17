"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl.py
@DateTime: 2026-08-12 11:36
@Docs: HITL 提案查询与人工审批 HTTP API。

实现流程：
1. 全部端点以 agent:hitl_approve 门控，审批人可看到完整 action_payload。
2. 列表与详情直接复用 hitl_proposal_crud 的会话查询与按 ID 读取。
3. 审批接口调用 decide_proposal；仅在批准时再调用 resume_proposal（拒绝不恢复执行）。
4. 人工批准或重试成功的 device_query 通过独立短会话生成并持久化总结；总结失败不改变设备执行结果。
5. 新增 POST /proposals/{id}/retry，对 APPROVED 提案再次调用幂等的 resume_proposal。
6. decide/resume/retry 注入 BufferedWsHitlEventPublisher，在 db.commit() 之后 flush，
   避免前端收到 hitl_* 事件后立刻 GET 提案却读不到未提交的行。
7. 已提交的 hitl_resolved 先广播，再追加 assistant_delta；广播异常只记安全 warning。
8. 异常映射：缺失 404、非法迁移/恢复失败 409、提案校验拒绝 400；事务内审计后统一 commit。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.device_result_summary import SummaryDelivery, deliver_device_query_summary
from app.agent.hitl import (
    HitlProposalRejectedError,
    HitlResumeError,
    decide_proposal,
    resume_proposal,
)
from app.agent.ws_hub import BufferedWsHitlEventPublisher, hub
from app.core.database import get_db
from app.core.deps import get_client_ip, require_permission
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import InvalidHitlTransitionError, hitl_proposal_crud
from app.models.user import User
from app.schemas.agent_ws import AgentWsServerMessage
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


def _reveal_password(secret: SecretStr | None) -> str | None:
    """在调用执行链之前解开一次性口令。

    明文只在这一个位置产生，从这里往下（resume_proposal → 执行器 → Netmiko）
    是短暂的内存传递，不落库、不进日志、不进 ExecutionResult.detail。
    集中成一个函数是为了让「明文从哪来」在代码里只有一个可搜的答案。
    """
    return secret.get_secret_value() if secret is not None else None


async def _deliver_executed_query_summary(
    db: AsyncSession,
    *,
    proposal_id: int,
) -> SummaryDelivery | None:
    """仅为已成功执行的 device_query 交付总结；失败不影响设备成功状态。"""
    try:
        proposal = await hitl_proposal_crud.get(db, proposal_id)
        if (
            proposal is None
            or proposal.action_type != "device_query"
            or proposal.status != "EXECUTED"
        ):
            return None

        engine = db.bind
        if engine is None:
            raise RuntimeError("数据库会话未绑定 AsyncEngine")
        session_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
        )
        return await deliver_device_query_summary(
            session_factory=session_factory,
            proposal_id=proposal_id,
        )
    except Exception as exc:
        logger.warning(
            "设备查询总结交付失败 proposal_id=%s exc_type=%s",
            proposal_id,
            type(exc).__name__,
        )
        return None


async def _broadcast_summary_delivery(delivery: SummaryDelivery | None) -> None:
    """只广播本次新建的总结消息；广播失败不改变 HTTP 成功语义。"""
    if delivery is None or not delivery.created_message:
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


async def _to_response(db: AsyncSession, proposal: object) -> HitlProposalResponse:
    """将 ORM 提案转为审批人 DTO，并附带执行摘要与资产凭据类型。"""
    payload = getattr(proposal, "action_payload", None)
    payload_dict = payload if isinstance(payload, dict) else {}

    raw_result_excerpt = payload_dict.get("last_result_excerpt")
    result_excerpt = raw_result_excerpt if isinstance(raw_result_excerpt, str) else None

    asset_credential_type: str | None = None
    raw_asset_id = payload_dict.get("asset_id")
    if isinstance(raw_asset_id, int) and not isinstance(raw_asset_id, bool):
        asset = await cmdb_asset_crud.get(db, raw_asset_id)
        if asset is not None:
            asset_credential_type = asset.credential_type

    base = HitlProposalResponse.model_validate(proposal)
    return base.model_copy(
        update={
            "result_excerpt": result_excerpt,
            "asset_credential_type": asset_credential_type,
        },
    )


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
    _: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[HitlProposalResponse]:
    """获取单个 HITL 提案；不存在时返回 404。"""
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[HitlProposalResponse]:
    """批准或拒绝提案；批准后触发 resume，拒绝则只更新状态。

    **审批与执行是两件事**：审批一旦提交就不可回滚（执行器必须能观测到已提交的
    EXECUTING）。因此执行阶段失败时**不返回 409**——那会让前端显示「批准失败」，
    而提案其实已经 APPROVED，用户再点批准只会因状态已变再拿一个 409，卡死循环。
    这里改为返回 200 + `execution_error`，让前端如实显示「已批准，执行失败」
    并把主操作切换成「重试执行」。
    """
    actor_ip = get_client_ip(request)
    existing = await hitl_proposal_crud.get(db, proposal_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")

    if body.approve and existing.action_type in ("device_query", "device_control"):
        raw_asset_id = existing.action_payload.get("asset_id")
        asset = await cmdb_asset_crud.get(db, raw_asset_id) if isinstance(raw_asset_id, int) else None
        if asset is not None and asset.credential_type == "dynamic" and not body.dynamic_credential_password:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="该资产使用动态凭据，批准时必须提供本次登录密码",
            )

    publisher = BufferedWsHitlEventPublisher()
    delivery: SummaryDelivery | None = None
    execution_error: str | None = None

    # 阶段一：审批。这一段失败什么都没提交，可以如实返回 4xx。
    try:
        await decide_proposal(
            db,
            proposal_id=proposal_id,
            approve=body.approve,
            reviewed_by_user_id=current_user.id,
            publisher=publisher,
            actor_ip=actor_ip,
        )
        await db.commit()
    except InvalidHitlTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except HitlProposalRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
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

    # 阶段二：执行。此时 APPROVED 已落库不可回滚，失败只记录不抛 HTTP 错误。
    if body.approve:
        try:
            await resume_proposal(
                db,
                proposal_id=proposal_id,
                actor_user_id=current_user.id,
                publisher=publisher,
                dynamic_password=_reveal_password(body.dynamic_credential_password),
                actor_ip=actor_ip,
            )
            delivery = await _deliver_executed_query_summary(
                db,
                proposal_id=proposal_id,
            )
        except HitlResumeError as exc:
            execution_error = str(exc)
            logger.info(
                "HITL 审批成功但执行未启动 proposal_id=%s reason=%s",
                proposal_id,
                execution_error,
            )

    # 编排层已写入审计记录；审批已提交，执行服务在独立短会话内完成。
    await db.commit()
    # 提交后再广播 HITL 事件，避免前端收到事件后读不到未提交的行。
    await publisher.flush()
    await _broadcast_summary_delivery(delivery)

    proposal = await hitl_proposal_crud.get(db, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")
    response = await _to_response(db, proposal)
    if execution_error is not None:
        response = response.model_copy(update={"execution_error": execution_error})
        return success_response(response, message="已批准，但执行未成功，可重试")
    return success_response(response, message="审批完成")


@router.post(
    "/proposals/{proposal_id}/retry",
    response_model=ResponseEnvelope[HitlProposalResponse],
)
async def retry_hitl_proposal(
    proposal_id: int,
    body: HitlRetryRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[HitlProposalResponse]:
    """重试执行一个已批准但执行失败的提案；EXECUTED 时幂等返回。

    **无论成功与否都先写一条审计**：重试可能在预检阶段就失败（命令不存在、
    动态凭据缺失、资产被删），此时提案保持 APPROVED、执行阶段的审计一条都不会写。
    如果这里也不写，管理员就能对同一条提案反复尝试直到某次成功，而日志里只留下
    最后成功的那条——中间试了多少次、什么时候、从哪试的全部不可见。
    """
    actor_ip = get_client_ip(request)
    existing = await hitl_proposal_crud.get(db, proposal_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")

    if existing.status != "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="只有已批准但未执行成功的提案可以重试",
        )

    if existing.action_type in ("device_query", "device_control"):
        raw_asset_id = existing.action_payload.get("asset_id")
        asset = await cmdb_asset_crud.get(db, raw_asset_id) if isinstance(raw_asset_id, int) else None
        if asset is not None and asset.credential_type == "dynamic" and not body.dynamic_credential_password:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="该资产使用动态凭据，重试时必须提供本次登录密码",
            )

    # 「有人发起了重试」是一个已经发生的事实，与执行结果无关，所以**立即单独提交**，
    # 不能挂在后面那次 commit 上：resume_proposal 会用 db.bind 派生独立短会话，
    # 那些会话的生命周期不由这里控制，把审计的持久性押在它们身上是脆的
    # （测试环境的 SQLite 共用连接时就会被内层 rollback 丢掉）。
    await log_audit(
        db,
        current_user.id,
        "hitl_retry_requested",
        target=f"hitl_proposal:{proposal_id}",
        detail=f"动作类型：{existing.action_type}",
        ip=actor_ip,
    )
    await db.commit()

    publisher = BufferedWsHitlEventPublisher()
    delivery: SummaryDelivery | None = None
    try:
        await resume_proposal(
            db,
            proposal_id=proposal_id,
            actor_user_id=current_user.id,
            publisher=publisher,
            dynamic_password=_reveal_password(body.dynamic_credential_password),
            actor_ip=actor_ip,
        )
        delivery = await _deliver_executed_query_summary(
            db,
            proposal_id=proposal_id,
        )
    except HitlResumeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    await db.commit()
    await publisher.flush()
    await _broadcast_summary_delivery(delivery)

    proposal = await hitl_proposal_crud.get(db, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")
    return success_response(await _to_response(db, proposal), message="重试完成")


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
