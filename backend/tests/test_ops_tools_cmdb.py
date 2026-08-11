"""Tests for query_cmdb and query_cmdb_dependencies."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ops_tools import query_cmdb, query_cmdb_dependencies
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.cmdb_asset_dependency import cmdb_asset_dependency_crud

pytestmark = pytest.mark.asyncio


async def _make_asset(db_session: AsyncSession, hostname: str, ip: str, business_system: str = "") -> int:
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


async def test_query_cmdb_by_ip_returns_asset_details(db_session: AsyncSession) -> None:
    await _make_asset(db_session, "srv-01", "10.0.0.5", business_system="财务系统")
    await db_session.commit()

    result = await query_cmdb(db_session, ip="10.0.0.5")

    assert result.control == "ok"
    assert "srv-01" in result.content
    assert "财务系统" in result.content


async def test_query_cmdb_no_filters_returns_all(db_session: AsyncSession) -> None:
    await _make_asset(db_session, "srv-01", "10.0.0.5")
    await _make_asset(db_session, "srv-02", "10.0.0.6")
    await db_session.commit()

    result = await query_cmdb(db_session)

    assert result.control == "ok"
    assert "srv-01" in result.content
    assert "srv-02" in result.content


async def test_query_cmdb_reports_no_matches(db_session: AsyncSession) -> None:
    result = await query_cmdb(db_session, ip="10.0.0.99")

    assert result.control == "ok"
    assert result.content == "没有找到匹配的资产"


async def test_query_cmdb_dependencies_reports_chain(db_session: AsyncSession) -> None:
    switch_id = await _make_asset(db_session, "sw-01", "10.0.0.1")
    server_id = await _make_asset(db_session, "srv-01", "10.0.0.5")
    await cmdb_asset_dependency_crud.create(
        db_session, parent_asset_id=switch_id, child_asset_id=server_id, relation_type="uplink"
    )
    await db_session.commit()

    result = await query_cmdb_dependencies(db_session, switch_id, direction="down")

    assert result.control == "ok"
    assert "srv-01" in result.content


async def test_query_cmdb_dependencies_reports_empty_graph(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(db_session, "srv-01", "10.0.0.5")
    await db_session.commit()

    result = await query_cmdb_dependencies(db_session, asset_id, direction="down")

    assert result.control == "ok"
    assert result.content == "没有找到依赖关系"
