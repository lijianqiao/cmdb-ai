"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: monitor.py
@DateTime: 2026-08-13 14:00
@Docs: 监控目标管理 API：CRUD、最近探测状态与审计
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_client_ip, require_permission
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.models.monitor_status_event import MonitorStatusEvent
from app.models.monitor_target import MonitorTarget
from app.models.user import User
from app.schemas.common import PaginatedData, ResponseEnvelope, paginated_response, success_response
from app.schemas.monitor import (
    MonitorLatestStatus,
    MonitorRuntimeResponse,
    MonitorTargetCreate,
    MonitorTargetResponse,
    MonitorTargetUpdate,
)
from app.services.system_config import get_effective_operations_config
from app.utils.audit import log_audit

router = APIRouter()


def _coerce_latest_status(value: str | None) -> MonitorLatestStatus | None:
    """只接受 sweep 写入的 up/down；其他值视为尚未探测。"""
    if value == "up":
        return "up"
    if value == "down":
        return "down"
    return None


def _to_response(
    target: MonitorTarget,
    latest: MonitorStatusEvent | None,
) -> MonitorTargetResponse:
    """把目标与最近一次探测结果拼成响应。

    Args:
        target: 监控目标
        latest: 该目标最新一条探测事件，从未探测过则为 None

    Returns:
        管理页使用的监控目标响应
    """
    return MonitorTargetResponse(
        id=target.id,
        cmdb_asset_id=target.cmdb_asset_id,
        ip_address=target.ip_address,
        port=target.port,
        label=target.label,
        check_interval_seconds=target.check_interval_seconds,
        is_active=target.is_active,
        created_at=target.created_at,
        latest_status=_coerce_latest_status(None if latest is None else latest.status),
        latest_latency_ms=latest.latency_ms if latest is not None else None,
        latest_detail=latest.detail if latest is not None else "",
        latest_checked_at=latest.checked_at if latest is not None else None,
    )


async def _latest_map(
    db: AsyncSession,
    targets: list[MonitorTarget],
) -> dict[int, MonitorStatusEvent]:
    """批量查询一组目标的最近探测结果。"""
    return await monitor_status_event_crud.get_latest_status_for_targets(
        db, [target.id for target in targets]
    )


async def _ensure_cmdb_asset(db: AsyncSession, cmdb_asset_id: int | None) -> None:
    """若填写了关联资产，则校验资产存在且未删除。"""
    if cmdb_asset_id is None:
        return
    asset = await cmdb_asset_crud.get(db, cmdb_asset_id)
    if asset is None:
        raise HTTPException(status_code=422, detail="关联的 CMDB 资产不存在")


async def _ensure_unique_ip_port(
    db: AsyncSession,
    *,
    ip_address: str,
    port: int,
    exclude_id: int | None = None,
) -> None:
    """同一 IP + 端口只允许一条监控目标。"""
    existing = await monitor_target_crud.get_by_ip_port(
        db, ip_address, port, exclude_id=exclude_id
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="该 IP 与端口的监控目标已存在")


def _target_label(target: MonitorTarget) -> str:
    """审计详情用的短标签。"""
    name = target.label.strip() or target.ip_address
    return f"{name}:{target.port}"


@router.get("/runtime", response_model=ResponseEnvelope[MonitorRuntimeResponse])
async def get_monitor_runtime(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("monitor:read")),
) -> ResponseEnvelope[MonitorRuntimeResponse]:
    """返回当前生效的全局巡检间隔，供管理页轮询状态。"""
    operations = await get_effective_operations_config(db)
    return success_response(
        MonitorRuntimeResponse(
            sweep_interval_seconds=int(operations.monitor_sweep_interval_seconds)
        )
    )


@router.get("/targets", response_model=ResponseEnvelope[PaginatedData[MonitorTargetResponse]])
async def list_targets(
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=10, ge=1, le=100),
    search: str | None = Query(default=None, min_length=1, max_length=100),
    is_active: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("monitor:read")),
) -> ResponseEnvelope[PaginatedData[MonitorTargetResponse]]:
    """分页列出监控目标，并附带最近一次探测状态。"""
    targets, total = await monitor_target_crud.get_multi_filtered(
        db,
        search=search,
        is_active=is_active,
        skip=(page - 1) * page_size,
        limit=page_size,
    )
    latest_by_id = await _latest_map(db, targets)
    items = [_to_response(target, latest_by_id.get(target.id)) for target in targets]
    return paginated_response(items, total, page, page_size)


