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


async def test_list_recent_for_targets_groups_by_target_newest_first(
    db_session: AsyncSession,
) -> None:
    """一次窗口查询按目标分组返回最近 N 条，取代逐目标查询的 N+1。"""
    first_id = await _make_target(db_session, ip="10.0.0.11")
    second_id = await _make_target(db_session, ip="10.0.0.12")

    base = datetime.now(UTC)
    for index, status in enumerate(["up", "down", "up"]):
        event = await monitor_status_event_crud.record(
            db_session, target_id=first_id, status=status
        )
        event.checked_at = base - timedelta(minutes=10 - index)
    for index, status in enumerate(["down", "up"]):
        event = await monitor_status_event_crud.record(
            db_session, target_id=second_id, status=status
        )
        event.checked_at = base - timedelta(minutes=5 - index)
    await db_session.commit()

    grouped = await monitor_status_event_crud.list_recent_for_targets(
        db_session, [first_id, second_id], limit=2
    )

    assert set(grouped) == {first_id, second_id}
    # 每组最多 limit 条，且最新在前
    assert len(grouped[first_id]) == 2
    assert grouped[first_id][0].checked_at > grouped[first_id][1].checked_at
    assert len(grouped[second_id]) == 2
    assert grouped[second_id][0].status == "up"


async def test_list_recent_for_targets_handles_empty_inputs(db_session: AsyncSession) -> None:
    """空目标列表或非正 limit 不发查询，直接返回空结果。"""
    assert await monitor_status_event_crud.list_recent_for_targets(db_session, [], limit=5) == {}

    target_id = await _make_target(db_session, ip="10.0.0.13")
    await monitor_status_event_crud.record(db_session, target_id=target_id, status="up")
    await db_session.commit()

    assert await monitor_status_event_crud.list_recent_for_targets(
        db_session, [target_id], limit=0
    ) == {}


async def test_list_recent_for_targets_omits_targets_without_events(
    db_session: AsyncSession,
) -> None:
    """从未探测过的目标直接缺席，不返回空列表条目。"""
    with_events = await _make_target(db_session, ip="10.0.0.14")
    without_events = await _make_target(db_session, ip="10.0.0.15")
    await monitor_status_event_crud.record(db_session, target_id=with_events, status="up")
    await db_session.commit()

    grouped = await monitor_status_event_crud.list_recent_for_targets(
        db_session, [with_events, without_events], limit=5
    )

    assert set(grouped) == {with_events}


# ---------------------------------------------------------------------------
# 按时间窗批量取，供监控页的可用率状态条使用。
#
# 为什么不复用 list_recent_for_targets(limit=N)：状态条要的是「最近一小时」，
# 而 N 条对应多长时间取决于该目标的探测间隔（可配 5~3600 秒）。
# 用条数限制会让快间隔的目标只覆盖到几分钟、慢间隔的目标拉回几小时前的旧数据。
# 按时间过滤既语义正确，也天然有界：一小时最多 3600/5 = 720 条/目标。


async def test_list_since_for_targets_returns_only_events_in_window(
    db_session: AsyncSession,
) -> None:
    """窗口外的旧事件不能返回，否则「最近一小时」这句话就是假的。"""
    target_id = await _make_target(db_session, ip="10.0.0.21")
    now = datetime.now(UTC)

    inside = await monitor_status_event_crud.record(
        db_session, target_id=target_id, status="up"
    )
    inside.checked_at = now - timedelta(minutes=30)
    outside = await monitor_status_event_crud.record(
        db_session, target_id=target_id, status="down"
    )
    outside.checked_at = now - timedelta(hours=2)
    await db_session.commit()

    grouped = await monitor_status_event_crud.list_since_for_targets(
        db_session, [target_id], since=now - timedelta(hours=1)
    )

    assert [status for status, _ in grouped[target_id]] == ["up"]


async def test_list_since_for_targets_groups_by_target(
    db_session: AsyncSession,
) -> None:
    """一次查询覆盖整页目标，不能退化成逐目标的 N+1。"""
    first_id = await _make_target(db_session, ip="10.0.0.22")
    second_id = await _make_target(db_session, ip="10.0.0.23")
    now = datetime.now(UTC)

    for target_id, status in ((first_id, "up"), (second_id, "down")):
        event = await monitor_status_event_crud.record(
            db_session, target_id=target_id, status=status
        )
        event.checked_at = now - timedelta(minutes=5)
    await db_session.commit()

    grouped = await monitor_status_event_crud.list_since_for_targets(
        db_session, [first_id, second_id], since=now - timedelta(hours=1)
    )

    assert grouped[first_id][0][0] == "up"
    assert grouped[second_id][0][0] == "down"


async def test_list_since_for_targets_omits_targets_without_events(
    db_session: AsyncSession,
) -> None:
    """没有事件的目标不出现在结果里，调用方按「缺席即无数据」处理。"""
    target_id = await _make_target(db_session, ip="10.0.0.24")

    grouped = await monitor_status_event_crud.list_since_for_targets(
        db_session, [target_id], since=datetime.now(UTC) - timedelta(hours=1)
    )

    assert grouped == {}


async def test_list_since_for_targets_handles_empty_target_list(
    db_session: AsyncSession,
) -> None:
    """空目标列表不发查询——列表页可能一条目标都没有。"""
    assert (
        await monitor_status_event_crud.list_since_for_targets(
            db_session, [], since=datetime.now(UTC)
        )
        == {}
    )
