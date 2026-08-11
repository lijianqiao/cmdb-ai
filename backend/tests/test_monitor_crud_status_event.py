"""CRUD tests for MonitorStatusEvent, including the latest-status window query."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud

pytestmark = pytest.mark.asyncio


async def _make_target(db_session: AsyncSession, ip: str = "10.0.0.5") -> int:
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": ip, "port": 22}
    )
    await db_session.flush()
    return target.id


async def test_record_and_list_recent_newest_first(db_session: AsyncSession) -> None:
    target_id = await _make_target(db_session)

    await monitor_status_event_crud.record(db_session, target_id=target_id, status="up", latency_ms=5)
    await monitor_status_event_crud.record(
        db_session, target_id=target_id, status="down", detail="连接被拒绝"
    )
    await db_session.commit()

    events = await monitor_status_event_crud.list_recent_for_target(db_session, target_id)

    assert [e.status for e in events] == ["down", "up"]


async def test_get_latest_status_for_targets_returns_most_recent_per_target(
    db_session: AsyncSession,
) -> None:
    target_a = await _make_target(db_session, "10.0.0.5")
    target_b = await _make_target(db_session, "10.0.0.6")

    await monitor_status_event_crud.record(db_session, target_id=target_a, status="up")
    await monitor_status_event_crud.record(db_session, target_id=target_a, status="down")
    await monitor_status_event_crud.record(db_session, target_id=target_b, status="up")
    await db_session.commit()

    latest = await monitor_status_event_crud.get_latest_status_for_targets(
        db_session, [target_a, target_b]
    )

    assert latest[target_a].status == "down"
    assert latest[target_b].status == "up"


async def test_get_latest_status_for_targets_omits_targets_with_no_events(
    db_session: AsyncSession,
) -> None:
    target_id = await _make_target(db_session)
    await db_session.commit()

    latest = await monitor_status_event_crud.get_latest_status_for_targets(db_session, [target_id])

    assert latest == {}
