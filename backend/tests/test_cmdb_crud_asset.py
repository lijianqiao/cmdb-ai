"""CRUD tests for CmdbAsset."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud

pytestmark = pytest.mark.asyncio


async def _make_asset(
    db_session: AsyncSession, *, hostname: str, ip: str, business_system: str = ""
) -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "server",
            "hostname": hostname,
            "ip_address": ip,
            "business_system": business_system,
            "subnet_cidr": "",
        },
    )
    await db_session.flush()
    return asset.id


async def test_get_by_ip(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(db_session, hostname="srv-01", ip="10.0.0.5")
    await db_session.commit()

    fetched = await cmdb_asset_crud.get_by_ip(db_session, "10.0.0.5")
    assert fetched is not None
    assert fetched.id == asset_id


async def test_get_by_ip_ignores_soft_deleted(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(db_session, hostname="srv-02", ip="10.0.0.6")
    await db_session.flush()
    await cmdb_asset_crud.soft_delete(db_session, asset_id)
    await db_session.commit()

    fetched = await cmdb_asset_crud.get_by_ip(db_session, "10.0.0.6")
    assert fetched is None


async def test_list_all_excludes_soft_deleted(db_session: AsyncSession) -> None:
    kept_id = await _make_asset(db_session, hostname="srv-03", ip="10.0.0.7")
    removed_id = await _make_asset(db_session, hostname="srv-04", ip="10.0.0.8")
    await db_session.flush()
    await cmdb_asset_crud.soft_delete(db_session, removed_id)
    await db_session.commit()

    assets = await cmdb_asset_crud.list_all(db_session)

    assert {a.id for a in assets} == {kept_id}


async def test_list_by_business_system_filters(db_session: AsyncSession) -> None:
    await _make_asset(db_session, hostname="srv-05", ip="10.0.0.9", business_system="财务系统")
    await _make_asset(db_session, hostname="srv-06", ip="10.0.0.10", business_system="OA系统")
    await db_session.commit()

    finance_assets = await cmdb_asset_crud.list_by_business_system(db_session, "财务系统")

    assert len(finance_assets) == 1
    assert finance_assets[0].hostname == "srv-05"


async def test_list_by_ids_preserves_only_requested(db_session: AsyncSession) -> None:
    first_id = await _make_asset(db_session, hostname="srv-07", ip="10.0.0.11")
    second_id = await _make_asset(db_session, hostname="srv-08", ip="10.0.0.12")
    await _make_asset(db_session, hostname="srv-09", ip="10.0.0.13")
    await db_session.commit()

    assets = await cmdb_asset_crud.list_by_ids(db_session, [first_id, second_id])

    assert {a.id for a in assets} == {first_id, second_id}
