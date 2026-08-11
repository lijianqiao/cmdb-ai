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

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud

logger = logging.getLogger(__name__)


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


async def run_monitor_sweep_once(db: AsyncSession) -> int:
    """Probe every active target once, record one status event each, commit.

    A probe failure for one target is logged and recorded as "down" (with the
    exception text as detail) rather than aborting the whole sweep — one bad
    target must not stop the others from being checked.
    """
    targets = await monitor_target_crud.list_active(db)
    for target in targets:
        try:
            status, latency_ms, detail = await probe_tcp(
                target.ip_address, target.port, timeout_seconds=settings.MONITOR_PROBE_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - a single target's probe must never abort the sweep
            status, latency_ms, detail = "down", None, str(exc)

        await monitor_status_event_crud.record(
            db, target_id=target.id, status=status, latency_ms=latency_ms, detail=detail
        )

    await db.commit()
    return len(targets)


async def run_monitor_sweep_loop(*, interval_seconds: float | None = None) -> None:
    """Run `run_monitor_sweep_once` forever, sleeping `interval_seconds` between rounds."""
    interval = (
        interval_seconds if interval_seconds is not None else settings.MONITOR_SWEEP_INTERVAL_SECONDS
    )
    while True:
        try:
            async with AsyncSessionLocal() as db:
                count = await run_monitor_sweep_once(db)
                logger.info("monitor sweep 完成，探测 %d 个目标", count)
        except Exception:
            logger.exception("monitor sweep 单轮失败")
        await asyncio.sleep(interval)
