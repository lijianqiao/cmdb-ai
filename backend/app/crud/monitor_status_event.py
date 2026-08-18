"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: monitor_status_event.py
@DateTime: 2026-08-13
@Docs: 监控状态事件 CRUD，含最新状态查询与过期清理
"""

from datetime import UTC, datetime, timedelta
from typing import TypedDict, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.crud.base import contains_pattern
from app.models.monitor_status_event import MonitorStatusEvent
from app.models.monitor_target import MonitorTarget


class MonitorLogItemRow(TypedDict):
    """监控日志列表行，含目标展示字段。"""

    id: int
    target_id: int
    label: str
    ip_address: str
    port: int
    status: str
    latency_ms: int | None
    detail: str
    checked_at: datetime


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
        current: MonitorStatusEvent | None = None,
    ) -> MonitorStatusEvent:
        """记录一次探测结果：同状态更新当前行，变状态追加新行。

        Args:
            db: 数据库会话。
            target_id: 监控目标 ID。
            status: 探测状态（``up`` 或 ``down``）。
            latency_ms: 探测延迟（毫秒），失败时为 ``None``。
            detail: 附加说明（如错误信息）。
            current: 调用方已批量查得的当前行。探活扫描会先用一次
                ``get_latest_status_for_targets`` 拿到全部目标的当前状态，
                传进来可以避免在循环里对每台设备重复跑一遍窗口查询。
                省略时退回自行查询，保持既有调用点行为不变。

        Returns:
            更新或新建的状态事件行。
        """
        if current is None:
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

    async def list_logs(
        self,
        db: AsyncSession,
        *,
        target_id: int | None = None,
        status: str | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int = 10,
    ) -> tuple[list[MonitorLogItemRow], int]:
        """分页列出监控状态变化日志，并附带目标展示字段。

        Args:
            db: 数据库会话。
            target_id: 可选，按监控目标筛选。
            status: 可选，按探测状态（``up`` / ``down``）筛选。
            search: 可选，对目标标签与 IP 做模糊匹配。
            skip: 分页偏移。
            limit: 每页条数。

        Returns:
            日志行列表与符合条件的总条数。
        """
        filters = []
        if target_id is not None:
            filters.append(MonitorStatusEvent.target_id == target_id)
        if status is not None:
            filters.append(MonitorStatusEvent.status == status)
        if search:
            pattern = contains_pattern(search)
            filters.append(
                MonitorTarget.label.ilike(pattern, escape="\\")
                | MonitorTarget.ip_address.ilike(pattern, escape="\\")
            )

        stmt = (
            select(
                MonitorStatusEvent.id,
                MonitorStatusEvent.target_id,
                MonitorTarget.label,
                MonitorTarget.ip_address,
                MonitorTarget.port,
                MonitorStatusEvent.status,
                MonitorStatusEvent.latency_ms,
                MonitorStatusEvent.detail,
                MonitorStatusEvent.checked_at,
            )
            .join(MonitorTarget, MonitorStatusEvent.target_id == MonitorTarget.id)
            .where(*filters)
        )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await db.execute(count_stmt)).scalar_one()

        page_stmt = stmt.order_by(
            MonitorStatusEvent.checked_at.desc(),
            MonitorStatusEvent.id.desc(),
        ).offset(skip).limit(limit)
        rows = (await db.execute(page_stmt)).all()
        items: list[MonitorLogItemRow] = [
            {
                "id": row.id,
                "target_id": row.target_id,
                "label": row.label,
                "ip_address": row.ip_address,
                "port": row.port,
                "status": row.status,
                "latency_ms": row.latency_ms,
                "detail": row.detail,
                "checked_at": row.checked_at,
            }
            for row in rows
        ]
        return items, total

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

    async def list_since_for_targets(
        self, db: AsyncSession, target_ids: list[int], *, since: datetime
    ) -> dict[int, list[tuple[str, datetime]]]:
        """按 target 分组返回 ``since`` 之后的 (status, checked_at)。

        为什么按时间而不是按条数（对比 list_recent_for_targets）：可用率状态条
        要的是「最近一小时」，而 N 条对应多长时间取决于该目标的探测间隔
        （可配 5~3600 秒）。用条数限制会让快间隔的目标只覆盖到几分钟、
        慢间隔的目标反而拉回几小时前的旧数据，画出来的条是错的。

        按时间过滤也天然有界：一小时最多 3600/5 = 720 条/目标。

        只取两列而不是整行 ORM 对象：调用方只需要状态和时间，
        一页 10 个目标最多 7200 行，少搬几个字段就少几 MB 的无谓开销。

        Args:
            db: 数据库会话。
            target_ids: 目标 ID 列表。
            since: 时间下界（含）。

        Returns:
            target_id → [(status, checked_at)]，按时间升序。没有事件的目标不出现。
        """
        if not target_ids:
            return {}

        stmt = (
            select(
                MonitorStatusEvent.target_id,
                MonitorStatusEvent.status,
                MonitorStatusEvent.checked_at,
            )
            .where(
                MonitorStatusEvent.target_id.in_(target_ids),
                MonitorStatusEvent.checked_at >= since,
            )
            # 与 ix_monitor_status_events_target_checked 同序，走索引不用额外排序
            .order_by(MonitorStatusEvent.target_id, MonitorStatusEvent.checked_at)
        )

        grouped: dict[int, list[tuple[str, datetime]]] = {}
        for target_id, status, checked_at in (await db.execute(stmt)).all():
            grouped.setdefault(target_id, []).append((status, checked_at))
        return grouped

    async def list_recent_for_targets(
        self, db: AsyncSession, target_ids: list[int], *, limit: int
    ) -> dict[int, list[MonitorStatusEvent]]:
        """按 target 分组返回各自最近 ``limit`` 条事件，每组最新在前。

        一次窗口查询取代「逐个目标调 list_recent_for_target」的 N 次往返。
        没有事件的目标不会出现在结果里。

        Args:
            db: 数据库会话。
            target_ids: 目标 ID 列表。
            limit: 每个目标返回的最大条数。

        Returns:
            target_id → 最近事件列表（最新在前）。
        """
        if not target_ids or limit <= 0:
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
        recent = aliased(MonitorStatusEvent, ranked)
        # 按 (target_id, rn) 排序：rn 保证组内最新在前，target_id 让扫描顺序
        # 与 ix_monitor_status_events_target_checked 一致，避免跨目标交错。
        stmt = (
            select(recent)
            .where(ranked.c.rn <= limit)
            .order_by(ranked.c.target_id, ranked.c.rn)
        )

        grouped: dict[int, list[MonitorStatusEvent]] = {}
        for event in (await db.execute(stmt)).scalars().all():
            grouped.setdefault(event.target_id, []).append(event)
        return grouped

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
        result = cast(
            CursorResult[tuple[()]],
            await db.execute(
                delete(MonitorStatusEvent).where(MonitorStatusEvent.id.in_(stale_ids))
            ),
        )
        return result.rowcount or 0


monitor_status_event_crud = CRUDMonitorStatusEvent()
