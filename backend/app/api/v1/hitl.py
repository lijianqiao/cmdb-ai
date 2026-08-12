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
4. decide/resume 注入 WsHitlEventPublisher，把 hitl_resolved / hitl_execution_failed 推到会话 WS。
5. 异常映射：缺失 404、非法迁移/恢复失败 409、提案校验拒绝 400；事务内审计后统一 commit。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.hitl import (
    HitlProposalRejectedError,
    HitlResumeError,
    decide_proposal,
    resume_proposal,
)
from app.agent.ws_hub import WsHitlEventPublisher
from app.core.database import get_db
from app.core.deps import require_permission
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import InvalidHitlTransitionError, hitl_proposal_crud
from app.models.user import User
from app.schemas.common import ResponseEnvelope, success_response
from app.schemas.hitl import HitlDecideRequest, HitlProposalResponse

router = APIRouter()


def _to_response(proposal: object) -> HitlProposalResponse:
    """将 ORM 提案转为审批人 DTO。"""
    return HitlProposalResponse.model_validate(proposal)


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
    return success_response([_to_response(item) for item in proposals])


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
    return success_response(_to_response(proposal))


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

    if body.approve and existing.action_type == "device_query":
        raw_asset_id = existing.action_payload.get("asset_id")
        asset = await cmdb_asset_crud.get(db, raw_asset_id) if isinstance(raw_asset_id, int) else None
        if asset is not None and asset.credential_type == "dynamic" and not body.dynamic_credential_password:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="该资产使用动态凭据，批准时必须提供本次登录密码",
            )

    publisher = WsHitlEventPublisher()
    try:
        await decide_proposal(
            db,
            proposal_id=proposal_id,
            approve=body.approve,
            reviewed_by_user_id=current_user.id,
            publisher=publisher,
        )
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

    # 编排层已写入审计记录；端点只负责一次 commit，避免半提交。
    await db.commit()

    proposal = await hitl_proposal_crud.get(db, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HITL 提案不存在")
    return success_response(_to_response(proposal), message="审批完成")
