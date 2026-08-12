"""SystemConfig 模型结构与约束。"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_config import SystemConfig

pytestmark = pytest.mark.asyncio


def test_key_column_is_non_null_and_unique() -> None:
    """配置键必须非空且在表内唯一。"""
    mapper = inspect(SystemConfig)
    key_col = mapper.columns["key"]
    assert key_col.nullable is False
    assert key_col.unique is True


def test_value_column_is_nullable() -> None:
    """配置值允许为空，用于显式清空秘密覆盖。"""
    mapper = inspect(SystemConfig)
    assert mapper.columns["value"].nullable is True


def test_updated_by_user_id_is_nullable_and_references_users() -> None:
    """更新人可为空，外键指向 users.id 且删除用户时置空。"""
    mapper = inspect(SystemConfig)
    col = mapper.columns["updated_by_user_id"]
    assert col.nullable is True
    foreign_keys = list(col.foreign_keys)
    assert len(foreign_keys) == 1
    assert foreign_keys[0].column.table.name == "users"
    assert foreign_keys[0].column.name == "id"
    assert foreign_keys[0].ondelete == "SET NULL"


async def test_duplicate_key_rejected(db_session: AsyncSession) -> None:
    """重复配置键在数据库层被拒绝。"""
    db_session.add(SystemConfig(key="DUP_KEY", value="a"))
    await db_session.flush()
    db_session.add(SystemConfig(key="DUP_KEY", value="b"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
