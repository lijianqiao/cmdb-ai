"""Asynchronous SQLAlchemy engine and request-scoped session dependency."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# 需要短暂查库、中间又要等外部服务（模型、设备、子 Agent）的代码接受这两种来源：
# 会话工厂 → 每段数据库操作开一个短会话、用完即还连接；现成的会话 → 直接借用，
# 事务由调用方负责（测试夹具和已经处在一个短事务里的调用方）。
type SessionSource = AsyncSession | async_sessionmaker[AsyncSession]


@asynccontextmanager
async def session_scope(
    source: SessionSource, *, commit: bool = False
) -> AsyncIterator[AsyncSession]:
    """为一段数据库操作提供会话，离开时按来源收尾。

    AsyncSession 从第一次查询起就占住一条连接，直到 commit / rollback / close 才归还——
    await 不阻塞事件循环，不等于不占连接。所以等外部服务之前必须先离开这个作用域。

    Args:
        source: 会话工厂（新开短会话，离开时关闭）或现成的会话（借用，不关闭）
        commit: 正常离开时是否落盘——新开的会话 commit，借用的会话只 flush，
            由它的主人决定何时提交；异常离开时新开的会话随关闭回滚

    Yields:
        本段操作使用的会话
    """
    if isinstance(source, AsyncSession):
        yield source
        if commit:
            await source.flush()
        return
    async with source() as session:
        yield session
        if commit:
            await session.commit()


def _to_async_database_url(url: str) -> str:
    """Normalize common synchronous URLs to their async SQLAlchemy dialect."""
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


database_url = _to_async_database_url(settings.DATABASE_URL)

# SQL 语句输出由日志级别控制（见 app.main.configure_logging），避免 echo 再挂一层 handler 导致重复打印
if database_url.startswith("sqlite+"):
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        echo=False,
        hide_parameters=True,
    )
else:
    engine = create_async_engine(
        database_url,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
        echo=False,
        hide_parameters=True,
    )

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield one async session for the complete FastAPI dependency chain.

    CRUD functions only flush. A mutating endpoint must commit exactly once after
    both its business change and audit record succeed. Any exception rolls back
    all pending work before the session is returned to the pool.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
