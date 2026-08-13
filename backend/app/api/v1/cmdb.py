"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: cmdb.py
@DateTime: 2026-08-12 16:20
@Docs: CMDB 资产管理 API：CRUD、回收站、凭据加密持久化与审计
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cmdb_credential import (
    CmdbCredentialDecryptError,
    CmdbCredentialKeyMissingError,
    decrypt_credential_password,
    encrypt_credential_password,
)
from app.core.database import get_db
from app.core.deps import get_client_ip, require_permission
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud
from app.models.cmdb_asset import CmdbAsset
from app.models.user import User
from app.schemas.cmdb import (
    CmdbAssetCreate,
    CmdbAssetDependencyCreate,
    CmdbAssetDependencyListResponse,
    CmdbAssetDependencyResponse,
    CmdbAssetResponse,
    CmdbAssetUpdate,
    CmdbCredentialRevealResponse,
)
from app.schemas.common import PaginatedData, ResponseEnvelope, paginated_response, success_response
from app.utils.audit import log_audit

router = APIRouter()

_CREDENTIAL_KEY_MISSING_SAVE_DETAIL = (
    "未配置 CMDB_CREDENTIAL_KEY，无法保存静态密码，请联系管理员配置"
)
_CREDENTIAL_KEY_MISSING_REVEAL_DETAIL = (
    "未配置 CMDB_CREDENTIAL_KEY，无法查看静态密码。请在环境变量中配置后再试。"
)
_CREDENTIAL_DECRYPT_REVEAL_DETAIL = (
    "静态密码解密失败，可能是密钥已轮换。请检查 CMDB_CREDENTIAL_KEY。"
)


def _to_response(asset: CmdbAsset) -> CmdbAssetResponse:
    return CmdbAssetResponse(
        id=asset.id,
        asset_type=asset.asset_type,
        vendor=asset.vendor,
        hostname=asset.hostname,
        ip_address=asset.ip_address,
        location=asset.location,
        owner_user_id=asset.owner_user_id,
        business_system=asset.business_system,
        subnet_cidr=asset.subnet_cidr,
        notes=asset.notes,
        credential_type=asset.credential_type,
        credential_username=asset.credential_username,
        credential_password_set=bool(asset.credential_password_encrypted),
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _prepare_persist_data(
    payload: dict[str, object],
    *,
    existing: CmdbAsset | None,
) -> dict[str, object]:
    """把请求里的凭据明文换成待持久化字段；非凭据字段原样透传。

    没有出现在 payload 里的字段完全不放进返回值，交给 CRUDBase 的
    "只更新出现过的键" 语义去保留原值——这正是"编辑资产时不碰密码就不改密码"
    这个安全约束的落地方式。
    """
    data = dict(payload)
    if "credential_type" not in data:
        return data

    credential_type = data["credential_type"]
    plain_password = data.pop("credential_password", None)

    if credential_type == "static":
        if plain_password is not None:
            if not isinstance(plain_password, str):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="静态凭据密码格式无效",
                )
            try:
                data["credential_password_encrypted"] = encrypt_credential_password(plain_password)
            except CmdbCredentialKeyMissingError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="未配置 CMDB_CREDENTIAL_KEY，无法保存静态密码，请联系管理员配置",
                ) from exc
        elif existing is None or existing.credential_type != "static" or not existing.credential_password_encrypted:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="切换为静态凭据时必须提供密码",
            )
        # 否则：静态类型不变、没传新密码 → 不放 credential_password_encrypted 进 data，保留原密文
    elif credential_type == "none":
        # 不依赖调用方老实传空账号名——服务端自己保证 none 类型下不留残留账号，
        # 即使有人绕过前端直接调 API 只传 credential_type=none 也不会留下不一致数据。
        data["credential_username"] = ""
        data["credential_password_encrypted"] = None
    else:  # dynamic
        data["credential_password_encrypted"] = None

    return data


def _credential_changed(existing: CmdbAsset, persist_data: dict[str, object]) -> bool:
    """判断本次更新是否实际改动了凭据字段（用于审计详情，不记录密码明文）。"""
    if "credential_type" in persist_data and persist_data["credential_type"] != existing.credential_type:
        return True
    if (
        "credential_username" in persist_data
        and persist_data["credential_username"] != existing.credential_username
    ):
        return True
    if (
        "credential_password_encrypted" in persist_data
        and persist_data["credential_password_encrypted"] != existing.credential_password_encrypted
    ):
        return True
    return False


