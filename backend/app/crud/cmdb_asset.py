"""CRUD operations for CMDB assets."""

import ipaddress
from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase, contains_pattern
from app.models.cmdb_asset import CmdbAsset

# 「算在 CMDB 范围内」的网段最短前缀。登记成 10.0.0.0/8、0.0.0.0/0 这种过宽的网段时
# 不算命中：否则「ping 的目标不在 CMDB 内就转人工审批」（D2）会被一条登记彻底绕过。
_MIN_SUBNET_PREFIX: Mapping[int, int] = {4: 16, 6: 48}


class CRUDCmdbAsset(CRUDBase[CmdbAsset]):
    """CMDB asset persistence; generic get/create/update/soft_delete come from CRUDBase."""

    model = CmdbAsset

    async def get_by_ip(self, db: AsyncSession, ip_address: str) -> CmdbAsset | None:
        """Return one active asset by IP address, or None."""
        stmt = self._active_statement().where(CmdbAsset.ip_address == ip_address)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def covers_ip_address(self, db: AsyncSession, value: str) -> bool:
        """这个 IP 是否在 CMDB 登记范围内：等于某台资产的 IP，或落在某条 subnet_cidr 内。

        给 D2 用：ping/traceroute 的目标不在范围内时不拒绝，但不论审批档位都要人批一次。

        subnet_cidr 是自由文本（用户可能写「办公网」），解析不了的行直接跳过——一条脏
        数据不能让整条设备命令链路抛错；判不出来时按「不在范围内」处理，最多多批一次。
        """
        try:
            target = ipaddress.ip_address(value)
        except ValueError:
            return False
        # 刻意不用 get_by_ip：ip_address 没有唯一约束，重复登记时它会抛 MultipleResultsFound。
        # 这里只需要知道「有没有」，不需要那一行。
        exists_stmt = (
            select(CmdbAsset.id)
            .where(CmdbAsset.is_deleted.is_(False), CmdbAsset.ip_address == value)
            .limit(1)
        )
        if (await db.execute(exists_stmt)).first() is not None:
            return True
        stmt = select(CmdbAsset.subnet_cidr).where(
            CmdbAsset.is_deleted.is_(False), CmdbAsset.subnet_cidr != ""
        )
        result = await db.execute(stmt)
        for raw in result.scalars().all():
            try:
                network = ipaddress.ip_network(raw.strip(), strict=False)
            except ValueError:
                continue
            if network.version != target.version:
                continue
            if network.prefixlen < _MIN_SUBNET_PREFIX[network.version]:
                continue
            if target in network:
                return True
        return False

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
