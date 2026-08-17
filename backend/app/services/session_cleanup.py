"""Bounded refresh-session retention cleanup.

`purge_expired_refresh_history` 是可独立调用的单批清理；`run_session_cleanup_loop`
是挂进 app/main.py lifespan 的常驻循环。

在此之前这个模块只有手工 CLI（backend/cleanup_sessions.py）一个调用方，进程里
没有任何调度在跑它——而每次登录、每次 token 轮换都会往 refresh_sessions 写一行。
配置项 REFRESH_SESSION_HISTORY_RETENTION_DAYS 与 REFRESH_SESSION_CLEANUP_BATCH_SIZE
一直都在，说明设计上就是要定期跑的，只是没接上。
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.refresh_session import RefreshSession
from app.models.refresh_session_family import RefreshSessionFamily

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Rows removed by one short cleanup transaction."""

    sessions_deleted: int
    families_deleted: int

    @property
    def total(self) -> int:
        return self.sessions_deleted + self.families_deleted


async def purge_expired_refresh_history(
    db: AsyncSession,
    *,
    replay_grace_days: int,
    family_retention_days: int,
    batch_size: int,
    now: datetime | None = None,
) -> CleanupResult:
    """Delete one lock-safe batch while keeping transactions short."""
    current_time = now or datetime.now(UTC)
    session_cutoff = current_time - timedelta(days=replay_grace_days)
    family_cutoff = current_time - timedelta(days=family_retention_days)

    session_ids = list(
        (
            await db.scalars(
                select(RefreshSession.id)
                .where(RefreshSession.expires_at < session_cutoff)
                .order_by(RefreshSession.expires_at, RefreshSession.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if session_ids:
        await db.execute(delete(RefreshSession).where(RefreshSession.id.in_(session_ids)))

    family_ids = list(
        (
            await db.scalars(
                select(RefreshSessionFamily.id)
                .where(RefreshSessionFamily.expires_at < family_cutoff)
                .order_by(RefreshSessionFamily.expires_at, RefreshSessionFamily.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if family_ids:
        await db.execute(
            delete(RefreshSessionFamily).where(RefreshSessionFamily.id.in_(family_ids))
        )

    await db.flush()
    return CleanupResult(
        sessions_deleted=len(session_ids),
        families_deleted=len(family_ids),
    )


async def run_session_cleanup_loop(*, interval_seconds: float | None = None) -> None:
    """常驻循环：按保留期分批清理过期的 refresh 会话历史。

    每轮把当前可删的批次抽干（单批上限 REFRESH_SESSION_CLEANUP_BATCH_SIZE），
    避免一次性删除造成长事务；抽干后再睡到下一轮。

    **先睡后跑**（与 cmdb_diff 一致）：保留期清理不是启动时的紧急事项，
    过期数据多留一个周期没有任何影响。反过来在启动瞬间抢数据库连接，
    会和应用自身的初始化竞争。

    异常处理器**不查数据库**：兜底间隔取自静态配置。与 monitor_sweep / cmdb_diff
    保持同一模式——如果 except 分支自己也依赖数据库，数据库不可用时异常会穿透
    while True 让循环永久退出，清理从此静默停摆。
    """
    interval = (
        interval_seconds
        if interval_seconds is not None
        else settings.SESSION_CLEANUP_INTERVAL_SECONDS
    )
    while True:
        await asyncio.sleep(interval)
        try:
            total = 0
            while True:
                async with AsyncSessionLocal() as db:
                    result = await purge_expired_refresh_history(
                        db,
                        replay_grace_days=settings.REFRESH_SESSION_REPLAY_GRACE_DAYS,
                        family_retention_days=settings.REFRESH_SESSION_HISTORY_RETENTION_DAYS,
                        batch_size=settings.REFRESH_SESSION_CLEANUP_BATCH_SIZE,
                    )
                    await db.commit()
                total += result.total
                # 本批没删满说明已经抽干，不必继续
                if result.total == 0:
                    break
            if total:
                logger.info("refresh 会话历史清理完成，删除 %d 行", total)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("refresh 会话历史清理单轮失败，%.0f 秒后重试", interval)
