"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: monitor_status_event.py
@DateTime: 2026-08-13
@Docs: 监控状态事件 CRUD，含最新状态查询与过期清理
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
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

    async def purge_older_than(
        self,
        db: AsyncSession,
        *,
        retention_days: int,
    ) -> int:
        """删除超过保留天数的过期历史事件行。

        按 ``target_id`` 分区、``checked_at desc, id desc`` 排序计算行号；
        仅删除 ``rn > 1`` 且 ``checked_at`` 早于保留截止时间的行，
        每台目标最新一行（``rn == 1``）始终保留。

        Args:
            db: 数据库会话。
            retention_days: 保留天数。

        Returns:
            本次删除的行数。
        """
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        row_number = (
            func.row_number()
            .over(
                partition_by=MonitorStatusEvent.target_id,
                order_by=(MonitorStatusEvent.checked_at.desc(), MonitorStatusEvent.id.desc()),
            )
            .label("rn")
        )
        ranked = select(
            MonitorStatusEvent.id,
            MonitorStatusEvent.checked_at,
            row_number,
        ).subquery()
        stale_ids = select(ranked.c.id).where(
            ranked.c.rn > 1,
            ranked.c.checked_at < cutoff,
        )
        result = await db.execute(
            delete(MonitorStatusEvent).where(MonitorStatusEvent.id.in_(stale_ids))
        )
        return result.rowcount or 0


monitor_status_event_crud = CRUDMonitorStatusEvent()
