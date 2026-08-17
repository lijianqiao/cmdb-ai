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
from app.core.config import settings
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


async def _probe_one(
    target: MonitorTarget,
    *,
    timeout_seconds: float,
    limiter: asyncio.Semaphore,
) -> tuple[int, str, int | None, str]:
    """并发探测单个目标；任何异常都收敛成 down，保证不会中断整轮扫描。

    Args:
        target: 待探测目标。
        timeout_seconds: 单次 TCP 连接超时。
        limiter: 全轮共享的并发闸门。

    Returns:
        (target_id, status, latency_ms, detail)。
    """
    async with limiter:
        try:
            status, latency_ms, detail = await probe_tcp(
                target.ip_address,
                target.port,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            # 刻意吞掉所有异常：单个目标探测失败绝不能中断整轮扫描，
            # 也不能让 gather 取消其他并发探测。失败一律记为 down。
            status, latency_ms, detail = "down", None, str(exc)
    return target.id, status, latency_ms, detail


async def run_monitor_sweep_once(
    db: AsyncSession,
    *,
    probe_timeout_seconds: float | None = None,
    alert_publisher: MonitorAlertPublisher | None = None,
    purge_expired: bool = True,
) -> int:
    """Probe every active target once, record one status event each, commit.

    探测阶段并发执行（受 MONITOR_PROBE_CONCURRENCY 限流），落库阶段串行：
    AsyncSession 不是并发安全的，绝不能在 gather 的多个分支里共用同一个 session，
    所以并发部分只做纯网络 I/O。

    A probe failure for one target is logged and recorded as "down" (with the
    exception text as detail) rather than aborting the whole sweep — one bad
    target must not stop the others from being checked.

    状态翻转告警在数据库提交成功后发布；发布失败只记日志，不回滚已提交事实。

    Args:
        purge_expired: 本轮是否顺带清理过期历史。清理要对全表做窗口排序，属于
            低频维护动作，由调用方（run_monitor_sweep_loop）按时间节流决定，
            本函数不持有节流状态，保证单测可重复调用且行为一致。
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

    # 探测阶段：并发。用 gather 而不是 TaskGroup——TaskGroup 的语义是「任一子任务
    # 异常就取消全部兄弟」，而这里要求单台失败绝不影响其他目标，恰好相反。
    # _probe_one 内部已把异常收敛成 down，永不抛出，所以 gather 不需要 return_exceptions。
    limiter = asyncio.Semaphore(settings.MONITOR_PROBE_CONCURRENCY)
    probe_results = await asyncio.gather(
        *(
            _probe_one(target, timeout_seconds=probe_timeout_seconds, limiter=limiter)
            for target in targets
        )
    )

    # 落库阶段：串行，复用同一个 session。
    targets_by_id = {target.id: target for target in targets}
    pending_alerts: list[dict[str, object]] = []
    for target_id, status, latency_ms, detail in probe_results:
        previous_event = previous.get(target_id)
        previous_status = previous_event.status if previous_event is not None else None
        event = await monitor_status_event_crud.record_probe(
            db,
            target_id=target_id,
            status=status,
            latency_ms=latency_ms,
            detail=detail,
            # 复用上面那次批量查询的结果，避免 record_probe 为每台设备
            # 再跑一遍窗口查询（N+1）。
            current=previous_event,
        )
        if previous_status is not None and previous_status != status:
            pending_alerts.append(
                _monitor_alert_payload(targets_by_id[target_id], previous_status, event)
            )

    if purge_expired:
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
    """Run `run_monitor_sweep_once` forever, sleeping `interval_seconds` between rounds.

    异常处理器**不查数据库**：兜底间隔取自静态配置。原实现在 except 分支里
    再开一次会话读配置，数据库不可用时 try 和 except 会同时失败，异常穿透
    while True 让循环永久退出——监控从此静默停摆，且不会再有任何日志。

    过期记录清理按 MONITOR_PURGE_MIN_INTERVAL_SECONDS 节流，节流状态是本函数的
    局部变量，不做成模块级全局，避免在测试进程里跨用例泄漏。
    """
    fallback_interval = (
        interval_seconds
        if interval_seconds is not None
        else settings.MONITOR_SWEEP_INTERVAL_SECONDS
    )
    last_purge_monotonic: float | None = None
    while True:
        sweep_interval = fallback_interval
        try:
            now = time.monotonic()
            purge_expired = (
                last_purge_monotonic is None
                or now - last_purge_monotonic >= settings.MONITOR_PURGE_MIN_INTERVAL_SECONDS
            )
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
                    purge_expired=purge_expired,
                )
                logger.info("monitor sweep 完成，探测 %d 个目标", count)
            # 只在本轮真的提交成功后才推进节流计时，避免失败轮次把清理跳过一整个周期。
            if purge_expired:
                last_purge_monotonic = now
        except asyncio.CancelledError:
            # 关停信号必须放行。CancelledError 继承 BaseException 本就不会被
            # except Exception 捕获，显式写出来是为了防止后续有人改成 except BaseException。
            raise
        except Exception:
            logger.exception("monitor sweep 单轮失败，%.0f 秒后重试", sweep_interval)
        await asyncio.sleep(sweep_interval)