@router.get("/assets", response_model=ResponseEnvelope[PaginatedData[CmdbAssetResponse]])
async def list_assets(
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=10, ge=1, le=100),
    search: str | None = Query(default=None, min_length=1, max_length=100),
    asset_type: str | None = Query(default=None, min_length=1, max_length=50),
    business_system: str | None = Query(default=None, min_length=1, max_length=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("cmdb:read")),
) -> ResponseEnvelope[PaginatedData[CmdbAssetResponse]]:
    """Return a filtered page of active CMDB assets."""
    assets, total = await cmdb_asset_crud.get_multi_filtered(
        db,
        search=search,
        asset_type=asset_type,
        business_system=business_system,
        skip=(page - 1) * page_size,
        limit=page_size,
    )
    items = [_to_response(asset) for asset in assets]
    return paginated_response(items, total, page, page_size)


@router.post("/assets", response_model=ResponseEnvelope[CmdbAssetResponse], status_code=status.HTTP_201_CREATED)
async def create_asset(
    asset_in: CmdbAssetCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[CmdbAssetResponse]:
    """Create a CMDB asset, optionally with an encrypted static credential."""
    persist_data = _prepare_persist_data(asset_in.model_dump(), existing=None)
    asset = await cmdb_asset_crud.create(db, persist_data)

    credential_changed = asset_in.credential_type != "none"
    await log_audit(
        db,
        user_id=current_user.id,
        action="create_cmdb_asset",
        target=f"cmdb_asset:{asset.id}",
        detail=f"创建资产: {asset.hostname}；凭据{'已设置' if credential_changed else '未设置'}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(_to_response(asset), message="创建成功", code=status.HTTP_201_CREATED)


@router.get("/assets/deleted", response_model=ResponseEnvelope[PaginatedData[CmdbAssetResponse]])
async def list_deleted_assets(
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=10, ge=1, le=100),
    search: str | None = Query(default=None, min_length=1, max_length=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[PaginatedData[CmdbAssetResponse]]:
    """List soft-deleted assets in the recycle bin."""
    assets, total = await cmdb_asset_crud.get_deleted_multi(
        db, search=search, skip=(page - 1) * page_size, limit=page_size
    )
    items = [_to_response(asset) for asset in assets]
    return paginated_response(items, total, page, page_size)


@router.get("/assets/{asset_id}", response_model=ResponseEnvelope[CmdbAssetResponse])
async def get_asset(
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("cmdb:read")),
) -> ResponseEnvelope[CmdbAssetResponse]:
    """Return one active asset."""
    asset = await cmdb_asset_crud.get(db, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    return success_response(_to_response(asset))


@router.get(
    "/assets/{asset_id}/credential",
    response_model=ResponseEnvelope[CmdbCredentialRevealResponse],
)
async def reveal_asset_credential(
    request: Request,
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:credential_read")),
) -> ResponseEnvelope[CmdbCredentialRevealResponse]:
    """按需解密并返回静态凭据明文，同时写入审计。"""
    asset = await cmdb_asset_crud.get(db, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")

    if asset.credential_type != "static" or not asset.credential_password_encrypted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="该资产没有可查看的静态密码",
        )

    try:
        password = decrypt_credential_password(asset.credential_password_encrypted)
    except CmdbCredentialKeyMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_CREDENTIAL_KEY_MISSING_REVEAL_DETAIL,
        ) from exc
    except CmdbCredentialDecryptError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_CREDENTIAL_DECRYPT_REVEAL_DETAIL,
        ) from exc

    await log_audit(
        db,
        user_id=current_user.id,
        action="view_cmdb_credential",
        target=f"cmdb_asset:{asset_id}",
        detail=f"查看静态凭据: {asset.hostname}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(CmdbCredentialRevealResponse(password=password))


@router.patch("/assets/{asset_id}", response_model=ResponseEnvelope[CmdbAssetResponse])
async def update_asset(
    asset_in: CmdbAssetUpdate,
    request: Request,
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[CmdbAssetResponse]:
    """Partially update a CMDB asset."""
    existing = await cmdb_asset_crud.get(db, asset_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="资产不存在")

    persist_data = _prepare_persist_data(
        asset_in.model_dump(exclude_unset=True), existing=existing
    )
    credential_touched = _credential_changed(existing, persist_data)
    updated = await cmdb_asset_crud.update(db, asset_id, persist_data)
    if updated is None:
        raise HTTPException(status_code=404, detail="资产不存在")

    await log_audit(
        db,
        user_id=current_user.id,
        action="update_cmdb_asset",
        target=f"cmdb_asset:{asset_id}",
        detail=f"更新资产: {updated.hostname}；凭据{'已变更' if credential_touched else '未变更'}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(_to_response(updated), message="更新成功")


@router.delete("/assets/{asset_id}", response_model=ResponseEnvelope[None])
async def delete_asset(
    request: Request,
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[None]:
    """Soft-delete a CMDB asset."""
    asset = await cmdb_asset_crud.get(db, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")

    if not await cmdb_asset_crud.soft_delete(db, asset_id):
        raise HTTPException(status_code=404, detail="资产不存在")

    await log_audit(
        db,
        user_id=current_user.id,
        action="delete_cmdb_asset",
        target=f"cmdb_asset:{asset_id}",
        detail=f"删除资产: {asset.hostname}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(None, message="删除成功")


@router.post("/assets/{asset_id}/restore", response_model=ResponseEnvelope[CmdbAssetResponse])
async def restore_asset(
    request: Request,
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[CmdbAssetResponse]:
    """Restore a soft-deleted asset from the recycle bin."""
    restored = await cmdb_asset_crud.restore(db, asset_id)
    if restored is None:
        raise HTTPException(status_code=404, detail="回收站中不存在该资产")

    await log_audit(
        db,
        user_id=current_user.id,
        action="restore_cmdb_asset",
        target=f"cmdb_asset:{asset_id}",
        detail=f"恢复资产: {restored.hostname}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(_to_response(restored), message="恢复成功")


@router.delete("/assets/{asset_id}/purge", response_model=ResponseEnvelope[None])
async def purge_asset(
    request: Request,
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[None]:
    """Permanently delete a soft-deleted asset."""
    if not await cmdb_asset_crud.hard_delete(db, asset_id):
        raise HTTPException(status_code=404, detail="回收站中不存在该资产")

    await log_audit(
        db,
        user_id=current_user.id,
        action="purge_cmdb_asset",
        target=f"cmdb_asset:{asset_id}",
        detail="永久删除资产",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(None, message="已永久删除")


@router.post(
    "/assets/{asset_id}/dependencies",
    response_model=ResponseEnvelope[CmdbAssetDependencyResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_dependency(
    dependency_in: CmdbAssetDependencyCreate,
    request: Request,
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[CmdbAssetDependencyResponse]:
    """Add one dependency edge from `asset_id` (parent) to another asset (child)."""
    if dependency_in.child_asset_id == asset_id:
        raise HTTPException(status_code=422, detail="资产不能依赖自身")

    parent = await cmdb_asset_crud.get(db, asset_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="父资产不存在")
    child = await cmdb_asset_crud.get(db, dependency_in.child_asset_id)
    if child is None:
        raise HTTPException(status_code=404, detail="子资产不存在")

    existing_children = await cmdb_asset_dependency_crud.get_children(db, asset_id)
    if any(edge.child_asset_id == dependency_in.child_asset_id for edge in existing_children):
        raise HTTPException(status_code=409, detail="依赖关系已存在")

    edge = await cmdb_asset_dependency_crud.create(
        db,
        parent_asset_id=asset_id,
        child_asset_id=dependency_in.child_asset_id,
        relation_type=dependency_in.relation_type,
    )
    await log_audit(
        db,
        user_id=current_user.id,
        action="create_cmdb_asset_dependency",
        target=f"cmdb_asset:{asset_id}",
        detail=f"新增依赖: {asset_id} -> {dependency_in.child_asset_id}（{dependency_in.relation_type}）",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(
        CmdbAssetDependencyResponse.model_validate(edge),
        message="创建成功",
        code=status.HTTP_201_CREATED,
    )


@router.get(
    "/assets/{asset_id}/dependencies",
    response_model=ResponseEnvelope[CmdbAssetDependencyListResponse],
)
async def list_dependencies(
    asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("cmdb:read")),
) -> ResponseEnvelope[CmdbAssetDependencyListResponse]:
    """List one asset's direct dependency edges in both directions."""
    asset = await cmdb_asset_crud.get(db, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")

    children = await cmdb_asset_dependency_crud.get_children(db, asset_id)
    parents = await cmdb_asset_dependency_crud.get_parents(db, asset_id)
    return success_response(
        CmdbAssetDependencyListResponse(
            children=[CmdbAssetDependencyResponse.model_validate(edge) for edge in children],
            parents=[CmdbAssetDependencyResponse.model_validate(edge) for edge in parents],
        )
    )


@router.delete(
    "/assets/{asset_id}/dependencies/{child_asset_id}",
    response_model=ResponseEnvelope[None],
)
async def delete_dependency(
    request: Request,
    asset_id: int = Path(gt=0),
    child_asset_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("cmdb:manage")),
) -> ResponseEnvelope[None]:
    """Remove one dependency edge."""
    removed = await cmdb_asset_dependency_crud.remove(
        db, parent_asset_id=asset_id, child_asset_id=child_asset_id
    )
    if not removed:
        raise HTTPException(status_code=404, detail="依赖关系不存在")

    await log_audit(
        db,
        user_id=current_user.id,
        action="delete_cmdb_asset_dependency",
        target=f"cmdb_asset:{asset_id}",
        detail=f"删除依赖: {asset_id} -> {child_asset_id}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(None, message="删除成功")
