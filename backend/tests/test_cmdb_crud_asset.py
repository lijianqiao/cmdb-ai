"""CRUD tests for CmdbAsset."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud

pytestmark = pytest.mark.asyncio


async def _make_asset(
    db_session: AsyncSession,
    *,
    hostname: str,
    ip: str,
    business_system: str = "",
    subnet_cidr: str = "",
) -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": hostname,
            "ip_address": ip,
            "business_system": business_system,
            "subnet_cidr": subnet_cidr,
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


async def test_covers_ip_address_matches_an_asset_ip(db_session: AsyncSession) -> None:
    """D2：ping 的目标就是某台登记设备的管理 IP 时，算在 CMDB 范围内。"""
    await _make_asset(db_session, hostname="sw-cov-01", ip="10.20.0.1")
    await db_session.commit()

    assert await cmdb_asset_crud.covers_ip_address(db_session, "10.20.0.1") is True
    assert await cmdb_asset_crud.covers_ip_address(db_session, "10.20.0.2") is False


async def test_covers_ip_address_tolerates_duplicate_asset_ips(db_session: AsyncSession) -> None:
    """ip_address 没有唯一约束：两台设备登记同一个 IP 时只能回答「在范围内」，不能抛错。

    抛错会顺着 gate_action 冒到门控，用户看到的是「门控建提案失败」，而不是一次 ping。
    """
    await _make_asset(db_session, hostname="sw-dup-01", ip="10.27.0.1")
    await _make_asset(db_session, hostname="sw-dup-02", ip="10.27.0.1")
    await db_session.commit()

    assert await cmdb_asset_crud.covers_ip_address(db_session, "10.27.0.1") is True


async def test_covers_ip_address_matches_a_registered_subnet(db_session: AsyncSession) -> None:
    """目标落在某台设备登记的网段里也算：从交换机 ping 本网段的终端是日常操作。"""
    await _make_asset(
        db_session, hostname="sw-cov-02", ip="10.21.0.1", subnet_cidr="10.21.0.0/24"
    )
    await db_session.commit()

    assert await cmdb_asset_crud.covers_ip_address(db_session, "10.21.0.77") is True
    assert await cmdb_asset_crud.covers_ip_address(db_session, "10.22.0.77") is False


async def test_covers_ip_address_ignores_over_broad_subnets(db_session: AsyncSession) -> None:
    """登记 10.0.0.0/8、0.0.0.0/0 这种过宽网段时不算命中，否则「不在 CMDB 就转人工」形同虚设。"""
    await _make_asset(db_session, hostname="sw-cov-03", ip="10.23.0.1", subnet_cidr="10.0.0.0/8")
    await _make_asset(db_session, hostname="sw-cov-04", ip="10.23.0.2", subnet_cidr="0.0.0.0/0")
    await db_session.commit()

    assert await cmdb_asset_crud.covers_ip_address(db_session, "10.99.99.99") is False


async def test_covers_ip_address_skips_unparsable_subnet_rows(db_session: AsyncSession) -> None:
    """subnet_cidr 是自由文本：脏数据只跳过这一行，不能让整条判定抛错。"""
    await _make_asset(db_session, hostname="sw-cov-05", ip="10.24.0.1", subnet_cidr="办公网")
    await _make_asset(
        db_session, hostname="sw-cov-06", ip="10.24.0.2", subnet_cidr=" 10.24.1.0/24 "
    )
    await db_session.commit()

    assert await cmdb_asset_crud.covers_ip_address(db_session, "10.24.1.5") is True
    assert await cmdb_asset_crud.covers_ip_address(db_session, "10.24.9.5") is False


async def test_covers_ip_address_ignores_soft_deleted_assets(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(
        db_session, hostname="sw-cov-07", ip="10.25.0.1", subnet_cidr="10.25.0.0/24"
    )
    await db_session.flush()
    await cmdb_asset_crud.soft_delete(db_session, asset_id)
    await db_session.commit()

    assert await cmdb_asset_crud.covers_ip_address(db_session, "10.25.0.1") is False
    assert await cmdb_asset_crud.covers_ip_address(db_session, "10.25.0.9") is False


async def test_covers_ip_address_handles_ipv6_and_bad_input(db_session: AsyncSession) -> None:
    """IPv6 网段阈值取 /48：比它更宽的登记（如 /32）同样不算命中。"""
    await _make_asset(
        db_session, hostname="sw-cov-08", ip="10.26.0.1", subnet_cidr="2001:db8:1::/48"
    )
    await _make_asset(
        db_session, hostname="sw-cov-09", ip="10.26.0.2", subnet_cidr="2001:db9::/32"
    )
    await db_session.commit()

    assert await cmdb_asset_crud.covers_ip_address(db_session, "2001:db8:1::5") is True
    assert await cmdb_asset_crud.covers_ip_address(db_session, "2001:db9::5") is False
    assert await cmdb_asset_crud.covers_ip_address(db_session, "not-an-ip") is False


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
                "asset_type": "switch",
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
        {"asset_type": "switch", "hostname": "srv-trash-01", "ip_address": "10.0.2.1"},
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
