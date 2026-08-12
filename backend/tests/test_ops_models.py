"""Structural tests for the CMDB + monitoring ORM models."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cmdb_asset import CmdbAsset
from app.models.cmdb_asset_dependency import CmdbAssetDependency
from app.models.monitor_status_event import MonitorStatusEvent
from app.models.monitor_target import MonitorTarget
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_cmdb_asset_round_trip(db_session: AsyncSession, test_user: User) -> None:
    asset = CmdbAsset(
        asset_type="switch",
        hostname="sw-core-01",
        ip_address="10.0.0.1",
        location="机房A-机柜3",
        owner_user_id=test_user.id,
        business_system="网络基础设施",
        subnet_cidr="10.0.0.0/24",
        notes="",
    )
    db_session.add(asset)
    await db_session.commit()

    stored = (await db_session.execute(select(CmdbAsset).where(CmdbAsset.id == asset.id))).scalar_one()
    assert stored.hostname == "sw-core-01"
    assert stored.is_deleted is False


async def test_cmdb_asset_dependency_composite_key(db_session: AsyncSession) -> None:
    parent = CmdbAsset(asset_type="switch", hostname="sw-01", ip_address="10.0.0.1", subnet_cidr="")
    child = CmdbAsset(asset_type="server", hostname="srv-01", ip_address="10.0.0.2", subnet_cidr="")
    db_session.add_all([parent, child])
    await db_session.flush()

    dependency = CmdbAssetDependency(
        parent_asset_id=parent.id, child_asset_id=child.id, relation_type="uplink"
    )
    db_session.add(dependency)
    await db_session.commit()

    stored = (
        await db_session.execute(
            select(CmdbAssetDependency).where(
                CmdbAssetDependency.parent_asset_id == parent.id,
                CmdbAssetDependency.child_asset_id == child.id,
            )
        )
    ).scalar_one()
    assert stored.relation_type == "uplink"


async def test_monitor_target_defaults(db_session: AsyncSession) -> None:
    asset = CmdbAsset(asset_type="server", hostname="srv-02", ip_address="10.0.0.5", subnet_cidr="")
    db_session.add(asset)
    await db_session.flush()

    target = MonitorTarget(cmdb_asset_id=asset.id, ip_address="10.0.0.5", port=22, label="SSH")
    db_session.add(target)
    await db_session.commit()

    stored = (await db_session.execute(select(MonitorTarget).where(MonitorTarget.id == target.id))).scalar_one()
    assert stored.is_active is True
    assert stored.check_interval_seconds == 30


async def test_monitor_target_allows_ad_hoc_ip_without_cmdb_asset(db_session: AsyncSession) -> None:
    target = MonitorTarget(cmdb_asset_id=None, ip_address="10.0.0.99", port=80, label="临时探测")
    db_session.add(target)
    await db_session.commit()

    stored = (await db_session.execute(select(MonitorTarget).where(MonitorTarget.id == target.id))).scalar_one()
    assert stored.cmdb_asset_id is None


async def test_monitor_status_event_round_trip(db_session: AsyncSession) -> None:
    target = MonitorTarget(cmdb_asset_id=None, ip_address="10.0.0.5", port=22, label="")
    db_session.add(target)
    await db_session.flush()

    event = MonitorStatusEvent(target_id=target.id, status="up", latency_ms=12, detail="")
    db_session.add(event)
    await db_session.commit()

    stored = (
        await db_session.execute(select(MonitorStatusEvent).where(MonitorStatusEvent.id == event.id))
    ).scalar_one()
    assert stored.status == "up"
    assert stored.latency_ms == 12


async def test_cmdb_asset_credential_fields_default_to_none_type(
    db_session: AsyncSession,
) -> None:
    """新建资产不填凭据字段时，应落在安全的默认值上。"""
    asset = CmdbAsset(
        asset_type="server",
        hostname="srv-cred-01",
        ip_address="10.0.0.90",
    )
    db_session.add(asset)
    await db_session.flush()

    assert asset.credential_type == "none"
    assert asset.credential_username == ""
    assert asset.credential_password_encrypted is None


async def test_cmdb_asset_can_store_static_credential_ciphertext(
    db_session: AsyncSession,
) -> None:
    """静态凭据把密文原样存取，模型层不关心加密细节。"""
    asset = CmdbAsset(
        asset_type="server",
        hostname="srv-cred-02",
        ip_address="10.0.0.91",
        credential_type="static",
        credential_username="admin",
        credential_password_encrypted="gAAAAA-fake-ciphertext",
    )
    db_session.add(asset)
    await db_session.flush()
    await db_session.refresh(asset)

    assert asset.credential_type == "static"
    assert asset.credential_username == "admin"
    assert asset.credential_password_encrypted == "gAAAAA-fake-ciphertext"
