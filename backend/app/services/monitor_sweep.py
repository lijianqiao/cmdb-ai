"""TCP-connect probing and the single-pass monitor sweep.

`run_monitor_sweep_once` is the deterministic core (docs/guide.md §1.3: this
is a rules-clear, reproducible task, not something an Agent should decide how
to do). `run_monitor_sweep_loop` is a thin infinite wrapper wired into
app/main.py's lifespan — it is not itself unit-tested (see this plan's Global
Constraints).
"""

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ws_hub import WsMonitorAlertPublisher
from app.core.database import AsyncSessionLocal
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.models.monitor_status_event import MonitorStatusEvent
from app.models.monitor_target import MonitorTarget
from app.services.system_config import get_effective_operations_config

logger = logging.getLogger(__name__)

_monitor_alert_publisher = WsMonitorAlertPublisher()


class MonitorAlertPublisher(Protocol):
    """监控告警发布器协议：由 monitor sweep 在提交后调用。"""

    async def publish_monitor_alert(self, payload: Mapping[str, object]) -> None:
        """发布一条不含凭据与探测原文的安全告警摘要。"""
        ...


def _monitor_alert_payload(
    target: MonitorTarget,
    previous_status: str,
    event: MonitorStatusEvent,
) -> dict[str, object]:
    """构造前端横幅所需的安全告警字段，不包含探测 detail 或内部配置。"""
    if event.status == "down":
        title = "设备离线告警"
        severity = "critical"
    else:
        title = "设备恢复通知"
        severity = "info"

    target_label = target.label.strip()
    endpoint = f"{target.ip_address}:{target.port}"
    if target_label:
        message = f"{target_label} ({endpoint}) 状态由 {previous_status} 变为 {event.status}"
    else:
        message = f"{endpoint} 状态由 {previous_status} 变为 {event.status}"

    return {
        "target_id": target.id,
        "asset_id": target.cmdb_asset_id,
        "asset_name": target.label,
        "ip_address": target.ip_address,
        "port": target.port,
        "previous_status": previous_status,
        "status": event.status,
        "latency_ms": event.latency_ms,
        "checked_at": event.checked_at.isoformat(),
        "title": title,
        "message": message,
        "severity": severity,
    }


async def probe_tcp(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
    """Attempt a TCP connect; return (status, latency_ms, detail).

    status is "up" on a successful connect, "down" on timeout or any
    connection error. latency_ms is None when the probe did not succeed.
    """
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout_seconds
        )
    except TimeoutError:
        return "down", None, "连接超时"
    except OSError as exc:
        return "down", None, str(exc)

    latency_ms = int((time.monotonic() - start) * 1000)
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return "up", latency_ms, ""


async def run_monitor_sweep_once(
    db: AsyncSession,
    *,
    probe_timeout_seconds: float | None = None,
    alert_publisher: MonitorAlertPublisher | None = None,
) -> int:
    """Probe every active target once, record one status event each, commit.

    A probe failure for one target is logged and recorded as "down" (with the
    exception text as detail) rather than aborting the whole sweep — one bad
    target must not stop the others from being checked.

    状态翻转告警在数据库提交成功后发布；发布失败只记日志，不回滚已提交事实。
    """
    if probe_timeout_seconds is None:
        operations = await get_effective_operations_config(db)
        probe_timeout_seconds = operations.monitor_probe_timeout_seconds
    else:
        operations = None

    targets = await monitor_target_crud.list_active(db)
    previous = await monitor_status_event_crud.get_latest_status_for_targets(
        db, [target.id for target in targets]
    )
    pending_alerts: list[dict[str, object]] = []

    for target in targets:
        previous_status = previous[target.id].status if target.id in previous else None
        try:
            status, latency_ms, detail = await probe_tcp(
                target.ip_address,
                target.port,
                timeout_seconds=probe_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - a single target's probe must never abort the sweep
            status, latency_ms, detail = "down", None, str(exc)

        event = await monitor_status_event_crud.record_probe(
            db,
            target_id=target.id,
            status=status,
            latency_ms=latency_ms,
            detail=detail,
        )
        if previous_status is not None and previous_status != status:
            pending_alerts.append(_monitor_alert_payload(target, previous_status, event))

    if operations is None:
        operations = await get_effective_operations_config(db)
    await monitor_status_event_crud.purge_older_than(
        db, retention_days=operations.monitor_event_retention_days
    )

    await db.commit()
    if alert_publisher is not None:
        for payload in pending_alerts:
            try:
                await alert_publisher.publish_monitor_alert(payload)
            except Exception:
                logger.exception(
                    "monitor_alert 发布失败", extra={"target_id": payload["target_id"]}
                )
    return len(targets)


async def run_monitor_sweep_loop(*, interval_seconds: float | None = None) -> None:
    """Run `run_monitor_sweep_once` forever, sleeping `interval_seconds` between rounds."""
    while True:
        sweep_interval = 0.0
        try:
            async with AsyncSessionLocal() as db:
                operations = await get_effective_operations_config(db)
                sweep_interval = (
                    interval_seconds
                    if interval_seconds is not None
                    else operations.monitor_sweep_interval_seconds
                )
                count = await run_monitor_sweep_once(
                    db,
                    probe_timeout_seconds=operations.monitor_probe_timeout_seconds,
                    alert_publisher=_monitor_alert_publisher,
                )
                logger.info("monitor sweep 完成，探测 %d 个目标", count)
        except Exception:
            logger.exception("monitor sweep 单轮失败")
            async with AsyncSessionLocal() as db:
                operations = await get_effective_operations_config(db)
                sweep_interval = (
                    interval_seconds
                    if interval_seconds is not None
                    else operations.monitor_sweep_interval_seconds
                )
        await asyncio.sleep(sweep_interval)
