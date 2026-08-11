"""CRUD tests for MonitorTarget."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.monitor_target import monitor_target_crud

pytestmark = pytest.mark.asyncio


async def test_create_and_get(db_session: AsyncSession) -> None:
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "label": "SSH"}
    )
    await db_session.commit()

    fetched = await monitor_target_crud.get(db_session, target.id)
    assert fetched is not None
    assert fetched.port == 22


async def test_list_active_excludes_inactive_targets(db_session: AsyncSession) -> None:
    active = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True}
    )
    await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.6", "port": 22, "is_active": False}
    )
    await db_session.commit()

    targets = await monitor_target_crud.list_active(db_session)

    assert [t.id for t in targets] == [active.id]


async def test_list_by_ip_prefix_matches_subnet_style_prefix(db_session: AsyncSession) -> None:
    await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22}
    )
    await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.1.5", "port": 22}
    )
    await db_session.commit()

    matches = await monitor_target_crud.list_by_ip_prefix(db_session, "10.0.0.")

    assert len(matches) == 1
    assert matches[0].ip_address == "10.0.0.5"
