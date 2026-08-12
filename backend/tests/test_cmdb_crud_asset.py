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


async def test_get_multi_filtered_paginates_and_searches(db_session: AsyncSession) -> None:
    for i in range(3):
        await cmdb_asset_crud.create(
            db_session,
            {
                "asset_type": "server",
                "hostname": f"srv-list-{i}",
                "ip_address": f"10.0.1.{i}",
                "business_system": "财务系统" if i == 0 else "",
            },
        )
    await db_session.flush()

    assets, total = await cmdb_asset_crud.get_multi_filtered(db_session, limit=2)
    assert total == 3
    assert len(assets) == 2

    filtered, filtered_total = await cmdb_asset_crud.get_multi_filtered(
        db_session, search="srv-list-0"
    )
    assert filtered_total == 1
    assert filtered[0].hostname == "srv-list-0"

    by_business, by_business_total = await cmdb_asset_crud.get_multi_filtered(
        db_session, business_system="财务系统"
    )
    assert by_business_total == 1
    assert by_business[0].hostname == "srv-list-0"


async def test_soft_delete_restore_and_hard_delete_round_trip(
    db_session: AsyncSession,
) -> None:
    asset = await cmdb_asset_crud.create(
        db_session,
        {"asset_type": "server", "hostname": "srv-trash-01", "ip_address": "10.0.2.1"},
    )
    await db_session.flush()

    assert await cmdb_asset_crud.soft_delete(db_session, asset.id) is True
    assert await cmdb_asset_crud.get(db_session, asset.id) is None

    deleted, deleted_total = await cmdb_asset_crud.get_deleted_multi(db_session)
    assert deleted_total == 1
    assert deleted[0].id == asset.id

    restored = await cmdb_asset_crud.restore(db_session, asset.id)
    assert restored is not None
    assert await cmdb_asset_crud.get(db_session, asset.id) is not None

    assert await cmdb_asset_crud.soft_delete(db_session, asset.id) is True
    assert await cmdb_asset_crud.hard_delete(db_session, asset.id) is True
    assert await cmdb_asset_crud.restore(db_session, asset.id) is None


async def test_restore_and_hard_delete_return_falsy_for_unknown_id(
    db_session: AsyncSession,
) -> None:
    assert await cmdb_asset_crud.restore(db_session, 999_999) is None
    assert await cmdb_asset_crud.hard_delete(db_session, 999_999) is False
