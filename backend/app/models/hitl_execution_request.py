"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: hitl_execution_request.py
@DateTime: 2026-09-22 12:30
@Docs: 审批通过后的执行请求元数据。只记提案、发起人、尝试号和队列状态，不记动态密码。
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 还没结束的请求。同一提案同时只能有一条，避免重启或重复点击排出第二条命令。
OPEN_EXECUTION_STATUSES = ("queued", "running", "awaiting_credential")


class HitlExecutionRequest(Base):
    """一次后台执行请求的非秘密元数据。

    用来区分「已批准、还在排队」和「已批准、可以重试」。进程在启动任务前崩溃时，
    静态凭据任务靠这张表被重新发现；动态密码只存在于当时的内存里，重启后这条记录
    改成等待重新输入，不会自动执行。
    """

    __tablename__ = "hitl_execution_requests"
    __table_args__ = (
        Index(
            "uq_hitl_exec_req_open_proposal",
            "proposal_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running', 'awaiting_credential')"),
            postgresql_where=text("status IN ('queued', 'running', 'awaiting_credential')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    proposal_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "hitl_proposals.id",
            ondelete="RESTRICT",
            name="fk_hitl_exec_req_proposal_id_hitl_proposals",
        ),
        nullable=False,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_hitl_exec_req_actor_user_id_users",
        ),
        nullable=True,
    )
    # queued / running / awaiting_credential / finished / abandoned
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # none / static / dynamic。只记凭据种类，明文密码不进这张表。
    credential_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<HitlExecutionRequest(id={self.id}, proposal_id={self.proposal_id}, "
            f"status={self.status!r})>"
        )
