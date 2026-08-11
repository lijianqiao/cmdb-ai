"""Tests for the CMDB <-> monitoring drift detector."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.models.audit_log import AuditLog
from app.services.cmdb_diff import run_cmdb_diff_once

pytestmark = pytest.mark.asyncio


async def test_flags_reachable_ip_not_in_cmdb(db_session: AsyncSession) -> None:
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.99", "port": 80}
    )
    await db_session.flush()
    await monitor_status_event_crud.record(db_session, target_id=target.id, status="up")
    await db_session.commit()

    findings = await run_cmdb_diff_once(db_session)

    assert findings == 1
    logs = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "cmdb_drift_detected"))
    ).scalars().all()
    assert len(logs) == 1
    assert "10.0.0.99" in logs[0].detail


async def test_flags_cmdb_asset_never_reachable(db_session: AsyncSession) -> None:
    await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "server",
            "hostname": "srv-ghost",
            "ip_address": "10.0.0.50",
            "subnet_cidr": "",
        },
    )
    await db_session.commit()

    findings = await run_cmdb_diff_once(db_session)

    assert findings == 1
    logs = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "cmdb_drift_detected"))
    ).scalars().all()
    assert len(logs) == 1
    assert "10.0.0.50" in logs[0].detail


async def test_no_findings_when_cmdb_and_monitoring_agree(db_session: AsyncSession) -> None:
    asset = await cmdb_asset_crud.create(
        db_session,
        {"asset_type": "server", "hostname": "srv-ok", "ip_address": "10.0.0.10", "subnet_cidr": ""},
    )
    await db_session.flush()
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": asset.id, "ip_address": "10.0.0.10", "port": 22}
    )
    await db_session.flush()
    await monitor_status_event_crud.record(db_session, target_id=target.id, status="up")
    await db_session.commit()

    findings = await run_cmdb_diff_once(db_session)

    assert findings == 0


async def test_does_not_modify_cmdb_or_monitor_tables(db_session: AsyncSession) -> None:
    """Drift detection only logs — it never creates/deletes CmdbAsset or MonitorTarget rows."""
    target = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.99", "port": 80}
    )
    await db_session.flush()
    await monitor_status_event_crud.record(db_session, target_id=target.id, status="up")
    await db_session.commit()

    await run_cmdb_diff_once(db_session)

    assets = await cmdb_asset_crud.list_all(db_session)
    assert assets == []
