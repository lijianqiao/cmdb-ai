"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl_executor.py
@DateTime: 2026-09-22
@Docs: 人工批准 / 重试后的执行放进进程内受管理的后台任务（R8 第二阶段）。

实现流程：
1. 批准 / 重试接口先 reserve 一个名额（同步，不 await；满了直接拒绝，审批不提交），
   提交审批后 start，立刻返回 202。设备命令可能要跑几十秒，不能让 HTTP 请求一直挂着——
   浏览器 30 秒就放弃等待，会把「其实已批准、正在执行」误报成失败。
2. 后台任务自己开会话，只调用唯一的 execute_approved_proposal（复核授权与策略 →
   原子认领 EXECUTING → 执行 → 落终态），不另起第二条可能重复下发命令的入口。
3. 同一提案同时只有一个执行任务：重复点重试拿到已有的任务，不会重复入队。
4. 有界：同时执行数与排队数都有上限，满了由接口返回 503。
5. 事件：execute_approved_proposal 每一步都先提交再发布；这里用即时广播的发布器，
   前端再用 execution_state（排队 / 执行中）与轮询兜底丢失的事件。
6. 设备查询执行成功后再单独生成总结并广播；总结失败不影响已落库的设备结果，也不会
   再次连接设备。
7. 执行请求的非秘密元数据落在 hitl_execution_requests：提案、发起人、请求 ID、
   尝试号、队列状态、凭据种类。动态密码只留在这个任务的内存里。
8. 进程崩溃后：启动时先把遗留 EXECUTING 转成 UNKNOWN（不重跑）。静态凭据且提案
   仍是 APPROVED 的排队请求可以重新拉起，派发前由 execute_approved_proposal 再核
   授权和策略。动态凭据请求改成 awaiting_credential，等用户重新输入。没有执行
   请求记录的历史 APPROVED 不会被当成新任务。
9. 关停时还在排队的静态请求留在表里，下次启动再恢复；动态请求改成等待重新输入。
   已经开始执行的被打断后落 UNKNOWN，请求标成 abandoned，不会自动重跑。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.device_result_summary import SummaryDelivery, deliver_device_query_summary
from app.agent.hitl import HitlResumeError
from app.agent.hitl_execution import execute_approved_proposal
from app.agent.ws_hub import WsHitlEventPublisher, hub
from app.core.config import settings
from app.crud import hitl_execution_request as execution_request_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.schemas.agent_ws import AgentWsServerMessage

logger = logging.getLogger(__name__)

type ExecutionState = Literal["queued", "running"]


@dataclass(slots=True)
class ExecutionTicket:
    """一个提案的后台执行请求；request_id 返回给前端，重复请求据此认出是同一个。"""

    proposal_id: int
    request_id: str = field(default_factory=lambda: str(uuid4()))
    state: ExecutionState = "queued"
    task: asyncio.Task[None] | None = None
    credential_kind: str = "none"
    actor_user_id: int | None = None


async def deliver_executed_query_summary(
    session_factory: async_sessionmaker[AsyncSession], proposal_id: int
) -> SummaryDelivery | None:
    """仅为已成功执行的 device_query 交付总结；失败只记日志，不影响设备成功状态。"""
    try:
        async with session_factory() as lookup:
            proposal = await hitl_proposal_crud.get(lookup, proposal_id)
            executed_query = (
                proposal is not None
                and proposal.action_type == "device_query"
                and proposal.status == "EXECUTED"
            )
        if not executed_query:
            return None
        return await deliver_device_query_summary(
            session_factory=session_factory, proposal_id=proposal_id
        )
    except Exception as exc:
        logger.warning(
            "设备查询总结交付失败 proposal_id=%s exc_type=%s", proposal_id, type(exc).__name__
        )
        return None


