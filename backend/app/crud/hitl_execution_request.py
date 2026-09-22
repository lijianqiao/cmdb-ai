"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl_execution_request.py
@DateTime: 2026-09-22 12:30
@Docs: 执行请求的写入与状态迁移。只 flush，提交由调用方的短事务负责。
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hitl_execution_request import OPEN_EXECUTION_STATUSES, HitlExecutionRequest

_CLOSED_STATUSES = frozenset({"finished", "abandoned"})


async def create_open_request(
    db: AsyncSession,
    *,
    proposal_id: int,
    request_id: str,
    actor_user_id: int,
    credential_kind: str,
) -> HitlExecutionRequest:
    """写入一条排队中的执行请求。同一提案已有未结束请求时由唯一索引拒绝。"""
    row = HitlExecutionRequest(
        request_id=request_id,
        proposal_id=proposal_id,
        attempt=1,
        actor_user_id=actor_user_id,
        status="queued",
        credential_kind=credential_kind,
    )
    db.add(row)
    await db.flush()
    return row


async def get_open_request(db: AsyncSession, proposal_id: int) -> HitlExecutionRequest | None:
    """返回这个提案还没结束的执行请求；没有则 None。"""
    result = await db.execute(
        select(HitlExecutionRequest).where(
            HitlExecutionRequest.proposal_id == proposal_id,
            HitlExecutionRequest.status.in_(OPEN_EXECUTION_STATUSES),
        )
    )
    return result.scalar_one_or_none()


async def list_open_requests(db: AsyncSession) -> list[HitlExecutionRequest]:
    """启动恢复用：所有还没结束的执行请求。"""
    result = await db.execute(
        select(HitlExecutionRequest)
        .where(HitlExecutionRequest.status.in_(OPEN_EXECUTION_STATUSES))
        .order_by(HitlExecutionRequest.id)
    )
    return list(result.scalars().all())


async def open_requests_for(
    db: AsyncSession, proposal_ids: list[int]
) -> dict[int, HitlExecutionRequest]:
    """按提案 ID 批量取出未结束的执行请求，给列表和快照用。"""
    if not proposal_ids:
        return {}
    result = await db.execute(
        select(HitlExecutionRequest).where(
            HitlExecutionRequest.proposal_id.in_(proposal_ids),
            HitlExecutionRequest.status.in_(OPEN_EXECUTION_STATUSES),
        )
    )
    return {row.proposal_id: row for row in result.scalars().all()}


async def resume_awaiting_request(
    db: AsyncSession,
    row: HitlExecutionRequest,
    *,
    actor_user_id: int,
) -> HitlExecutionRequest:
    """用户重新输入动态密码后，把等待中的请求改回排队并增加尝试号。

    不换 request_id：紧接着的重复点击要能认出是同一次请求，而不是再排一条。
    """
    row.status = "queued"
    row.attempt += 1
    row.actor_user_id = actor_user_id
    row.finished_at = None
    await db.flush()
    return row


async def set_request_status(db: AsyncSession, request_id: str, status: str) -> None:
    """按 request_id 改状态。记录已经不在时什么都不做。"""
    result = await db.execute(
        select(HitlExecutionRequest).where(HitlExecutionRequest.request_id == request_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return
    now = datetime.now(UTC)
    row.status = status
    if status == "running" and row.started_at is None:
        row.started_at = now
    if status in _CLOSED_STATUSES:
        row.finished_at = now
    await db.flush()
