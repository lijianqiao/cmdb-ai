"""CMDB <-> monitoring drift detection.

Compares "reachable IPs with no matching active CmdbAsset" (shadow assets)
against "active CmdbAsset entries never observed reachable" (stale entries).
Only logs findings via the existing audit_logs table — never creates,
updates, or deletes CmdbAsset/MonitorTarget rows itself
(docs/AGENT_ARCHITECTURE.md §7: automated confirmation stays out of this
job's hands; a human or a future HITL-gated proposal reconciles drift).
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.models.monitor_target import MonitorTarget
from app.services.system_config import get_effective_operations_config
from app.utils.audit import log_audit

logger = logging.getLogger(__name__)


async def run_cmdb_diff_once(db: AsyncSession) -> int:
    """Compare CMDB assets against monitor targets; log drift; commit; return finding count."""
    assets = await cmdb_asset_crud.list_all(db)
    targets = await monitor_target_crud.list_active(db)
    latest_status = await monitor_status_event_crud.get_latest_status_for_targets(
        db, [t.id for t in targets]
    )

    asset_ips = {asset.ip_address for asset in assets}
    reachable_ips = {
        target.ip_address
        for target in targets
        if target.id in latest_status and latest_status[target.id].status == "up"
    }

    findings = 0

    for ip in sorted(reachable_ips - asset_ips):
        await log_audit(
            db,
            None,
            "cmdb_drift_detected",
            target=f"ip:{ip}",
            detail=f"探测到在线但 CMDB 未登记的资产: {ip}",
            ip="local",
        )
        findings += 1

    ip_to_targets: dict[str, list[MonitorTarget]] = {}
    for target in targets:
        ip_to_targets.setdefault(target.ip_address, []).append(target)

    for asset in assets:
        asset_targets = ip_to_targets.get(asset.ip_address, [])
        ever_reachable = any(
            target.id in latest_status and latest_status[target.id].status == "up"
            for target in asset_targets
        )
        if not ever_reachable:
            await log_audit(
                db,
                None,
                "cmdb_drift_detected",
                target=f"cmdb_asset:{asset.id}",
                detail=f"CMDB 登记的资产从未探测到在线: {asset.ip_address}",
                ip="local",
            )
            findings += 1

    await db.commit()
    return findings


async def run_cmdb_diff_loop(*, interval_seconds: float | None = None) -> None:
    """Run `run_cmdb_diff_once` forever, sleeping `interval_seconds` between rounds.

    Sleeps first (unlike the monitor sweep, this job is not urgent on startup).
    """
    while True:
        sleep_interval = interval_seconds
        if sleep_interval is None:
            async with AsyncSessionLocal() as db:
                operations = await get_effective_operations_config(db)
                sleep_interval = operations.cmdb_diff_interval_seconds
        await asyncio.sleep(sleep_interval)
        try:
            async with AsyncSessionLocal() as db:
                count = await run_cmdb_diff_once(db)
                if count:
                    logger.info("cmdb diff 巡检发现 %d 条差异", count)
        except Exception:
            logger.exception("cmdb diff 单轮失败")
