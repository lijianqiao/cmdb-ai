"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: turn_registry.py
@DateTime: 2026-08-17
@Docs: 进程内正在运行的 turn 注册表，支撑用户主动中止一轮对话。

实现流程：
1. post_session_message 把 run_chat_turn 包成 asyncio.Task 并在这里登记，
   条目生命周期严格绑定该请求的 try/finally，不会泄漏。
2. 取消端点按 session_id 找到条目，记下发起人再 cancel() 那个 task。
3. 原请求捕获 CancelledError 后回查 was_cancelled_by_user，据此决定
   返回 200（用户主动停止）还是把取消原样抛出去（客户端断开/进程关停）。

**为什么必须区分这两种 CancelledError**：吞掉一个非用户发起的取消是 asyncio 里的
经典错误——进程关停时会挂住，客户端断开时会留下一个永远返回不了的请求。
标志位是唯一可靠的区分依据，不能靠「有没有人在等」之类的间接推断。

**为什么是进程内状态**：本项目强制 WEB_CONCURRENCY=1
（见 app/main.py:validate_single_worker_environment），SpawnManager 与 ws_hub
也建立在同一约束上。跨进程取消需要数据库协作标志，但那样只能在步与步之间生效，
最坏要等一次 LLM 超时加一次设备命令超时（约 135 秒）才停得下来——
用户点「停止」的预期是马上停，两分钟等于没有这个功能。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _RunningTurn:
    """一条正在运行的 turn 记录。"""

    task: asyncio.Task[Any]
    token: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    cancel_requested_by: int | None = None


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    """一次取消请求的结果。"""

    cancelled: bool
    turn_token: str | None = None


class TurnRegistry:
    """按 session_id 索引的进程内运行中 turn 表。"""

    def __init__(self) -> None:
        self._running: dict[int, _RunningTurn] = {}

    def register(self, session_id: int, token: str, task: asyncio.Task[Any]) -> None:
        """登记一条正在运行的 turn。

        同一会话同时只可能有一个 turn（由数据库租约保证），这里直接覆盖：
        如果出现残留条目，说明上一条没走到 unregister，覆盖比拒绝更安全。
        """
        existing = self._running.get(session_id)
        if existing is not None and not existing.task.done():
            logger.warning(
                "会话 %s 登记新 turn 时发现上一条仍在表中（token=%s），已覆盖",
                session_id,
                existing.token,
            )
        self._running[session_id] = _RunningTurn(task=task, token=token)

    def unregister(self, session_id: int, token: str) -> None:
        """注销一条 turn；token 不匹配时不动，避免误删他人的条目。"""
        existing = self._running.get(session_id)
        if existing is not None and existing.token == token:
            del self._running[session_id]

    def request_cancel(self, session_id: int, *, by_user_id: int) -> CancelOutcome:
        """请求中止某个会话正在运行的 turn。

        幂等：没有运行中的 turn（已结束、或已被取消过）时返回 cancelled=False，
        调用方应据此返回 200 而不是 404——用户连点两次「停止」不该看到报错。

        Args:
            session_id: 目标会话
            by_user_id: 发起取消的用户，写进标志位供原请求回查

        Returns:
            是否真的发出了取消，以及被取消的 turn token
        """
        existing = self._running.get(session_id)
        if existing is None or existing.task.done():
            return CancelOutcome(cancelled=False)

        if existing.cancel_requested_by is not None:
            # **只 cancel 一次**：hitl_execution.execute_approved_proposal 捕获
            # CancelledError 之后还要 await 一次数据库写入，把提案置成 UNKNOWN。
            # 再 cancel 一遍会把那个 await 也打断，提案就永远卡在 EXECUTING。
            return CancelOutcome(cancelled=True, turn_token=existing.token)

        existing.cancel_requested_by = by_user_id
        existing.task.cancel()
        logger.info(
            "用户 %s 中止会话 %s 的 turn（token=%s，已运行 %.1f 秒）",
            by_user_id,
            session_id,
            existing.token,
            (datetime.now(UTC) - existing.started_at).total_seconds(),
        )
        return CancelOutcome(cancelled=True, turn_token=existing.token)

    def was_cancelled_by_user(self, session_id: int, token: str) -> bool:
        """回查某条 turn 的取消是否由用户主动发起。

        原请求在捕获 CancelledError 后调用：返回 True 才能把取消转成正常响应，
        否则必须把 CancelledError 原样抛出去。
        """
        existing = self._running.get(session_id)
        if existing is None or existing.token != token:
            return False
        return existing.cancel_requested_by is not None


# 模块单例，与 spawn_manager / hub 同一模式
turn_registry = TurnRegistry()
