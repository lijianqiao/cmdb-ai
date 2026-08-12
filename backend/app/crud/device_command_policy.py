"""CRUD for device command whitelist/blacklist policies."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase, ModelData
from app.models.device_command_policy import DeviceCommandPolicy


class DuplicateDeviceCommandPolicyError(ValueError):
    """同一个 (scope, asset_type 或 asset_id, command_name) 已经有一条未删除的策略。"""


class CRUDDeviceCommandPolicy(CRUDBase[DeviceCommandPolicy]):
    """设备命令策略持久化；create 额外做唯一性校验。"""

    model = DeviceCommandPolicy

    async def _find_conflicting(
        self, db: AsyncSession, obj_data: ModelData
    ) -> DeviceCommandPolicy | None:
        data = dict(obj_data)
        stmt = select(DeviceCommandPolicy).where(
            DeviceCommandPolicy.scope == data["scope"],
            DeviceCommandPolicy.command_name == data["command_name"],
            DeviceCommandPolicy.is_deleted.is_(False),
        )
        if data["scope"] == "asset_type":
            stmt = stmt.where(DeviceCommandPolicy.asset_type == data.get("asset_type"))
        else:
            stmt = stmt.where(DeviceCommandPolicy.asset_id == data.get("asset_id"))
        return (await db.execute(stmt)).scalar_one_or_none()

    async def create(self, db: AsyncSession, obj_data: ModelData) -> DeviceCommandPolicy:
        """在通用 create 之前先做唯一性检查，避免同一目标+命令出现两条冲突策略。"""
        conflict = await self._find_conflicting(db, obj_data)
        if conflict is not None:
            raise DuplicateDeviceCommandPolicyError(
                f"该目标已有一条 {obj_data['command_name']!r} 的策略（决定：{conflict.decision}）"
            )
        return await super().create(db, obj_data)

    async def resolve_policy(
        self, db: AsyncSession, *, asset_id: int, asset_type: str, command_name: str
    ) -> str | None:
        """单台设备策略优先于设备类型策略；都没有则返回 None（表示未分类）。"""
        asset_stmt = select(DeviceCommandPolicy).where(
            DeviceCommandPolicy.scope == "asset",
            DeviceCommandPolicy.asset_id == asset_id,
            DeviceCommandPolicy.command_name == command_name,
            DeviceCommandPolicy.is_deleted.is_(False),
        )
        asset_policy = (await db.execute(asset_stmt)).scalar_one_or_none()
        if asset_policy is not None:
            return asset_policy.decision

        type_stmt = select(DeviceCommandPolicy).where(
            DeviceCommandPolicy.scope == "asset_type",
            DeviceCommandPolicy.asset_type == asset_type,
            DeviceCommandPolicy.command_name == command_name,
            DeviceCommandPolicy.is_deleted.is_(False),
        )
        type_policy = (await db.execute(type_stmt)).scalar_one_or_none()
        return type_policy.decision if type_policy is not None else None

    async def get_multi_filtered(
        self, db: AsyncSession, *, skip: int = 0, limit: int = 10
    ) -> tuple[list[DeviceCommandPolicy], int]:
        """Return a page of active policies for the management page."""
        stmt = self._active_statement()
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()
        page_stmt = stmt.order_by(DeviceCommandPolicy.id.desc()).offset(skip).limit(limit)
        policies = list((await db.execute(page_stmt)).scalars().all())
        return policies, total

    async def get_deleted_multi(
        self, db: AsyncSession, *, skip: int = 0, limit: int = 10
    ) -> tuple[list[DeviceCommandPolicy], int]:
        """Return a page of soft-deleted policies for the recycle bin."""
        stmt = select(DeviceCommandPolicy).where(DeviceCommandPolicy.is_deleted.is_(True))
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()
        page_stmt = (
            stmt.order_by(DeviceCommandPolicy.updated_at.desc(), DeviceCommandPolicy.id.desc())
            .offset(skip)
            .limit(limit)
        )
        policies = list((await db.execute(page_stmt)).scalars().all())
        return policies, total

    async def restore(self, db: AsyncSession, id: int) -> DeviceCommandPolicy | None:
        """Restore a soft-deleted policy."""
        stmt = (
            select(DeviceCommandPolicy)
            .where(DeviceCommandPolicy.id == id, DeviceCommandPolicy.is_deleted.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        policy = (await db.execute(stmt)).scalar_one_or_none()
        if policy is None:
            return None
        policy.is_deleted = False
        await db.flush()
        return policy

    async def hard_delete(self, db: AsyncSession, id: int) -> bool:
        """Permanently remove a soft-deleted policy."""
        stmt = (
            select(DeviceCommandPolicy)
            .where(DeviceCommandPolicy.id == id, DeviceCommandPolicy.is_deleted.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        policy = (await db.execute(stmt)).scalar_one_or_none()
        if policy is None:
            return False
        await db.delete(policy)
        await db.flush()
        return True


device_command_policy_crud = CRUDDeviceCommandPolicy()
