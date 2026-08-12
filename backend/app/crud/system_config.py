"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: system_config.py
@DateTime: 2026-08-13 14:00
@Docs: 系统配置 CRUD，只 flush 不 commit，由调用方控制事务。
"""

from collections.abc import Collection, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_config import SystemConfig


class CRUDSystemConfig:
    """系统配置持久化：批量读取、幂等补缺与事务内 upsert。"""

    async def get_by_keys(
        self,
        db: AsyncSession,
        keys: Collection[str],
    ) -> dict[str, SystemConfig]:
        """
        按配置键批量读取。

        Args:
            db: 异步数据库会话
            keys: 待查询的配置键集合

        Returns:
            键到 ORM 行的映射；不存在的键不会出现在结果中
        """
        normalized = tuple(dict.fromkeys(keys))
        if not normalized:
            return {}
        result = await db.execute(
            select(SystemConfig).where(SystemConfig.key.in_(normalized))
        )
        return {row.key: row for row in result.scalars().all()}

    async def upsert_values(
        self,
        db: AsyncSession,
        values: Mapping[str, str | None],
        *,
        updated_by_user_id: int | None,
    ) -> dict[str, SystemConfig]:
        """
        插入或更新配置值。

        Args:
            db: 异步数据库会话
            values: 键到字符串值（或显式 None）的映射
            updated_by_user_id: 更新人用户 ID，可为空

        Returns:
            本次涉及的全部配置行
        """
        rows = await self.get_by_keys(db, values.keys())
        for key, value in values.items():
            row = rows.get(key)
            if row is None:
                row = SystemConfig(
                    key=key,
                    value=value,
                    updated_by_user_id=updated_by_user_id,
                )
                db.add(row)
                rows[key] = row
            else:
                row.value = value
                row.updated_by_user_id = updated_by_user_id
        await db.flush()
        return rows

    async def create_missing(
        self,
        db: AsyncSession,
        values: Mapping[str, str | None],
        *,
        updated_by_user_id: int | None,
    ) -> int:
        """
        仅插入尚不存在的配置键，不覆盖已有值。

        Args:
            db: 异步数据库会话
            values: 待补全的默认键值
            updated_by_user_id: 创建人用户 ID，可为空

        Returns:
            实际新增的行数
        """
        rows = await self.get_by_keys(db, values.keys())
        created = 0
        for key, value in values.items():
            if key in rows:
                continue
            row = SystemConfig(
                key=key,
                value=value,
                updated_by_user_id=updated_by_user_id,
            )
            db.add(row)
            rows[key] = row
            created += 1
        if created:
            await db.flush()
        return created


system_config_crud = CRUDSystemConfig()
