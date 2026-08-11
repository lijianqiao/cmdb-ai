"""Tests for query_monitor_status."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ops_tools import query_monitor_status
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud

pytestmark = pytest.mark.asyncio


async def _make_target_with_status(
    db_session: AsyncSession,
    ip: str,
    status: str,
) -> int:
    target = await monitor_target_crud.create(
        db_session,
        {"cmdb_asset_id": None, "ip_address": ip, "port": 22, "label": ip},
    )
    await db_session.flush()
    await monitor_status_event_crud.record(db_session, target_id=target.id, status=status)
    return target.id


async def test_query_monitor_status_by_target_ids(db_session: AsyncSession) -> None:
    target_id = await _make_target_with_status(db_session, "10.0.0.5", "up")
    await db_session.commit()

    result = await query_monitor_status(db_session, target_ids=[target_id])

    assert result.control == "ok"
    assert "10.0.0.5" in result.content
    assert "up" in result.content


async def test_query_monitor_status_by_ip_prefix(db_session: AsyncSession) -> None:
    await _make_target_with_status(db_session, "10.0.0.5", "down")
    await _make_target_with_status(db_session, "10.0.1.5", "up")
    await db_session.commit()

    result = await query_monitor_status(db_session, ip_prefix="10.0.0.")

    assert "10.0.0.5" in result.content
    assert "down" in result.content
    assert "10.0.1.5" not in result.content


async def test_query_monitor_status_reports_never_checked_target(
    db_session: AsyncSession,
) -> None:
    target = await monitor_target_crud.create(
        db_session,
        {"cmdb_asset_id": None, "ip_address": "10.0.0.9", "port": 22},
    )
    await db_session.commit()

    result = await query_monitor_status(db_session, target_ids=[target.id])

    assert result.control == "ok"
    assert "尚未探测" in result.content


async def test_query_monitor_status_reports_no_targets_found(
    db_session: AsyncSession,
) -> None:
    result = await query_monitor_status(db_session, ip_prefix="192.168.")

    assert result.control == "ok"
    assert result.content == "没有找到匹配的监控目标"