@router.post(
    "/targets",
    response_model=ResponseEnvelope[MonitorTargetResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_target(
    target_in: MonitorTargetCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("monitor:manage")),
) -> ResponseEnvelope[MonitorTargetResponse]:
    """创建监控目标；启用后下一轮 sweep 会开始探测。"""
    await _ensure_cmdb_asset(db, target_in.cmdb_asset_id)
    await _ensure_unique_ip_port(db, ip_address=target_in.ip_address, port=target_in.port)

    target = await monitor_target_crud.create(db, target_in.model_dump())
    await log_audit(
        db,
        user_id=current_user.id,
        action="create_monitor_target",
        target=f"monitor_target:{target.id}",
        detail=f"创建监控目标: {_target_label(target)}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(_to_response(target, None), message="创建成功", code=status.HTTP_201_CREATED)


@router.get("/targets/{target_id}", response_model=ResponseEnvelope[MonitorTargetResponse])
async def get_target(
    target_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("monitor:read")),
) -> ResponseEnvelope[MonitorTargetResponse]:
    """返回单个监控目标及其最近探测状态。"""
    target = await monitor_target_crud.get(db, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="监控目标不存在")
    latest_by_id = await _latest_map(db, [target])
    return success_response(_to_response(target, latest_by_id.get(target.id)))


@router.patch("/targets/{target_id}", response_model=ResponseEnvelope[MonitorTargetResponse])
async def update_target(
    target_in: MonitorTargetUpdate,
    request: Request,
    target_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("monitor:manage")),
) -> ResponseEnvelope[MonitorTargetResponse]:
    """部分更新监控目标。"""
    existing = await monitor_target_crud.get(db, target_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="监控目标不存在")

    payload = target_in.model_dump(exclude_unset=True)
    if "cmdb_asset_id" in payload:
        raw_asset_id = payload["cmdb_asset_id"]
        if raw_asset_id is None:
            await _ensure_cmdb_asset(db, None)
        elif isinstance(raw_asset_id, int):
            await _ensure_cmdb_asset(db, raw_asset_id)
        else:
            raise HTTPException(status_code=422, detail="关联的 CMDB 资产 ID 无效")

    next_ip = payload.get("ip_address", existing.ip_address)
    next_port = payload.get("port", existing.port)
    if not isinstance(next_ip, str) or not isinstance(next_port, int):
        raise HTTPException(status_code=422, detail="IP 或端口格式无效")
    await _ensure_unique_ip_port(db, ip_address=next_ip, port=next_port, exclude_id=target_id)

    updated = await monitor_target_crud.update(db, target_id, payload)
    if updated is None:
        raise HTTPException(status_code=404, detail="监控目标不存在")

    await log_audit(
        db,
        user_id=current_user.id,
        action="update_monitor_target",
        target=f"monitor_target:{target_id}",
        detail=f"更新监控目标: {_target_label(updated)}",
        ip=get_client_ip(request),
    )
    await db.commit()
    latest_by_id = await _latest_map(db, [updated])
    return success_response(_to_response(updated, latest_by_id.get(updated.id)), message="更新成功")


@router.delete("/targets/{target_id}", response_model=ResponseEnvelope[None])
async def delete_target(
    request: Request,
    target_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("monitor:manage")),
) -> ResponseEnvelope[None]:
    """硬删除监控目标，探测记录一并删除且不可恢复。"""
    existing = await monitor_target_crud.get(db, target_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="监控目标不存在")

    label = _target_label(existing)
    deleted = await monitor_target_crud.hard_delete(db, target_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="监控目标不存在")

    await log_audit(
        db,
        user_id=current_user.id,
        action="delete_monitor_target",
        target=f"monitor_target:{target_id}",
        detail=f"删除监控目标: {label}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(None, message="删除成功")
