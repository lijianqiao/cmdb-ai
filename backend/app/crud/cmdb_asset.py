"""CRUD operations for CMDB assets."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase, contains_pattern
from app.models.cmdb_asset import CmdbAsset


class CRUDCmdbAsset(CRUDBase[CmdbAsset]):
    """CMDB asset persistence; generic get/create/update/soft_delete come from CRUDBase."""

    model = CmdbAsset

    async def get_by_ip(self, db: AsyncSession, ip_address: str) -> CmdbAsset | None:
        """Return one active asset by IP address, or None."""
        stmt = self._active_statement().where(CmdbAsset.ip_address == ip_address)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_hostname(self, db: AsyncSession, hostname: str) -> list[CmdbAsset]:
        """Return active assets whose hostname matches, case-insensitively.

        返回列表而不是单个：hostname 在模型上没有唯一约束，重名时只给第一个
        会让调用方以为「就这一台」。大小写不敏感是因为人打字、模型转述都可能
        变形（SW-01 / sw-01），精确匹配会让 Agent 误以为设备不存在。
        """
        stmt = self._active_statement().where(
            func.lower(CmdbAsset.hostname) == hostname.lower()
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_all(self, db: AsyncSession) -> list[CmdbAsset]:
        """Return every active asset, ordered by id."""
        stmt = self._active_statement().order_by(CmdbAsset.id.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_business_system(self, db: AsyncSession, business_system: str) -> list[CmdbAsset]:
        """Return active assets tagged with a given business system."""
        stmt = self._active_statement().where(CmdbAsset.business_system == business_system)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_ids(self, db: AsyncSession, ids: list[int]) -> list[CmdbAsset]:
        """Return active assets among the given ids."""
        if not ids:
            return []
        stmt = self._active_statement().where(CmdbAsset.id.in_(ids))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_multi_filtered(
        self,
        db: AsyncSession,
        *,
        search: str | None = None,
        asset_type: str | None = None,
        business_system: str | None = None,
        skip: int = 0,
        limit: int = 10,
    ) -> tuple[list[CmdbAsset], int]:
        """Return a page of active assets for the management page."""
        stmt = self._active_statement()
        if search:
            pattern = contains_pattern(search)
            stmt = stmt.where(
                CmdbAsset.hostname.ilike(pattern, escape="\\")
                | CmdbAsset.ip_address.ilike(pattern, escape="\\")
                | CmdbAsset.business_system.ilike(pattern, escape="\\")
            )
        if asset_type:
            stmt = stmt.where(CmdbAsset.asset_type == asset_type)
        if business_system:
            stmt = stmt.where(CmdbAsset.business_system == business_system)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()

        page_stmt = stmt.order_by(CmdbAsset.id.desc()).offset(skip).limit(limit)
        assets = list((await db.execute(page_stmt)).scalars().all())
        return assets, total

    async def get_deleted_multi(
        self,
        db: AsyncSession,
        *,
        search: str | None = None,
        skip: int = 0,
        limit: int = 10,
    ) -> tuple[list[CmdbAsset], int]:
        """Return a page of soft-deleted assets for the recycle bin."""
        stmt = select(CmdbAsset).where(CmdbAsset.is_deleted.is_(True))
        if search:
            pattern = contains_pattern(search)
            stmt = stmt.where(
                CmdbAsset.hostname.ilike(pattern, escape="\\")
                | CmdbAsset.ip_address.ilike(pattern, escape="\\")
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()

        page_stmt = stmt.order_by(CmdbAsset.updated_at.desc(), CmdbAsset.id.desc()).offset(skip).limit(limit)
        assets = list((await db.execute(page_stmt)).scalars().all())
        return assets, total

    async def restore(self, db: AsyncSession, id: int) -> CmdbAsset | None:
        """Restore a soft-deleted asset."""
        stmt = (
            select(CmdbAsset)
            .where(CmdbAsset.id == id, CmdbAsset.is_deleted.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        asset = (await db.execute(stmt)).scalar_one_or_none()
        if asset is None:
            return None
        asset.is_deleted = False
        await db.flush()
        return asset

    async def hard_delete(self, db: AsyncSession, id: int) -> bool:
        """Permanently remove a soft-deleted asset."""
        stmt = (
            select(CmdbAsset)
            .where(CmdbAsset.id == id, CmdbAsset.is_deleted.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        asset = (await db.execute(stmt)).scalar_one_or_none()
        if asset is None:
            return False
        await db.delete(asset)
        await db.flush()
        return True


cmdb_asset_crud = CRUDCmdbAsset()
