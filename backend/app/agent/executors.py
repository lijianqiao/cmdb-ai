"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: executors.py
@DateTime: 2026-08-12
@Docs: HITL 动作执行器：notify 写审计日志，device_control 预留 stub。

实现流程：
1. ExecutionResult 是 hitl.resume 与各类执行器之间的统一返回契约（ok/message/detail）。
2. NotifyExecutor 从 payload 读取 message，校验非空后调用 log_audit(action=hitl_notify_executed)。
3. NotImplementedExecutor 作为 device_control 占位实现，永远返回失败，避免 stub 伪造 EXECUTED。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.audit import log_audit


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """HITL 执行器统一返回结构。"""

    ok: bool
    message: str
    detail: dict[str, object] = field(default_factory=dict)


class DeviceControlExecutor(Protocol):
    """设备管控执行器协议；真正接入通道前由 stub 实现。"""

    async def execute(self, payload: Mapping[str, object]) -> ExecutionResult:
        """执行设备管控动作并返回结果。"""
        ...


class NotImplementedExecutor:
    """device_control 占位执行器，保证 APPROVED 后无法伪装成 EXECUTED。"""

    async def execute(self, payload: Mapping[str, object]) -> ExecutionResult:
        """返回固定失败，表示执行器尚未接入。"""
        return ExecutionResult(ok=False, message="device_control 执行器尚未接入")


class NotifyExecutor:
    """低风险 notify 执行器：将通知内容写入 audit_logs。"""

    async def execute(
        self,
        db: AsyncSession,
        *,
        proposal_id: int,
        payload: Mapping[str, object],
        actor_user_id: int | None,
    ) -> ExecutionResult:
        """校验 message 并写入 hitl_notify_executed 审计记录。

        Args:
            db: 调用方事务内的数据库会话。
            proposal_id: 关联的 HITL 提案 ID。
            payload: 须包含非空字符串字段 message。
            actor_user_id: 触发执行的用户 ID，可为 None。

        Returns:
            成功时 ok=True 且已 flush 审计行；空消息时 ok=False 且不写审计。
        """
        raw_message = payload.get("message")
        if not isinstance(raw_message, str) or not raw_message.strip():
            return ExecutionResult(ok=False, message="通知消息不能为空")

        message = raw_message.strip()
        await log_audit(
            db,
            actor_user_id,
            "hitl_notify_executed",
            target=f"hitl_proposal:{proposal_id}",
            detail=message,
        )
        return ExecutionResult(
            ok=True,
            message="通知已记录",
            detail={"proposal_id": proposal_id, "message": message},
        )
