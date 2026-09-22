"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_postgres_hitl_execution_requests.py
@DateTime: 2026-09-22 12:40
@Docs: 可选的 PostgreSQL 验收：同一提案不能有两条未结束的执行请求。

设置 TEST_POSTGRES_DATABASE_URL 指向已经迁移过的可丢弃库才会跑。
没有这张表就跳过，不会在这里执行迁移，也不会改开发库的结构。
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

POSTGRES_DATABASE_URL = os.getenv("TEST_POSTGRES_DATABASE_URL")

pytestmark = pytest.mark.asyncio


@pytest.mark.skipif(POSTGRES_DATABASE_URL is None, reason="TEST_POSTGRES_DATABASE_URL is not configured")
async def test_open_execution_request_is_unique_per_proposal() -> None:
    """两条 queued 请求插进同一个提案时，数据库必须拒绝第二条。"""
    assert POSTGRES_DATABASE_URL is not None
    engine = create_async_engine(POSTGRES_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'hitl_execution_requests'"
                )
            )
        if exists is None:
            pytest.skip("测试库还没有 hitl_execution_requests，先迁移再验收")

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            proposal_id = await db.scalar(text("SELECT id FROM hitl_proposals LIMIT 1"))
            if proposal_id is None:
                pytest.skip("测试库没有提案行，无法验证唯一索引")
            await db.execute(
                text(
                    "INSERT INTO hitl_execution_requests "
                    "(request_id, proposal_id, attempt, status, credential_kind) "
                    "VALUES ('pg-unique-a', :proposal_id, 1, 'queued', 'static')"
                ),
                {"proposal_id": proposal_id},
            )
            with pytest.raises(IntegrityError):
                await db.execute(
                    text(
                        "INSERT INTO hitl_execution_requests "
                        "(request_id, proposal_id, attempt, status, credential_kind) "
                        "VALUES ('pg-unique-b', :proposal_id, 1, 'queued', 'static')"
                    ),
                    {"proposal_id": proposal_id},
                )
            await db.rollback()
    finally:
        await engine.dispose()
