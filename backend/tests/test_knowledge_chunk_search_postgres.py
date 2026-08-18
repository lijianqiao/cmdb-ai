"""Optional PostgreSQL regression for pgvector similarity search.

Set ``TEST_POSTGRES_DATABASE_URL`` to a migrated, disposable PostgreSQL
database with the vector extension available to enable this module — the
same convention as test_postgres_refresh_concurrency.py. The docker-compose
Postgres at the repo root (pgvector/pgvector:pg17, port 5433) already has the
extension; point TEST_POSTGRES_DATABASE_URL at a dedicated database on it
(not the app's own DATABASE_URL) if you want this to run locally.

This test creates its own rows inside a transaction it rolls back — it never
commits, so it leaves no residue even without per-row cleanup.
"""

import asyncio
import os
import selectors
import sys

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.models.base import Base

POSTGRES_DATABASE_URL = os.getenv("TEST_POSTGRES_DATABASE_URL")
if not POSTGRES_DATABASE_URL:
    pytest.skip("TEST_POSTGRES_DATABASE_URL is not configured", allow_module_level=True)


def _test_loop_factory() -> asyncio.AbstractEventLoop:
    """Create a local loop without using the deprecated global policy API."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.SelectorEventLoop()


def test_search_similar_orders_by_cosine_distance() -> None:
    """Run the pgvector search on a psycopg-compatible loop on every supported platform."""
    asyncio.run(_run_search_similar_test(), loop_factory=_test_loop_factory)


async def _run_search_similar_test() -> None:
    engine = create_async_engine(POSTGRES_DATABASE_URL)  # type: ignore[arg-type]
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        async with session_factory() as db:
            category = await knowledge_category_crud.create(
                db, {"code": "test-search", "name": "test", "description": ""}
            )
            await db.flush()
            document = await knowledge_document_crud.create(
                db,
                {
                    "category_id": category.id,
                    "title": "",
                    "original_filename": "a.md",
                    "file_path": "test-search/1_a.md",
                    "file_type": "md",
                    "content_hash": "e" * 64,
                    "status": "ready",
                    "uploaded_by": None,
                },
            )
            await db.flush()

            close_vector = [1.0] + [0.0] * 1023
            far_vector = [0.0] * 1023 + [1.0]
            await knowledge_chunk_crud.create(
                db,
                document_id=document.id,
                chunk_index=0,
                content="接近查询向量",
                token_count=5,
                embedding=close_vector,
            )
            await knowledge_chunk_crud.create(
                db,
                document_id=document.id,
                chunk_index=1,
                content="远离查询向量",
                token_count=5,
                embedding=far_vector,
            )

            query_embedding = [1.0] + [0.0] * 1023
            results = await knowledge_chunk_crud.search_similar(
                db, query_embedding=query_embedding, top_k=2
            )

            assert [chunk.content for chunk, _distance in results] == ["接近查询向量", "远离查询向量"]
            assert results[0][1] < results[1][1]

            await db.rollback()
    finally:
        # **不 drop_all**：本用例插入的行上面已经 rollback 掉了，清理不需要它；
        # 而 TEST_POSTGRES_DATABASE_URL 是多个模块共用的同一个库，
        # test_postgres_refresh_concurrency 要求库**已迁移**。字母序下本模块先跑，
        # 一旦在这里清空 schema，那边整组就会以「未迁移」失败——两个模块单独跑
        # 都是绿的，一起跑才炸，是最难查的那种。
        await engine.dispose()
