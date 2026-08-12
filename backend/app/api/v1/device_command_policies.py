"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: device_command_policies.py
@DateTime: 2026-08-12 22:15
@Docs: 设备命令策略管理 API：CRUD、回收站与审计
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_client_ip, require_permission
from app.crud.device_command_policy import (
    DuplicateDeviceCommandPolicyError,
    device_command_policy_crud,
)
from app.models.device_command_policy import DeviceCommandPolicy
from app.models.user import User
from app.schemas.common import PaginatedData, ResponseEnvelope, paginated_response, success_response
from app.schemas.device_command_policy import (
    DeviceCommandPolicyCreate,
    DeviceCommandPolicyResponse,
    DeviceCommandPolicyUpdate,
)
from app.utils.audit import log_audit

router = APIRouter()


def _to_response(policy: DeviceCommandPolicy) -> DeviceCommandPolicyResponse:
    return DeviceCommandPolicyResponse.model_validate(policy)


def _policy_target_label(policy: DeviceCommandPolicy) -> str:
    if policy.scope == "asset_type":
        return f"{policy.asset_type}/{policy.command_name}"
    return f"asset#{policy.asset_id}/{policy.command_name}"


@router.get("/policies", response_model=ResponseEnvelope[PaginatedData[DeviceCommandPolicyResponse]])
async def list_policies(
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("device_command_policy:read")),
) -> ResponseEnvelope[PaginatedData[DeviceCommandPolicyResponse]]:
    """Return a page of active device command policies."""
    policies, total = await device_command_policy_crud.get_multi_filtered(
        db,
        skip=(page - 1) * page_size,
        limit=page_size,
    )
    items = [_to_response(policy) for policy in policies]
    return paginated_response(items, total, page, page_size)


@router.post(
    "/policies",
    response_model=ResponseEnvelope[DeviceCommandPolicyResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_policy(
    policy_in: DeviceCommandPolicyCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("device_command_policy:manage")),
) -> ResponseEnvelope[DeviceCommandPolicyResponse]:
    """Create a device command whitelist/blacklist policy."""
    persist_data = policy_in.model_dump()
    persist_data["created_by_user_id"] = current_user.id
    try:
        policy = await device_command_policy_crud.create(db, persist_data)
    except DuplicateDeviceCommandPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    await log_audit(
        db,
        user_id=current_user.id,
        action="create_device_command_policy",
        target=f"device_command_policy:{policy.id}",
        detail=f"创建策略: {_policy_target_label(policy)} → {policy.decision}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(_to_response(policy), message="创建成功", code=status.HTTP_201_CREATED)


@router.get("/policies/deleted", response_model=ResponseEnvelope[PaginatedData[DeviceCommandPolicyResponse]])
async def list_deleted_policies(
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("device_command_policy:manage")),
) -> ResponseEnvelope[PaginatedData[DeviceCommandPolicyResponse]]:
    """List soft-deleted policies in the recycle bin."""
    policies, total = await device_command_policy_crud.get_deleted_multi(
        db, skip=(page - 1) * page_size, limit=page_size
    )
    items = [_to_response(policy) for policy in policies]
    return paginated_response(items, total, page, page_size)


@router.get("/policies/{policy_id}", response_model=ResponseEnvelope[DeviceCommandPolicyResponse])
async def get_policy(
    policy_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("device_command_policy:read")),
) -> ResponseEnvelope[DeviceCommandPolicyResponse]:
    """Return one active policy."""
    policy = await device_command_policy_crud.get(db, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return success_response(_to_response(policy))


@router.patch("/policies/{policy_id}", response_model=ResponseEnvelope[DeviceCommandPolicyResponse])
async def update_policy(
    policy_in: DeviceCommandPolicyUpdate,
    request: Request,
    policy_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("device_command_policy:manage")),
) -> ResponseEnvelope[DeviceCommandPolicyResponse]:
    """Partially update a device command policy (decision/note only)."""
    existing = await device_command_policy_crud.get(db, policy_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="策略不存在")

    try:
        updated = await device_command_policy_crud.update(
            db, policy_id, policy_in.model_dump(exclude_unset=True)
        )
    except DuplicateDeviceCommandPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="策略不存在")

    await log_audit(
        db,
        user_id=current_user.id,
        action="update_device_command_policy",
        target=f"device_command_policy:{policy_id}",
        detail=f"更新策略: {_policy_target_label(updated)} → {updated.decision}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(_to_response(updated), message="更新成功")


@router.delete("/policies/{policy_id}", response_model=ResponseEnvelope[None])
async def delete_policy(
    request: Request,
    policy_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("device_command_policy:manage")),
) -> ResponseEnvelope[None]:
    """Soft-delete a device command policy."""
    policy = await device_command_policy_crud.get(db, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="策略不存在")

    if not await device_command_policy_crud.soft_delete(db, policy_id):
        raise HTTPException(status_code=404, detail="策略不存在")

    await log_audit(
        db,
        user_id=current_user.id,
        action="delete_device_command_policy",
        target=f"device_command_policy:{policy_id}",
        detail=f"删除策略: {_policy_target_label(policy)}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(None, message="删除成功")


@router.post("/policies/{policy_id}/restore", response_model=ResponseEnvelope[DeviceCommandPolicyResponse])
async def restore_policy(
    request: Request,
    policy_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("device_command_policy:manage")),
) -> ResponseEnvelope[DeviceCommandPolicyResponse]:
    """Restore a soft-deleted policy from the recycle bin."""
    restored = await device_command_policy_crud.restore(db, policy_id)
    if restored is None:
        raise HTTPException(status_code=404, detail="回收站中不存在该策略")

    await log_audit(
        db,
        user_id=current_user.id,
        action="restore_device_command_policy",
        target=f"device_command_policy:{policy_id}",
        detail=f"恢复策略: {_policy_target_label(restored)}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(_to_response(restored), message="恢复成功")


@router.delete("/policies/{policy_id}/purge", response_model=ResponseEnvelope[None])
async def purge_policy(
    request: Request,
    policy_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("device_command_policy:manage")),
) -> ResponseEnvelope[None]:
    """Permanently delete a soft-deleted policy."""
    if not await device_command_policy_crud.hard_delete(db, policy_id):
        raise HTTPException(status_code=404, detail="回收站中不存在该策略")

    await log_audit(
        db,
        user_id=current_user.id,
        action="purge_device_command_policy",
        target=f"device_command_policy:{policy_id}",
        detail="永久删除策略",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(None, message="已永久删除")
