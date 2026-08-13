"""DeviceCommandPolicy CRUD：唯一性校验 + resolve_policy 优先级。"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.device_command_policy import (
    DuplicateDeviceCommandPolicyError,
    device_command_policy_crud,
)

pytestmark = pytest.mark.asyncio


async def _make_asset(db_session: AsyncSession, hostname: str = "sw-crud-01") -> int:
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": hostname,
            "ip_address": "10.0.0.97",
            "vendor": "cisco_iosxe",
        },
    )
    await db_session.flush()
    return asset.id


async def test_create_rejects_duplicate_asset_type_scope(db_session: AsyncSession) -> None:
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset_type", "asset_type": "switch", "command_name": "ping", "decision": "whitelist"},
    )
    with pytest.raises(DuplicateDeviceCommandPolicyError):
        await device_command_policy_crud.create(
            db_session,
            {"scope": "asset_type", "asset_type": "switch", "command_name": "ping", "decision": "blacklist"},
        )


async def test_resolve_policy_returns_none_when_unclassified(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(db_session)
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_id, asset_type="switch", command_name="show_version"
    )
    assert result is None


async def test_resolve_policy_falls_back_to_asset_type_level(db_session: AsyncSession) -> None:
    asset_id = await _make_asset(db_session)
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset_type",
            "asset_type": "switch",
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_id, asset_type="switch", command_name="show_version"
    )
    assert result == "whitelist"


async def test_asset_level_whitelist_overrides_asset_type_blacklist(
    db_session: AsyncSession,
) -> None:
    asset_id = await _make_asset(db_session)
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset_type",
            "asset_type": "switch",
            "command_name": "show_running_config",
            "decision": "blacklist",
        },
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_running_config",
            "decision": "whitelist",
        },
    )
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_id, asset_type="switch", command_name="show_running_config"
    )
    assert result == "whitelist"


async def test_asset_level_blacklist_overrides_asset_type_whitelist(
    db_session: AsyncSession,
) -> None:
    asset_id = await _make_asset(db_session)
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset_type", "asset_type": "switch", "command_name": "ping", "decision": "whitelist"},
    )
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset", "asset_id": asset_id, "command_name": "ping", "decision": "blacklist"},
    )
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_id, asset_type="switch", command_name="ping"
    )
    assert result == "blacklist"


async def test_resolve_policy_ignores_asset_type_whitelist_for_state_changing(
    db_session: AsyncSession,
) -> None:
    """历史遗留的 asset_type 级 reboot 白名单不得自动放行，应视为未分类。"""
    asset_id = await _make_asset(db_session)
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset_type",
            "asset_type": "switch",
            "command_name": "reboot",
            "decision": "whitelist",
        },
    )
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_id, asset_type="switch", command_name="reboot"
    )
    assert result is None


async def test_resolve_policy_ignores_other_assets_asset_level_rule(
    db_session: AsyncSession,
) -> None:
    asset_a = await _make_asset(db_session, "sw-crud-a")
    asset_b = await _make_asset(db_session, "sw-crud-b")
    await device_command_policy_crud.create(
        db_session,
        {"scope": "asset", "asset_id": asset_a, "command_name": "ping", "decision": "whitelist"},
    )
    result = await device_command_policy_crud.resolve_policy(
        db_session, asset_id=asset_b, asset_type="switch", command_name="ping"
    )
    assert result is None


async def test_soft_delete_restore_and_hard_delete_round_trip(db_session: AsyncSession) -> None:
    policy = await device_command_policy_crud.create(
        db_session,
        {"scope": "asset_type", "asset_type": "router", "command_name": "ping", "decision": "whitelist"},
    )
    assert await device_command_policy_crud.soft_delete(db_session, policy.id) is True

    deleted, total = await device_command_policy_crud.get_deleted_multi(db_session)
    assert total == 1
    assert deleted[0].id == policy.id

    restored = await device_command_policy_crud.restore(db_session, policy.id)
    assert restored is not None

    assert await device_command_policy_crud.soft_delete(db_session, policy.id) is True
    assert await device_command_policy_crud.hard_delete(db_session, policy.id) is True


async def test_restore_rejects_when_active_conflict_exists(db_session: AsyncSession) -> None:
    """软删后若已有同目标活跃策略，恢复必须失败，避免 resolve_policy 撞到多行。"""
    original = await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset_type",
            "asset_type": "firewall",
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    assert await device_command_policy_crud.soft_delete(db_session, original.id) is True

    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset_type",
            "asset_type": "firewall",
            "command_name": "show_version",
            "decision": "blacklist",
        },
    )

    with pytest.raises(DuplicateDeviceCommandPolicyError):
        await device_command_policy_crud.restore(db_session, original.id)

    # 原策略仍应留在回收站，不能被半恢复
    still_deleted = await device_command_policy_crud.get_deleted_multi(db_session)
    assert any(item.id == original.id for item in still_deleted[0])
