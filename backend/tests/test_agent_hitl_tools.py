"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_hitl_tools.py
@DateTime: 2026-08-12 11:26
@Docs: 验证根 Agent 的 HITL 提案工具、安全结果和专用调度边界。
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import hitl_tools, tool_dispatch
from app.agent.hitl import HitlProposalRejectedError, ProposalSafeSummary
from app.agent.loop import ToolResult
from app.agent.tool_dispatch import (
    build_root_tool_dispatcher,
    build_tool_dispatcher,
    root_tool_schemas,
)


async def test_propose_remediation_returns_pending_without_payload(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """待审批提案应停止循环，且不得把敏感载荷返回模型。"""
    captured: dict[str, object] = {}
    publisher = object()

    async def fake_propose_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        assert db is db_session
        captured.update(kwargs)
        return ProposalSafeSummary(
            proposal_id=41,
            action_type="device_control",
            status="PENDING",
            reason="恢复故障设备",
            asset_id=7,
        )

    monkeypatch.setattr(hitl_tools, "propose_action", fake_propose_action)

    result = await hitl_tools.propose_remediation(
        db_session,
        session_id=11,
        actor_user_id=13,
        proposed_by_agent_id="root-agent",
        asset_id=7,
        action_type="device_control",
        payload={"command": "reboot", "credential": "不得回传"},
        reason="恢复故障设备",
        publisher=publisher,  # type: ignore[arg-type]
    )

    assert result.control == "pending_approval"
    assert "41" in result.content
    assert "不得回传" not in result.content
    assert captured == {
        "session_id": 11,
        "actor_user_id": 13,
        "proposed_by_agent_id": "root-agent",
        "asset_id": 7,
        "action_type": "device_control",
        "payload": {"command": "reboot", "credential": "不得回传"},
        "reason": "恢复故障设备",
        "publisher": publisher,
    }


async def test_propose_remediation_returns_ok_after_notify_auto_execution(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通知已自动批准并执行时不应错误地要求人工审批。"""

    async def fake_propose_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        return ProposalSafeSummary(
            proposal_id=42,
            action_type="notify",
            status="EXECUTED",
            reason="发送告警",
            asset_id=8,
        )

    monkeypatch.setattr(hitl_tools, "propose_action", fake_propose_action)

    result = await hitl_tools.propose_remediation(
        db_session,
        session_id=12,
        actor_user_id=14,
        proposed_by_agent_id=None,
        asset_id=8,
        action_type="notify",
        payload={"message": "主机离线"},
        reason="发送告警",
    )

    assert result.control == "ok"
    assert "已自动批准并执行通知" in result.content
    assert "42" in result.content


async def test_propose_remediation_returns_actionable_rejection(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """载荷或资产校验失败应返回可操作的中文拒绝原因。"""

    async def fake_propose_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        raise HitlProposalRejectedError("payload.asset_id 与顶层 asset_id 不一致")

    monkeypatch.setattr(hitl_tools, "propose_action", fake_propose_action)

    result = await hitl_tools.propose_remediation(
        db_session,
        session_id=12,
        actor_user_id=14,
        proposed_by_agent_id=None,
        asset_id=8,
        action_type="notify",
        payload={"asset_id": 9, "message": "主机离线"},
        reason="发送告警",
    )

    assert result.control == "rejected"
    assert "asset_id" in result.content
    assert "不一致" in result.content


async def test_propose_remediation_hides_unexpected_exception_detail(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """意外异常只应暴露异常类型，不得泄露内部详情。"""

    async def fake_propose_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        raise RuntimeError("内部数据库地址")

    monkeypatch.setattr(hitl_tools, "propose_action", fake_propose_action)

    result = await hitl_tools.propose_remediation(
        db_session,
        session_id=12,
        actor_user_id=14,
        proposed_by_agent_id=None,
        asset_id=8,
        action_type="notify",
        payload={"message": "主机离线"},
        reason="发送告警",
    )

    assert result.control == "failed"
    assert "RuntimeError" in result.content
    assert "内部数据库地址" not in result.content


def test_root_schema_adds_strict_propose_remediation_definition() -> None:
    """根工具 Schema 应包含七个只读工具和一个严格写工具。"""
    schemas = root_tool_schemas()
    functions = {item["function"]["name"]: item["function"] for item in schemas}

    assert len(functions) == 8
    remediation = functions["propose_remediation"]
    parameters = remediation["parameters"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["required"]) == {"asset_id", "action_type", "payload", "reason"}
    assert parameters["properties"]["action_type"]["enum"] == ["notify", "device_control"]
    assert parameters["properties"]["payload"]["type"] == "object"


async def test_root_dispatcher_binds_context_to_remediation(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """根调度器应固定可信会话身份，并仅从模型参数接收动作内容。"""
    captured: dict[str, object] = {}
    publisher = object()

    async def fake_propose_remediation(
        db: AsyncSession,
        **kwargs: object,
    ) -> ToolResult:
        assert db is db_session
        captured.update(kwargs)
        return ToolResult(control="pending_approval", content="提案 51 待审批")

    monkeypatch.setattr(tool_dispatch, "propose_remediation", fake_propose_remediation)
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=21,
        actor_user_id=22,
        proposed_by_agent_id="root-agent",
        publisher=publisher,
    )

    result = await dispatch(
        "propose_remediation",
        {
            "asset_id": 23,
            "action_type": "notify",
            "payload": {"message": "告警"},
            "reason": "主机离线",
        },
    )

    assert result.control == "pending_approval"
    assert captured == {
        "session_id": 21,
        "actor_user_id": 22,
        "proposed_by_agent_id": "root-agent",
        "asset_id": 23,
        "action_type": "notify",
        "payload": {"message": "告警"},
        "reason": "主机离线",
        "publisher": publisher,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"asset_id": 1, "action_type": "notify", "payload": {}},
        {
            "asset_id": 1,
            "action_type": "delete",
            "payload": {},
            "reason": "非法动作",
        },
        {
            "asset_id": 1,
            "action_type": "notify",
            "payload": {},
            "reason": "额外参数",
            "session_id": 999,
        },
    ],
)
async def test_root_dispatcher_rejects_invalid_remediation_arguments(
    db_session: AsyncSession,
    arguments: dict[str, Any],
) -> None:
    """根调度器应拒绝缺字段、越界枚举和伪造可信上下文。"""
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=21,
        actor_user_id=22,
    )

    result = await dispatch("propose_remediation", arguments)

    assert result.control == "clarification"


async def test_child_dispatcher_never_exposes_remediation(
    db_session: AsyncSession,
) -> None:
    """即使持久化白名单被污染，子调度器也必须拒绝写工具。"""
    dispatch = build_tool_dispatcher(db_session, ("propose_remediation",))

    result = await dispatch(
        "propose_remediation",
        {
            "asset_id": 1,
            "action_type": "notify",
            "payload": {"message": "告警"},
            "reason": "越权尝试",
        },
    )

    assert result.control == "rejected"
    assert "未知工具" in result.content
