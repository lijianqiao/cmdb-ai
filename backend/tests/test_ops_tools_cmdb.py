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


# ---------------------------------------------------------------------------
# 按主机名查。
#
# eval 实测发现的缺口：运维人开口就是设备名（「SW-01 的 IP 和机房是什么」），
# 而 query_cmdb 只认 asset_ids / ip / business_system。模型只能如实回答
# 「无法按名称检索」，然后请用户先自己去查 IP——很反人类。
# 1085 条单元测试发现不了它，因为单测用假模型、假模型被告知该调什么。


async def test_query_cmdb_by_hostname_returns_asset(db_session: AsyncSession) -> None:
    """按设备名查是最自然的问法，必须支持。"""
    await _make_asset(db_session, "SW-01", "10.0.30.1", business_system="核心网")
    await _make_asset(db_session, "SW-02", "10.0.30.2", business_system="核心网")
    await db_session.commit()

    result = await query_cmdb(db_session, hostname="SW-01")

    assert result.control == "ok"
    assert "SW-01" in result.content
    assert "10.0.30.1" in result.content
    assert "SW-02" not in result.content


async def test_query_cmdb_by_hostname_is_case_insensitive(
    db_session: AsyncSession,
) -> None:
    """人打字不会在意大小写，模型转述时也可能变形；查不到会让它以为设备不存在。"""
    await _make_asset(db_session, "SW-01", "10.0.30.1")
    await db_session.commit()

    result = await query_cmdb(db_session, hostname="sw-01")

    assert "SW-01" in result.content


async def test_query_cmdb_by_hostname_can_return_multiple(
    db_session: AsyncSession,
) -> None:
    """hostname 在模型上没有唯一约束，重名必须全返回而不是只给第一个。"""
    await _make_asset(db_session, "node", "10.0.0.1")
    await _make_asset(db_session, "node", "10.0.0.2")
    await db_session.commit()

    result = await query_cmdb(db_session, hostname="node")

    assert "10.0.0.1" in result.content
    assert "10.0.0.2" in result.content


async def test_query_cmdb_unknown_hostname_reports_no_match(
    db_session: AsyncSession,
) -> None:
    """查不到要明说，不能返回空内容让模型自行脑补。"""
    result = await query_cmdb(db_session, hostname="SW-99")

    assert result.content == "没有找到匹配的资产"


async def test_query_cmdb_args_accept_hostname_as_the_only_filter() -> None:
    """hostname 要跟其余三个并列，仍然遵守「恰好一个过滤条件」的约束。"""
    from app.agent.tool_args import QueryCmdbArgs

    args = QueryCmdbArgs.model_validate({"hostname": "SW-01"})

    assert args.hostname == "SW-01"


async def test_query_cmdb_args_reject_hostname_combined_with_ip() -> None:
    """两个过滤条件一起给仍然要报错，否则语义含糊。"""
    import pydantic

    from app.agent.tool_args import QueryCmdbArgs

    with pytest.raises(pydantic.ValidationError):
        QueryCmdbArgs.model_validate({"hostname": "SW-01", "ip": "10.0.30.1"})
