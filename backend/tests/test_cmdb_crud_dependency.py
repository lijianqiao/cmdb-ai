"""CRUD tests for CmdbAssetDependency, including graph traversal."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud

pytestmark = pytest.mark.asyncio


async def _make_asset(db_session: AsyncSession, hostname: str) -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {"asset_type": "server", "hostname": hostname, "ip_address": "", "subnet_cidr": ""},
    )
    await db_session.flush()
    return asset.id


async def test_get_children_and_parents(db_session: AsyncSession) -> None:
    switch_id = await _make_asset(db_session, "sw-01")
    server_id = await _make_asset(db_session, "srv-01")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=switch_id, child_asset_id=server_id, relation_type="uplink"
    )
    await db_session.commit()

    children = await cmdb_asset_dependency_crud.get_children(db_session, switch_id)
    parents = await cmdb_asset_dependency_crud.get_parents(db_session, server_id)

    assert [c.child_asset_id for c in children] == [server_id]
    assert [p.parent_asset_id for p in parents] == [switch_id]


async def test_traverse_down_follows_chain_within_max_depth(db_session: AsyncSession) -> None:
    switch_id = await _make_asset(db_session, "sw-01")
    rack_id = await _make_asset(db_session, "rack-01")
    server_id = await _make_asset(db_session, "srv-01")
    unreachable_id = await _make_asset(db_session, "srv-02")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=switch_id, child_asset_id=rack_id, relation_type="uplink"
    )
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=rack_id, child_asset_id=server_id, relation_type="uplink"
    )
    await db_session.commit()

    reached = await cmdb_asset_dependency_crud.traverse(
        db_session, switch_id, direction="down", max_depth=2
    )

    reached_ids = {asset_id for asset_id, _depth in reached}
    assert reached_ids == {rack_id, server_id}
    assert unreachable_id not in reached_ids
    assert dict(reached)[rack_id] == 1
    assert dict(reached)[server_id] == 2


async def test_traverse_up_follows_reverse_direction(db_session: AsyncSession) -> None:
    switch_id = await _make_asset(db_session, "sw-01")
    server_id = await _make_asset(db_session, "srv-01")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=switch_id, child_asset_id=server_id, relation_type="uplink"
    )
    await db_session.commit()

    reached = await cmdb_asset_dependency_crud.traverse(
        db_session, server_id, direction="up", max_depth=3
    )

    assert reached == [(switch_id, 1)]


async def test_remove_deletes_existing_edge(db_session: AsyncSession) -> None:
    switch_id = await _make_asset(db_session, "sw-01")
    server_id = await _make_asset(db_session, "srv-01")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=switch_id, child_asset_id=server_id, relation_type="uplink"
    )
    await db_session.commit()

    removed = await cmdb_asset_dependency_crud.remove(
        db_session, parent_asset_id=switch_id, child_asset_id=server_id
    )
    await db_session.commit()

    assert removed is True
    assert await cmdb_asset_dependency_crud.get_children(db_session, switch_id) == []


async def test_remove_returns_false_when_edge_missing(db_session: AsyncSession) -> None:
    switch_id = await _make_asset(db_session, "sw-01")
    server_id = await _make_asset(db_session, "srv-01")

    removed = await cmdb_asset_dependency_crud.remove(
        db_session, parent_asset_id=switch_id, child_asset_id=server_id
    )

    assert removed is False


async def test_traverse_is_cycle_safe(db_session: AsyncSession) -> None:
    a_id = await _make_asset(db_session, "a")
    b_id = await _make_asset(db_session, "b")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=a_id, child_asset_id=b_id, relation_type="uplink"
    )
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=b_id, child_asset_id=a_id, relation_type="uplink"
    )
    await db_session.commit()

    reached = await cmdb_asset_dependency_crud.traverse(db_session, a_id, direction="down", max_depth=10)

    assert {asset_id for asset_id, _depth in reached} == {b_id}
