"""系统配置权限与运行参数种子契约。"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import init_db
from app.crud.system_config import system_config_crud
from app.services.system_config import (
    ALL_SYSTEM_CONFIG_KEYS,
    LLM_CONFIG_KEYS,
    OPERATIONS_CONFIG_KEYS,
)
from init_db import SEED_PERMISSIONS


def test_system_config_permission_is_seeded_once() -> None:
    """system_config:manage 权限应出现在种子定义中且无重复 code。"""
    codes = [item["code"] for item in SEED_PERMISSIONS]
    assert "system_config:manage" in codes
    assert len(codes) == len(set(codes))


@pytest.mark.asyncio
async def test_seed_system_configs_creates_only_four_operational_keys(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次种子应写入四项运行配置，且不包含任何 LLM/Embedding 键。"""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(init_db, "AsyncSessionLocal", session_factory)

    assert await init_db.seed_system_configs() == 4
    assert await init_db.seed_system_configs() == 0

    async with session_factory() as db:
        rows = await system_config_crud.get_by_keys(db, ALL_SYSTEM_CONFIG_KEYS)
    assert set(rows) == set(OPERATIONS_CONFIG_KEYS)
    assert not set(rows).intersection(LLM_CONFIG_KEYS)


@pytest.mark.asyncio
async def test_seed_system_configs_preserves_existing_values(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已存在的管理员配置不应被种子覆盖。"""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(init_db, "AsyncSessionLocal", session_factory)

    async with session_factory() as db:
        await system_config_crud.create_missing(
            db,
            {"MONITOR_SWEEP_INTERVAL_SECONDS": "45.0"},
            updated_by_user_id=None,
        )
        await db.commit()

    assert await init_db.seed_system_configs() == 3

    async with session_factory() as db:
        rows = await system_config_crud.get_by_keys(
            db, ["MONITOR_SWEEP_INTERVAL_SECONDS"]
        )
    assert rows["MONITOR_SWEEP_INTERVAL_SECONDS"].value == "45.0"
