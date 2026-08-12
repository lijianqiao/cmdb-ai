"""SystemConfig persistence contract."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.system_config import system_config_crud
from app.models.system_config import SystemConfig

pytestmark = pytest.mark.asyncio


async def test_create_missing_is_idempotent_and_never_overwrites(
    db_session: AsyncSession,
) -> None:
    created = await system_config_crud.create_missing(
        db_session,
        {"MONITOR_SWEEP_INTERVAL_SECONDS": "30.0"},
        updated_by_user_id=None,
    )
    await db_session.commit()
    assert created == 1

    row = (
        await db_session.execute(
            select(SystemConfig).where(
                SystemConfig.key == "MONITOR_SWEEP_INTERVAL_SECONDS"
            )
        )
    ).scalar_one()
    row.value = "45.0"
    await db_session.commit()

    created_again = await system_config_crud.create_missing(
        db_session,
        {"MONITOR_SWEEP_INTERVAL_SECONDS": "30.0"},
        updated_by_user_id=None,
    )
    await db_session.commit()
    assert created_again == 0
    assert row.value == "45.0"


async def test_upsert_supports_explicit_null_for_secret_override(
    db_session: AsyncSession,
) -> None:
    rows = await system_config_crud.upsert_values(
        db_session,
        {"LLM_CHAT_API_KEY": None},
        updated_by_user_id=None,
    )
    await db_session.commit()

    assert rows["LLM_CHAT_API_KEY"].value is None
    assert (await system_config_crud.get_by_keys(
        db_session, ["LLM_CHAT_API_KEY"]
    ))["LLM_CHAT_API_KEY"].value is None
