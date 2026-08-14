"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_hitl_tools.py
@DateTime: 2026-08-12 11:26
@Docs: 验证根 Agent 的 HITL 提案工具、安全结果和专用调度边界。
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import hitl_tools, tool_dispatch
from app.agent.executors import ExecutionResult
from app.agent.hitl import HitlProposalRejectedError, ProposalSafeSummary, gate_action
from app.agent.hitl_gate import HitlGateHook, dispatch_through_hitl_gate
from app.agent.loop import ToolResult
from app.agent.tool_dispatch import (
    build_root_tool_dispatcher,
    build_tool_dispatcher,
    root_tool_schemas,
)
from app.crud.agent_session import agent_session_crud
from app.crud.cmdb_asset import cmdb_asset_crud
from app.crud.hitl_proposal import hitl_proposal_crud
from app.models.user import User


async def _make_session_and_asset(
    db: AsyncSession, user_id: int
) -> tuple[int, int]:
    """创建 Agent 会话与 CMDB 资产，供设备命令工具测试使用。"""
    session = await agent_session_crud.create(
        db,
        {"user_id": user_id, "title": "设备命令工具测试", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db,
        {
            "asset_type": "server",
            "hostname": "srv-device-query",
            "ip_address": "10.0.0.50",
            "business_system": "测试系统",
            "subnet_cidr": "",
        },
    )
    await db.flush()
    return session.id, asset.id


def _hitl_session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)


def _make_gated_dispatch(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    session_id: int,
    actor_user_id: int,
) -> tuple[HitlGateHook, object]:
    gate = HitlGateHook(
        _hitl_session_factory(db_engine),
        session_id=session_id,
        actor_user_id=actor_user_id,
    )
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=session_id,
        actor_user_id=actor_user_id,
        gate_hook=gate,
    )
    return gate, dispatch


