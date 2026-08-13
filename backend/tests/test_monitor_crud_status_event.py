"""CRUD tests for MonitorStatusEvent, including the latest-status window query."""

from datetime import UTC, datetime, timedelta

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


async def test_purge_older_than_removes_old_history_keeps_latest(db_session: AsyncSession) -> None:
    """超过保留期的历史行应被删除，但每台目标最新一行始终保留。"""
    target_id = await _make_target(db_session)

    old_event = await monitor_status_event_crud.record(
        db_session, target_id=target_id, status="down", detail="旧故障"
    )
    new_event = await monitor_status_event_crud.record(
        db_session, target_id=target_id, status="up", latency_ms=5
    )
    await db_session.flush()

    old_event.checked_at = datetime.now(UTC) - timedelta(days=10)
    await db_session.flush()
    db_session.expire(old_event, ["checked_at"])

    deleted = await monitor_status_event_crud.purge_older_than(db_session, retention_days=7)
    await db_session.commit()

    events = await monitor_status_event_crud.list_recent_for_target(db_session, target_id)

    assert deleted == 1
    assert len(events) == 1
    assert events[0].status == "up"
    assert events[0].id == new_event.id


async def test_purge_older_than_keeps_single_stale_event(db_session: AsyncSession) -> None:
    """仅有一行且已过期时，该行作为最新状态仍应保留。"""
    target_id = await _make_target(db_session)

    event = await monitor_status_event_crud.record(db_session, target_id=target_id, status="down")
    await db_session.flush()

    event.checked_at = datetime.now(UTC) - timedelta(days=10)
    await db_session.flush()
    db_session.expire(event, ["checked_at"])

    deleted = await monitor_status_event_crud.purge_older_than(db_session, retention_days=7)
    await db_session.commit()

    events = await monitor_status_event_crud.list_recent_for_target(db_session, target_id)

    assert deleted == 0
    assert len(events) == 1
    assert events[0].status == "down"
