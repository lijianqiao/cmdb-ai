"""CRUD operations for monitor status events (append-only).

`get_latest_status_for_targets` is this module's key query: it derives each
target's "current" status from the latest event row, using a portable
ROW_NUMBER() window function rather than Postgres-only DISTINCT ON, so this
whole subsystem needs no TEST_POSTGRES_DATABASE_URL-gated test file.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.monitor_status_event import MonitorStatusEvent


class CRUDMonitorStatusEvent:
    """Append-only status-event storage plus the latest-status derivation query."""

    model = MonitorStatusEvent

    async def record(
        self,
        db: AsyncSession,
        *,
        target_id: int,
        status: str,
        latency_ms: int | None = None,
        detail: str = "",
    ) -> MonitorStatusEvent:
        """Append one probe result and flush."""
        event = MonitorStatusEvent(
            target_id=target_id, status=status, latency_ms=latency_ms, detail=detail
        )
        db.add(event)
        await db.flush()
        return event

    async def record_probe(
        self,
        db: AsyncSession,
        *,
        target_id: int,
        status: str,
        latency_ms: int | None = None,
        detail: str = "",
    ) -> MonitorStatusEvent:
        """记录一次探测结果：同状态更新当前行，变状态追加新行。

        Args:
            db: 数据库会话。
            target_id: 监控目标 ID。
            status: 探测状态（``up`` 或 ``down``）。
            latency_ms: 探测延迟（毫秒），失败时为 ``None``。
            detail: 附加说明（如错误信息）。

        Returns:
            更新或新建的状态事件行。
        """
        latest = await self.get_latest_status_for_targets(db, [target_id])
        current = latest.get(target_id)
        if current is not None and current.status == status:
            current.checked_at = datetime.now(UTC)
            current.latency_ms = latency_ms
            current.detail = detail
            await db.flush()
            db.expire(current, ["checked_at", "latency_ms", "detail"])
            return current
        return await self.record(
            db, target_id=target_id, status=status, latency_ms=latency_ms, detail=detail
        )

    async def list_recent_for_target(
        self, db: AsyncSession, target_id: int, *, limit: int = 20
    ) -> list[MonitorStatusEvent]:
        """Return a target's most recent events, newest-first."""
        stmt = (
            select(MonitorStatusEvent)
            .where(MonitorStatusEvent.target_id == target_id)
            .order_by(MonitorStatusEvent.checked_at.desc(), MonitorStatusEvent.id.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_latest_status_for_targets(
        self, db: AsyncSession, target_ids: list[int]
    ) -> dict[int, MonitorStatusEvent]:
        """Return each target's most recent event, keyed by target_id.

        Targets with no recorded events are simply absent from the result.
        """
        if not target_ids:
            return {}

        row_number = (
            func.row_number()
            .over(
                partition_by=MonitorStatusEvent.target_id,
                order_by=(MonitorStatusEvent.checked_at.desc(), MonitorStatusEvent.id.desc()),
            )
            .label("rn")
        )
        ranked = (
            select(MonitorStatusEvent, row_number)
            .where(MonitorStatusEvent.target_id.in_(target_ids))
            .subquery()
        )
        latest = aliased(MonitorStatusEvent, ranked)
        stmt = select(latest).where(ranked.c.rn == 1)

        result = await db.execute(stmt)
        events = result.scalars().all()
        return {event.target_id: event for event in events}


monitor_status_event_crud = CRUDMonitorStatusEvent()
