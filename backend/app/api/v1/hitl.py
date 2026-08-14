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
4. 新增 POST /proposals/{id}/retry，对 APPROVED 提案再次调用幂等的 resume_proposal。
5. decide/resume/retry 注入 BufferedWsHitlEventPublisher，在 db.commit() 之后 flush，
   避免前端收到 hitl_* 事件后立刻 GET 提案却读不到未提交的行。
6. 异常映射：缺失 404、非法迁移/恢复失败 409、提案校验拒绝 400；事务内审计后统一 commit。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.hitl import (
    HitlProposalRejectedError,
    HitlResumeError,
    decide_proposal,
    resume_proposal,
)
from app.agent.ws_hub import BufferedWsHitlEventPublisher
from app.core.database import get_db
from app.core.deps import require_permission
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import InvalidHitlTransitionError, hitl_proposal_crud
from app.models.user import User
from app.schemas.common import ResponseEnvelope, success_response
from app.schemas.hitl import (
    HitlDecideRequest,
    HitlProposalResponse,
    HitlRetryRequest,
    HitlUnknownResolutionRequest,
)
from app.utils.audit import log_audit

router = APIRouter()


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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[HitlProposalResponse]:
    """批准或拒绝提案；批准后触发 resume，拒绝则只更新状态。"""
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
    try:
        await decide_proposal(
            db,
            proposal_id=proposal_id,
            approve=body.approve,
            reviewed_by_user_id=current_user.id,
            publisher=publisher,
        )
        await db.commit()
        if body.approve:
            await resume_proposal(
                db,
                proposal_id=proposal_id,
                actor_user_id=current_user.id,
                publisher=publisher,
                dynamic_password=body.dynamic_credential_password,
            )
    except InvalidHitlTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except HitlResumeError as exc:
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

    # 编排层已写入审计记录；审批已提交，执行服务在独立短会话内完成。
    await db.commit()
    # 提交后再广播 HITL 事件，避免前端收到事件后读不到未提交的行。
    await publisher.flush()

    proposal = await hitl_proposal_crud.get(db, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")
    return success_response(await _to_response(db, proposal), message="审批完成")


@router.post(
    "/proposals/{proposal_id}/retry",
    response_model=ResponseEnvelope[HitlProposalResponse],
)
async def retry_hitl_proposal(
    proposal_id: int,
    body: HitlRetryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[HitlProposalResponse]:
    """重试执行一个已批准但执行失败的提案；EXECUTED 时幂等返回。"""
    existing = await hitl_proposal_crud.get(db, proposal_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")

    if existing.status != "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="只有已批准但未执行成功的提案可以重试",
        )

    if existing.status == "APPROVED" and existing.action_type in ("device_query", "device_control"):
        raw_asset_id = existing.action_payload.get("asset_id")
        asset = await cmdb_asset_crud.get(db, raw_asset_id) if isinstance(raw_asset_id, int) else None
        if asset is not None and asset.credential_type == "dynamic" and not body.dynamic_credential_password:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="该资产使用动态凭据，重试时必须提供本次登录密码",
            )

    publisher = BufferedWsHitlEventPublisher()
    try:
        await resume_proposal(
            db,
            proposal_id=proposal_id,
            actor_user_id=current_user.id,
            publisher=publisher,
            dynamic_password=body.dynamic_credential_password,
        )
    except HitlResumeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    await db.commit()
    await publisher.flush()

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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("agent:hitl_approve")),
) -> ResponseEnvelope[HitlProposalResponse]:
    """人工处置 UNKNOWN 提案：确认已执行或允许重试。"""
    existing = await hitl_proposal_crud.get(db, proposal_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")

    publisher = BufferedWsHitlEventPublisher()
    try:
        proposal = await hitl_proposal_crud.resolve_unknown(
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
    )
    await db.commit()

    proposal = await hitl_proposal_crud.get(db, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")

    payload_dict = proposal.action_payload if isinstance(proposal.action_payload, dict) else {}
    await publisher.publish(
        session_id=proposal.session_id,
        event_type="hitl_resolved",
        payload={
            "proposal_id": proposal.id,
            "action_type": proposal.action_type,
            "status": proposal.status,
            "status_reason": proposal.status_reason,
            "reason": str(payload_dict.get("proposal_reason", "")),
            "asset_id": payload_dict.get("asset_id"),
            "resolved_at": proposal.resolved_at,
        },
    )
    await publisher.flush()
    return success_response(await _to_response(db, proposal))