async def test_gate_before_notify_returns_pending_without_payload(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """待审批提案应停止循环，且不得把敏感载荷返回模型。"""
    captured: dict[str, object] = {}
    publisher = object()

    async def fake_gate_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        captured.update(kwargs)
        return ProposalSafeSummary(
            proposal_id=41,
            action_type="notify",
            status="PENDING",
            reason="恢复故障设备",
            asset_id=7,
        )

    monkeypatch.setattr("app.agent.hitl_gate.gate_action", fake_gate_action)

    gate = HitlGateHook(
        _hitl_session_factory(db_engine),
        session_id=11,
        actor_user_id=13,
        proposed_by_agent_id="root-agent",
        publisher=publisher,  # type: ignore[arg-type]
    )
    decision = await gate.before(
        "notify",
        {
            "asset_id": 7,
            "payload": {"message": "不得回传"},
            "reason": "恢复故障设备",
        },
    )

    assert decision.block is True
    assert decision.result is not None
    assert decision.result.control == "pending_approval"
    assert "41" in decision.result.content
    assert "不得回传" not in decision.result.content
    assert captured["asset_id"] == 7
    assert captured["action_type"] == "notify"


async def test_notify_thin_tool_fails_closed_without_executor(
    db_session: AsyncSession,
) -> None:
    """薄工具不得直接执行；应失败关闭并说明由门控统一处理。"""

    result = await hitl_tools.notify(
        db_session,
        session_id=12,
        actor_user_id=14,
        proposed_by_agent_id=None,
        asset_id=8,
        payload={"message": "主机离线"},
        reason="发送告警",
        gate_hook=None,
    )

    assert result.control == "failed"
    assert "门控" in result.content
    assert "执行" in result.content


async def test_gate_before_notify_returns_actionable_rejection(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """载荷或资产校验失败应返回可操作的中文拒绝原因。"""

    async def fake_gate_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        raise HitlProposalRejectedError("payload.asset_id 与顶层 asset_id 不一致")

    monkeypatch.setattr("app.agent.hitl_gate.gate_action", fake_gate_action)

    gate = HitlGateHook(_hitl_session_factory(db_engine), session_id=12, actor_user_id=14)
    decision = await gate.before(
        "notify",
        {
            "asset_id": 8,
            "payload": {"message": "主机离线"},
            "reason": "发送告警",
        },
    )

    assert decision.block is True
    assert decision.result is not None
    assert decision.result.control == "rejected"
    assert "asset_id" in decision.result.content
    assert "不一致" in decision.result.content


async def test_gate_before_notify_rejects_extra_secret_without_echo(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """真实校验路径拒绝额外密钥字段时，工具结果不得回显密钥值。"""
    session = await agent_session_crud.create(
        db_session,
        {"user_id": test_user.id, "title": "HITL 工具拒绝", "status": "active"},
    )
    asset = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "server",
            "hostname": "srv-hitl-tool",
            "ip_address": "10.0.0.21",
            "business_system": "测试系统",
            "subnet_cidr": "",
        },
    )
    await db_session.flush()
    secret = "SECRET_TOOL_REJECT_TOKEN_Y7"

    gate = HitlGateHook(
        _hitl_session_factory(db_engine),
        session_id=session.id,
        actor_user_id=test_user.id,
    )
    decision = await gate.before(
        "notify",
        {
            "asset_id": asset.id,
            "payload": {"message": "主机离线", "password": secret},
            "reason": "额外密钥字段工具回归",
        },
    )

    assert decision.block is True
    assert decision.result is not None
    assert decision.result.control == "clarification"
    assert secret not in decision.result.content
    assert "input_value" not in decision.result.content
    assert "password" in decision.result.content


async def test_gate_before_notify_hides_unexpected_exception_detail(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """意外异常只应暴露异常类型，不得泄露内部详情。"""

    async def fake_gate_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        raise RuntimeError("内部数据库地址")

    monkeypatch.setattr("app.agent.hitl_gate.gate_action", fake_gate_action)

    gate = HitlGateHook(_hitl_session_factory(db_engine), session_id=12, actor_user_id=14)
    decision = await gate.before(
        "notify",
        {
            "asset_id": 8,
            "payload": {"message": "主机离线"},
            "reason": "发送告警",
        },
    )

    assert decision.block is True
    assert decision.result is not None
    assert decision.result.control == "failed"
    assert "RuntimeError" in decision.result.content
    assert "内部数据库地址" not in decision.result.content


async def test_gate_before_device_control_returns_pending(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_gate_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        return ProposalSafeSummary(
            proposal_id=60,
            action_type="device_control",
            status="PENDING",
            reason="故障恢复",
            asset_id=9,
        )

    monkeypatch.setattr("app.agent.hitl_gate.gate_action", fake_gate_action)

    gate = HitlGateHook(_hitl_session_factory(db_engine), session_id=1, actor_user_id=2)
    decision = await gate.before(
        "device_control",
        {"asset_id": 9, "command_name": "reboot", "reason": "故障恢复"},
    )
    assert decision.block is True
    assert decision.result is not None
    assert decision.result.control == "pending_approval"
    assert "60" in decision.result.content


async def test_device_control_thin_tool_fails_closed_without_executor(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """设备管控薄工具不得直接执行外部命令。"""

    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)
    result = await hitl_tools.device_control(
        db_session,
        session_id=session_id,
        actor_user_id=2,
        proposed_by_agent_id=None,
        asset_id=asset_id,
        command_name="reboot",
        interface_name=None,
        reason="故障恢复",
        gate_hook=None,
    )
    assert result.control == "failed"
    assert "门控" in result.content


def test_root_schema_has_notify_and_device_control_without_propose() -> None:
    schemas = root_tool_schemas()
    functions = {item["function"]["name"]: item["function"] for item in schemas}
    assert len(functions) == 12
    assert "notify" in functions
    assert "device_control" in functions
    assert "propose_remediation" not in functions
    assert "propose_device_control" not in functions

    notify = functions["notify"]
    assert "message" in notify["parameters"]["properties"]["payload"]["properties"]

    control = functions["device_control"]
    control_params = control["parameters"]
    assert control_params["additionalProperties"] is False
    assert set(control_params["required"]) == {"asset_id", "command_name", "reason"}
    assert set(control_params["properties"]["command_name"]["enum"]) == {
        "show_version",
        "show_running_config",
        "show_interfaces",
        "ping",
        "reboot",
        "shutdown",
        "port_enable",
        "port_disable",
    }


async def test_root_dispatcher_routes_device_control(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def fake_device_control(db: AsyncSession, **kwargs: object) -> ToolResult:
        captured.update(kwargs)
        return ToolResult(control="pending_approval", content="提案 70 待审批")

    monkeypatch.setattr(tool_dispatch, "device_control", fake_device_control)
    dispatch = build_root_tool_dispatcher(db_session, session_id=21, actor_user_id=22)

    result = await dispatch(
        "device_control",
        {"asset_id": 9, "command_name": "port_disable", "interface_name": "Gi0/1", "reason": "端口异常"},
    )
    assert result.control == "pending_approval"
    assert captured["command_name"] == "port_disable"
    assert captured["interface_name"] == "Gi0/1"


async def test_root_dispatcher_binds_context_to_notify(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """根调度器应固定可信会话身份，并仅从模型参数接收动作内容。"""
    captured: dict[str, object] = {}
    publisher = object()

    async def fake_notify(db: AsyncSession, **kwargs: object) -> ToolResult:
        assert db is db_session
        captured.update(kwargs)
        return ToolResult(control="pending_approval", content="提案 51 待审批")

    monkeypatch.setattr(tool_dispatch, "notify", fake_notify)
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=21,
        actor_user_id=22,
        proposed_by_agent_id="root-agent",
        publisher=publisher,
    )

    result = await dispatch(
        "notify",
        {
            "asset_id": 23,
            "payload": {"message": "告警"},
            "reason": "主机离线",
        },
    )

    assert result.control == "pending_approval"
    assert captured["session_id"] == 21
    assert captured["actor_user_id"] == 22
    assert captured["asset_id"] == 23
    assert captured["payload"] == {"message": "告警"}
    assert captured["reason"] == "主机离线"


@pytest.mark.parametrize(
    "arguments",
    [
        {"asset_id": 1, "payload": {}},
        {
            "asset_id": 1,
            "payload": {"message": "告警"},
            "reason": "额外参数",
            "session_id": 999,
        },
    ],
)
async def test_root_dispatcher_rejects_invalid_notify_arguments(
    db_session: AsyncSession,
    arguments: dict[str, Any],
) -> None:
    """根调度器应拒绝缺字段、越界枚举和伪造可信上下文。"""
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=21,
        actor_user_id=22,
    )

    result = await dispatch("notify", arguments)

    assert result.control == "clarification"


async def test_child_dispatcher_rejects_notify_execution_tool(
    db_session: AsyncSession,
) -> None:
    """子调度器若被污染点到 notify，必须拒绝。"""
    dispatch = build_tool_dispatcher(db_session, ("query_monitor_status", "notify"))

    result = await dispatch(
        "notify",
        {
            "asset_id": 1,
            "payload": {"message": "告警"},
            "reason": "越权尝试",
        },
    )

    assert result.control == "rejected"
    assert "未知工具" in result.content


async def test_root_dispatcher_rejects_command_name_outside_catalog_enum(
    db_session: AsyncSession,
) -> None:
    """command_name 不在目录枚举里应在校验阶段被拒绝，不进入 gate_action。"""
    dispatch = build_root_tool_dispatcher(
        db_session,
        session_id=21,
        actor_user_id=22,
    )

    result = await dispatch(
        "query_device_command",
        {
            "asset_id": 1,
            "command_name": "show running-config",
            "reason": "猜测的真实 CLI 语法，不是目录里的语义 key",
        },
    )

    assert result.control == "clarification"


async def test_child_dispatcher_never_exposes_execution_tools(
    db_session: AsyncSession,
) -> None:
    """即使持久化白名单被污染，子调度器也必须拒绝写工具。"""
    dispatch = build_tool_dispatcher(db_session, ("notify",))

    result = await dispatch(
        "notify",
        {
            "asset_id": 1,
            "payload": {"message": "告警"},
            "reason": "越权尝试",
        },
    )

    assert result.control == "rejected"
    assert "未知工具" in result.content


async def test_query_device_command_returns_pending_when_not_executed(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """待审批设备命令查询应停止循环并返回提案 ID。"""

    async def fake_gate_action(db: AsyncSession, **kwargs: object) -> ProposalSafeSummary:
        return ProposalSafeSummary(
            proposal_id=51,
            action_type="device_query",
            status="PENDING",
            reason="排查交换机",
            asset_id=9,
        )

    monkeypatch.setattr("app.agent.hitl_gate.gate_action", fake_gate_action)

    gate = HitlGateHook(_hitl_session_factory(db_engine), session_id=1, actor_user_id=2)
    decision = await gate.before(
        "query_device_command",
        {"asset_id": 9, "command_name": "show_version", "reason": "排查交换机"},
    )

    assert decision.block is True
    assert decision.result is not None
    assert decision.result.control == "pending_approval"
    assert "51" in decision.result.content


async def test_query_device_command_thin_tool_fails_closed_without_executor(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """设备查询薄工具不得直接执行外部命令。"""

    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)
    result = await hitl_tools.query_device_command(
        db_session,
        session_id=session_id,
        actor_user_id=2,
        proposed_by_agent_id=None,
        asset_id=asset_id,
        command_name="show_version",
        reason="排查交换机",
        gate_hook=None,
    )

    assert result.control == "failed"
    assert "门控" in result.content


async def test_get_device_query_result_scopes_to_session(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """回查结果必须按会话隔离，不匹配时当作不存在。"""
    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)
    other_session_id, _ = await _make_session_and_asset(db_session, test_user.id)

    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_query",
        action_payload={"asset_id": asset_id, "command_name": "show_version"},
    )
    await db_session.commit()

    same_session = await hitl_tools.get_device_query_result(
        db_session, session_id=session_id, proposal_id=proposal.id
    )
    assert "不存在" not in same_session.content

    other_session = await hitl_tools.get_device_query_result(
        db_session, session_id=other_session_id, proposal_id=proposal.id
    )
    assert other_session.control == "rejected"


async def test_get_device_query_result_reports_unknown_state(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """UNKNOWN 时回查文案必须说明需人工核实，不能说正在执行。"""
    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)
    proposal = await hitl_proposal_crud.create(
        db_session,
        session_id=session_id,
        proposed_by_agent_id=None,
        action_type="device_query",
        action_payload={
            "asset_id": asset_id,
            "command_name": "show_version",
            "proposal_reason": "test",
        },
    )
    proposal.status = "UNKNOWN"
    proposal.status_reason = "dispatch_outcome_unknown"
    await db_session.flush()

    result = await hitl_tools.get_device_query_result(
        db_session, session_id=session_id, proposal_id=proposal.id
    )
    assert result.control == "ok"
    assert "不确定" in result.content
    assert "正在执行" not in result.content


async def test_query_device_command_thin_tool_never_calls_executor_on_failure_path(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """薄工具失败路径也不得触发执行器。"""

    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)
    result = await hitl_tools.query_device_command(
        db_session,
        session_id=session_id,
        actor_user_id=2,
        proposed_by_agent_id=None,
        asset_id=asset_id,
        command_name="show_version",
        reason="排查交换机",
        gate_hook=None,
    )
    assert result.control == "failed"
    assert "门控" in result.content


async def test_list_device_commands_rejects_missing_asset(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    session_id, _ = await _make_session_and_asset(db_session, test_user.id)
    result = await hitl_tools.list_device_commands_for_asset(
        db_session, session_id=session_id, asset_id=987654
    )
    assert result.control == "rejected"
    assert "不存在" in result.content


async def test_list_device_commands_rejects_asset_without_vendor(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """vendor 为空时要给出可行动提示，而不是泛泛失败。"""
    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)

    result = await hitl_tools.list_device_commands_for_asset(
        db_session, session_id=session_id, asset_id=asset_id
    )

    assert result.control == "rejected"
    assert "vendor" in result.content


async def test_list_device_commands_reports_policy_and_credential_state(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    """命令清单应含白名单/黑名单/需审批标注与凭据前提提示。"""
    from app.crud.device_command_policy import device_command_policy_crud

    session_id, asset_id = await _make_session_and_asset(db_session, test_user.id)
    asset = await cmdb_asset_crud.get(db_session, asset_id)
    assert asset is not None
    asset.vendor = "cisco_iosxe"
    await db_session.flush()

    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "show_version",
            "decision": "whitelist",
        },
    )
    await device_command_policy_crud.create(
        db_session,
        {
            "scope": "asset",
            "asset_id": asset_id,
            "command_name": "ping",
            "decision": "blacklist",
        },
    )
    await db_session.commit()

    result = await hitl_tools.list_device_commands_for_asset(
        db_session, session_id=session_id, asset_id=asset_id
    )

    assert result.control == "ok"
    assert "show_version：" in result.content
    assert "白名单（当前为请求审批，需人工批准）" in result.content
    assert "可自动执行" not in result.content
    assert "黑名单（禁止执行）" in result.content
    assert "未分类（需人工审批）" in result.content
    # 未配凭据的资产要提示先去 CMDB 配置凭据。
    assert "未配置登录凭据" in result.content

    session = await agent_session_crud.get(db_session, session_id)
    assert session is not None
    session.approval_mode = "assist"
    await db_session.commit()

    assist_result = await hitl_tools.list_device_commands_for_asset(
        db_session, session_id=session_id, asset_id=asset_id
    )
    assert assist_result.control == "ok"
    assert "白名单（可自动执行）" in assist_result.content
    assert "device_control" in assist_result.content


async def test_root_dispatcher_routes_list_device_commands(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """根调度器应把 list_device_commands 路由到工具实现，并对非法参数给可行动错误。"""
    captured: dict[str, object] = {}

    async def fake_list(db: AsyncSession, **kwargs: object) -> ToolResult:
        captured.update(kwargs)
        return ToolResult(control="ok", content="命令清单")

    monkeypatch.setattr(tool_dispatch, "list_device_commands_for_asset", fake_list)
    dispatch = build_root_tool_dispatcher(db_session, session_id=1, actor_user_id=2)

    ok_result = await dispatch("list_device_commands", {"asset_id": 9})
    assert ok_result.control == "ok"
    assert captured == {"asset_id": 9, "session_id": 1}

    bad_result = await dispatch("list_device_commands", {})
    assert bad_result.control == "clarification"
    assert "asset_id" in bad_result.content
