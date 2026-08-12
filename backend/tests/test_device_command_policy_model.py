"""device_command_policies 模型：两种 scope 都能落库。"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud
from app.models.device_command_policy import DeviceCommandPolicy

pytestmark = pytest.mark.asyncio


async def test_asset_type_scope_policy_round_trips(db_session: AsyncSession) -> None:
    policy = DeviceCommandPolicy(
        scope="asset_type",
        asset_type="switch",
        command_name="show_version",
        decision="whitelist",
    )
    db_session.add(policy)
    await db_session.flush()

    assert policy.id is not None
    assert policy.asset_id is None
    assert policy.note == ""
    assert policy.is_deleted is False


async def test_asset_scope_policy_requires_real_asset(db_session: AsyncSession) -> None:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": "sw-policy-01",
            "ip_address": "10.0.0.96",
            "vendor": "cisco_iosxe",
        },
    )
    await db_session.flush()

    policy = DeviceCommandPolicy(
        scope="asset",
        asset_id=asset.id,
        command_name="show_running_config",
        decision="blacklist",
    )
    db_session.add(policy)
    await db_session.flush()

    assert policy.asset_type is None
