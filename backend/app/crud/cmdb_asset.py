"""CRUD operations for CMDB assets."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.cmdb_asset import CmdbAsset


class CRUDCmdbAsset(CRUDBase[CmdbAsset]):
    """CMDB asset persistence; generic get/create/update/soft_delete come from CRUDBase."""

    model = CmdbAsset

    async def get_by_ip(self, db: AsyncSession, ip_address: str) -> CmdbAsset | None:
        """Return one active asset by IP address, or None."""
        stmt = self._active_statement().where(CmdbAsset.ip_address == ip_address)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

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


cmdb_asset_crud = CRUDCmdbAsset()