async def broadcast_summary_delivery(delivery: SummaryDelivery | None) -> None:
    """只广播本次新建的总结消息；广播失败不改变执行结果。"""
    if delivery is None or not delivery.created_message:
        return
    try:
        await hub.broadcast(
            delivery.session_id,
            AgentWsServerMessage(
                type="assistant_delta",
                payload={"text": delivery.content, "done": True},
            ),
        )
    except Exception as exc:
        logger.warning(
            "设备查询总结广播失败 proposal_id=%s exc_type=%s",
            delivery.proposal_id,
            type(exc).__name__,
        )


class HitlExecutionQueue:
    """进程内的审批执行队列：有界、同一提案只有一个任务。"""

    def __init__(self, *, max_running: int, max_queued: int) -> None:
        self._capacity = max_running + max_queued
        self._running_slots = asyncio.Semaphore(max_running)
        self._tickets: dict[int, ExecutionTicket] = {}
        self._accepting = True

    def active(self, proposal_id: int) -> ExecutionTicket | None:
        """这个提案当前排队或执行中的请求；没有则 None。"""
        return self._tickets.get(proposal_id)

    def reserve(self, proposal_id: int) -> ExecutionTicket | None:
        """为提案占一个新名额。已有任务或队列满时返回 None，不会覆盖正在跑的任务。"""
        if not self._accepting or proposal_id in self._tickets:
            return None
        if len(self._tickets) >= self._capacity:
            return None
        ticket = ExecutionTicket(proposal_id=proposal_id)
        self._tickets[proposal_id] = ticket
        return ticket

    def adopt(self, proposal_id: int, request_id: str) -> ExecutionTicket | None:
        """恢复时接上已经落库的请求。内存里已有任务就返回它，不再占第二个名额。"""
        existing = self._tickets.get(proposal_id)
        if existing is not None:
            return existing
        if not self._accepting or len(self._tickets) >= self._capacity:
            return None
        ticket = ExecutionTicket(proposal_id=proposal_id, request_id=request_id)
        self._tickets[proposal_id] = ticket
        return ticket

    def release(self, ticket: ExecutionTicket) -> None:
        """审批没能提交、或执行结束时归还名额。"""
        if self._tickets.get(ticket.proposal_id) is ticket:
            del self._tickets[ticket.proposal_id]

    def start(
        self,
        ticket: ExecutionTicket,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        actor_user_id: int,
        dynamic_password: str | None,
        actor_ip: str,
        credential_kind: str,
    ) -> None:
        """审批提交之后启动后台执行；动态密码只随这个任务留在内存里。"""
        if ticket.task is not None:
            return
        ticket.credential_kind = credential_kind
        ticket.actor_user_id = actor_user_id
        ticket.task = asyncio.create_task(
            self._run(
                ticket,
                session_factory=session_factory,
                actor_user_id=actor_user_id,
                dynamic_password=dynamic_password,
                actor_ip=actor_ip,
            ),
            name=f"hitl-execute:{ticket.proposal_id}",
        )

    async def _run(
        self,
        ticket: ExecutionTicket,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        actor_user_id: int,
        dynamic_password: str | None,
        actor_ip: str,
    ) -> None:
        finished = False
        try:
            async with self._running_slots:
                ticket.state = "running"
                await _set_request_status(session_factory, ticket.request_id, "running")
                summary = await execute_approved_proposal(
                    session_factory=session_factory,
                    proposal_id=ticket.proposal_id,
                    actor_user_id=actor_user_id,
                    publisher=WsHitlEventPublisher(),
                    dynamic_password=dynamic_password,
                    actor_ip=actor_ip,
                )
                if summary.status == "EXECUTED" and summary.action_type == "device_query":
                    delivery = await deliver_executed_query_summary(
                        session_factory, ticket.proposal_id
                    )
                    await broadcast_summary_delivery(delivery)
                await _set_request_status(session_factory, ticket.request_id, "finished")
                finished = True
        except HitlResumeError as exc:
            # 提案已不在可执行状态（例如被别处执行掉了）：关掉这条请求，避免恢复时再跑一次
            logger.info("后台执行未启动 proposal_id=%s reason=%s", ticket.proposal_id, exc)
            await _set_request_status(session_factory, ticket.request_id, "finished")
            finished = True
        except asyncio.CancelledError:
            if ticket.state == "queued" and ticket.credential_kind == "dynamic":
                await _set_request_status(
                    session_factory, ticket.request_id, "awaiting_credential"
                )
            elif ticket.state == "running":
                await _set_request_status(session_factory, ticket.request_id, "abandoned")
            raise
        except Exception:
            logger.exception("后台执行异常 proposal_id=%s", ticket.proposal_id)
            await _set_request_status(session_factory, ticket.request_id, "finished")
        finally:
            self.release(ticket)
            if finished and self._accepting:
                await self.pump(session_factory)

    async def pump(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """名额空出来后，把还停在表里的静态排队请求拉起来。动态密码丢了的不拉。"""
        if not self._accepting:
            return
        async with session_factory() as db:
            rows = await execution_request_crud.list_open_requests(db)
        for row in rows:
            if row.status != "queued" or row.credential_kind == "dynamic":
                continue
            if row.actor_user_id is None:
                continue
            ticket = self.adopt(row.proposal_id, row.request_id)
            if ticket is None or ticket.task is not None:
                continue
            self.start(
                ticket,
                session_factory=session_factory,
                actor_user_id=row.actor_user_id,
                dynamic_password=None,
                actor_ip="startup-recovery",
                credential_kind=row.credential_kind,
            )

    async def drain(self) -> None:
        """等当前所有执行任务结束（测试与运维排查用）。"""
        while tasks := [t.task for t in self._tickets.values() if t.task is not None]:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        """进程关停：取消所有任务。静态排队留待下次恢复，动态排队改等重新输入。"""
        self._accepting = False
        tasks = [t.task for t in list(self._tickets.values()) if t.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tickets.clear()
        self._accepting = True


async def _set_request_status(
    session_factory: async_sessionmaker[AsyncSession], request_id: str, status: str
) -> None:
    """短事务更新执行请求状态。写失败只记类型，不影响提案终态。"""
    try:
        async with session_factory() as db:
            await execution_request_crud.set_request_status(db, request_id, status)
            await db.commit()
    except Exception as exc:
        logger.warning(
            "更新执行请求状态失败 request_id=%s status=%s exc_type=%s",
            request_id,
            status,
            type(exc).__name__,
        )


async def recover_persisted_execution_requests(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """启动时处理还没结束的执行请求。必须在 EXECUTING→UNKNOWN 之后调用。

    静态凭据且提案仍是 APPROVED 的，重新排队并在派发前复核授权。动态凭据的密码
    已经随进程消失，标成等待重新输入，不执行。提案已经不是 APPROVED（含刚刚
    转成的 UNKNOWN）的请求直接关闭，绝不重跑可能已经生效的写操作。没有执行请求
    行的历史 APPROVED 不在这里，不会被自动执行。
    """
    async with session_factory() as db:
        rows = await execution_request_crud.list_open_requests(db)
        for row in rows:
            proposal = await hitl_proposal_crud.get(db, row.proposal_id)
            if proposal is None or proposal.status != "APPROVED":
                await execution_request_crud.set_request_status(db, row.request_id, "abandoned")
                continue
            if row.credential_kind == "dynamic":
                await execution_request_crud.set_request_status(
                    db, row.request_id, "awaiting_credential"
                )
                continue
            # 提案仍是 APPROVED：命令还没被认领。running 只是上次进程中断前的标记，改回排队。
            if row.status == "running":
                await execution_request_crud.set_request_status(db, row.request_id, "queued")
        await db.commit()
    await hitl_execution_queue.pump(session_factory)


hitl_execution_queue = HitlExecutionQueue(
    max_running=settings.HITL_EXECUTION_MAX_RUNNING,
    max_queued=settings.HITL_EXECUTION_MAX_QUEUED,
)
