"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_executors.py
@DateTime: 2026-08-12
@Docs: T10 HITL 执行器单元测试（notify + DeviceQueryExecutor 管控分支）。
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from netmiko.exceptions import ConfigInvalidException
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import executors
from app.agent.device_commands import get_device_command
from app.agent.executors import DeviceQueryExecutor, NotifyExecutor
from app.core.cmdb_credential import encrypt_credential_password
from app.core.config import settings
from app.crud.cmdb_asset import cmdb_asset_crud
from app.models.audit_log import AuditLog
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _make_asset(
    db_session: AsyncSession,
    *,
    credential_type: str = "static",
    credential_username: str = "admin",
    credential_password_encrypted: str | None = "placeholder",
    vendor: str = "cisco_iosxe",
) -> object:
    """创建带厂商与凭据的交换机资产，供管控命令执行测试使用。"""
    created = await cmdb_asset_crud.create(
        db_session,
        {
            "asset_type": "switch",
            "hostname": "sw-exec-ctrl",
            "ip_address": "10.0.0.97",
            "vendor": vendor,
            "credential_type": credential_type,
            "credential_username": credential_username,
            "credential_password_encrypted": credential_password_encrypted,
        },
    )
    await db_session.flush()
    return created


def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


async def test_run_device_command_returns_full_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设备查询执行器必须原样返回输出，不能在进入 HITL 收尾前永久截断。"""
    output = "A" * 5000
    connection = MagicMock()
    connection.send_command.return_value = output
    monkeypatch.setattr(executors, "_open_netmiko_connection", lambda **_: connection)

    result = executors._run_device_command(
        host="10.11.210.67",
        vendor="hp_comware",
        username="admin",
        password="one-use-password",
        command_name="show_running_config",
        definition=get_device_command("show_running_config"),
        interface_name=None,
        conn_timeout=5,
        read_timeout=30,
    )

    assert result.ok is True
    assert result.detail["output"] == output
    assert result.detail["truncated"] is False


async def test_device_query_executor_reboot_sends_timing_confirmation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reboot 的确认提示不是标准提示符，必须走 send_command_timing。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="cisco_iosxe",
    )
    executor = DeviceQueryExecutor()
    fake_connection = MagicMock()
    fake_connection.send_command_timing = MagicMock(
        side_effect=["Proceed with reload? [confirm]", ""]
    )
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        result = await executor.execute(
            db_session, asset=asset, command_name="reboot", dynamic_password=None
        )
    # 第一次发命令拿到确认提示，第二次发应答内容。
    assert fake_connection.send_command_timing.call_count == 2
    fake_connection.send_command.assert_not_called()
    # 确认发出后设备去重启了，拿不到成功证据：如实交给人工核实（R3），不报成功。
    assert result.ok is False
    assert result.dispatched is True
    assert "人工核实" in result.message


async def test_device_query_executor_connection_drop_during_reboot_is_conservative_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2：连接在重启命令执行中断开，不得伪造成功，必须提示人工核实。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="cisco_iosxe",
    )
    executor = DeviceQueryExecutor()
    fake_connection = MagicMock()
    fake_connection.send_command_timing = MagicMock(side_effect=ConnectionError("closed"))
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        result = await executor.execute(
            db_session, asset=asset, command_name="reboot", dynamic_password=None
        )
    assert result.ok is False
    assert "人工核实" in result.message
    assert result.dispatched is True


async def test_device_query_executor_port_disable_uses_send_config_set_with_interface(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """port_disable 走 send_config_set，接口名要正确代入模板。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="cisco_iosxe",
    )
    executor = DeviceQueryExecutor()
    fake_connection = MagicMock()
    fake_connection.send_config_set = MagicMock(return_value="ok")
    with patch("app.agent.executors._open_netmiko_connection", return_value=fake_connection):
        result = await executor.execute(
            db_session,
            asset=asset,
            command_name="port_disable",
            dynamic_password=None,
            interface_name="GigabitEthernet0/1",
        )
    assert result.ok is True
    sent_lines = fake_connection.send_config_set.call_args.args[0]
    assert sent_lines == ["interface GigabitEthernet0/1", "shutdown"]


async def test_device_query_executor_rejects_invalid_interface_name_before_connecting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法接口名必须在建立设备连接之前就拒绝，不能把它当命令片段发出去。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="cisco_iosxe",
    )
    executor = DeviceQueryExecutor()
    with patch("app.agent.executors._open_netmiko_connection") as mock_connect:
        result = await executor.execute(
            db_session,
            asset=asset,
            command_name="port_disable",
            dynamic_password=None,
            interface_name="eth0; reload",
        )
    assert result.ok is False
    mock_connect.assert_not_called()


async def test_device_query_executor_rejects_unsupported_vendor_before_connecting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """厂商不支持这条命令时，同样要在连接设备之前就失败（不是连接后才发现）。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="cisco_iosxe",
    )
    asset.vendor = "hp_comware"
    executor = DeviceQueryExecutor()
    with patch("app.agent.executors._open_netmiko_connection") as mock_connect:
        result = await executor.execute(
            db_session,
            asset=asset,
            command_name="port_disable",
            dynamic_password=None,
            interface_name="GigabitEthernet0/1",
        )
    assert result.ok is False
    assert result.message == "该设备厂商不支持这个命令"
    mock_connect.assert_not_called()


async def test_notify_executor_writes_audit_and_succeeds(
    db_session: AsyncSession,
    test_user: User,
) -> None:
    executor = NotifyExecutor()
    proposal_id = 42

    result = await executor.execute(
        db_session,
        proposal_id=proposal_id,
        payload={"message": "SW-12 离线"},
        actor_user_id=test_user.id,
    )
    await db_session.flush()

    assert result.ok is True
    logs = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "hitl_notify_executed")
        )
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].user_id == test_user.id
    assert logs[0].target == f"hitl_proposal:{proposal_id}"
    assert "SW-12 离线" in logs[0].detail


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": ""},
        {"message": "   "},
    ],
)
async def test_notify_executor_rejects_blank_message(
    db_session: AsyncSession,
    test_user: User,
    payload: dict[str, str],
) -> None:
    executor = NotifyExecutor()

    result = await executor.execute(
        db_session,
        proposal_id=1,
        payload=payload,
        actor_user_id=test_user.id,
    )
    await db_session.flush()

    assert result.ok is False
    logs = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "hitl_notify_executed")
        )
    ).scalars().all()
    assert logs == []


# ---------------------------------------------------------------------------
# R3：设备回了文本不等于成功。按厂商识别明确的错误、确认流程必须走完，
# 重启/关机拿不到成功证据时如实交给人工核实，而不是标成已执行。
# ---------------------------------------------------------------------------


class _FakeConfigConnection:
    """按 Netmiko 4.x 的行为模拟 send_config_set：传了 error_pattern 且命中就抛异常。"""

    def __init__(self, output: str) -> None:
        self.output = output
        self.error_pattern: str = ""

    def send_config_set(
        self, lines: list[str], *, read_timeout: float, error_pattern: str = "", **_: object
    ) -> str:
        self.error_pattern = error_pattern
        if error_pattern and re.search(error_pattern, self.output, flags=re.M):
            raise ConfigInvalidException(f"Invalid input detected at command: {lines[-1]}")
        return self.output

    def disconnect(self) -> None:
        return None


class _FakeTimingConnection:
    """按顺序回放 send_command_timing 的输出，并记录每次发了什么。"""

    def __init__(self, *outputs: str) -> None:
        self._outputs = list(outputs)
        self.sent: list[str] = []

    def send_command_timing(self, command: str, **_: object) -> str:
        self.sent.append(command)
        return self._outputs.pop(0) if self._outputs else ""

    def disconnect(self) -> None:
        return None


def _run(
    monkeypatch: pytest.MonkeyPatch,
    connection: object,
    *,
    vendor: str,
    command_name: str,
    interface_name: str | None = None,
) -> executors.ExecutionResult:
    monkeypatch.setattr(executors, "_open_netmiko_connection", lambda **_: connection)
    return executors._run_device_command(
        host="10.0.0.1",
        vendor=vendor,  # type: ignore[arg-type]
        username="admin",
        password="one-use-password",
        command_name=command_name,
        definition=get_device_command(command_name),
        interface_name=interface_name,
        conn_timeout=5,
        read_timeout=30,
    )


@pytest.mark.parametrize(
    ("vendor", "output"),
    [
        ("cisco_iosxe", "SW(config-if)#shutdown\n% Invalid input detected at '^' marker.\n"),
        (
            "huawei_vrp",
            "[SW-GigabitEthernet0/0/1]shutdown\nError: Unrecognized command found at '^' position.\n",
        ),
        ("juniper_junos", "set interfaces ge-0/0/1 disable\nsyntax error.\n"),
    ],
)
async def test_config_command_rejected_by_device_is_not_success(
    monkeypatch: pytest.MonkeyPatch, vendor: str, output: str
) -> None:
    """审查报告 R3 的复现之一：port_disable 被设备拒绝，不能得到 ok=True。
    已经连上设备并开始下发，可能部分生效，所以 dispatched 保持 True（交给 UNKNOWN 人工核实）。"""
    connection = _FakeConfigConnection(output)

    result = _run(
        monkeypatch, connection, vendor=vendor, command_name="port_disable",
        interface_name="GigabitEthernet0/0/1",
    )

    assert result.ok is False
    assert result.dispatched is True
    assert "设备拒绝" in result.message
    assert connection.error_pattern


async def test_config_command_without_device_error_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConfigConnection("SW(config)#interface Gi0/1\nSW(config-if)#shutdown\n")

    result = _run(
        monkeypatch, connection, vendor="cisco_iosxe", command_name="port_disable",
        interface_name="GigabitEthernet0/1",
    )

    assert result.ok is True
    assert connection.error_pattern


async def test_junos_config_needs_commit_complete_as_success_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Junos 的配置只有 commit 成功才生效：没看到 commit complete 就不能算成功。"""
    no_commit = _run(
        monkeypatch,
        _FakeConfigConnection("set interfaces ge-0/0/1 disable\ncommit\n"),
        vendor="juniper_junos",
        command_name="port_disable",
        interface_name="ge-0/0/1",
    )
    committed = _run(
        monkeypatch,
        _FakeConfigConnection("set interfaces ge-0/0/1 disable\ncommit\ncommit complete\n"),
        vendor="juniper_junos",
        command_name="port_disable",
        interface_name="ge-0/0/1",
    )

    assert no_commit.ok is False
    assert no_commit.dispatched is True
    assert committed.ok is True


async def test_reboot_rejected_by_device_never_sends_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审查报告 R3 的复现之二：reboot 回 % Authorization failed，
    原实现只发一次命令、没发确认应答，仍判成功。"""
    connection = _FakeTimingConnection("SW#reload\n% Authorization failed.\n")

    result = _run(monkeypatch, connection, vendor="cisco_iosxe", command_name="reboot")

    assert result.ok is False
    assert result.dispatched is True
    assert "设备拒绝" in result.message
    assert connection.sent == ["reload"]


async def test_reboot_with_unregistered_prompt_stops_without_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设备先问「要不要保存配置」这类目录里没登记的问题：停下，不替人回答，报告不确定。"""
    connection = _FakeTimingConnection(
        "SW#reload\nSystem configuration has been modified. Save? [yes/no]: "
    )

    result = _run(monkeypatch, connection, vendor="cisco_iosxe", command_name="reboot")

    assert result.ok is False
    assert result.dispatched is True
    assert "未出现预期的确认提示" in result.message
    assert connection.sent == ["reload"]


async def test_reboot_confirmation_sent_still_needs_manual_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认应答发出去之后设备就去重启了：断线/没回显都不是成功证据，如实交给人工核实。"""
    connection = _FakeTimingConnection("SW#reload\nProceed with reload? [confirm]", "")

    result = _run(monkeypatch, connection, vendor="cisco_iosxe", command_name="reboot")

    assert result.ok is False
    assert result.dispatched is True
    assert "人工核实" in result.message
    assert connection.sent == ["reload", "\n"]


async def test_read_only_command_error_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    connection.send_command.return_value = (
        "show interfaces status\n% Invalid input detected at '^' marker.\n"
    )

    result = _run(monkeypatch, connection, vendor="cisco_iosxe", command_name="show_interfaces")

    assert result.ok is False
    assert result.dispatched is True
    assert "设备拒绝" in result.message


async def test_error_words_deep_inside_normal_output_are_not_false_positives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不能用宽泛的 error 正则扫整段输出：配置正文里本来就有 logging errors、
    横幅里的 % 行等正常内容。只看输出开头的几行、只认具体的厂商报错句式。"""
    config_lines = [f" interface Vlan{index}" for index in range(200)]
    config_lines[120] = " logging trap errors"
    config_lines[150] = "% Error: this is just banner text in the config"
    output = "display current-configuration\n#\n version 7.1\n" + "\n".join(config_lines)
    connection = MagicMock()
    connection.send_command.return_value = output

    result = _run(
        monkeypatch, connection, vendor="hp_comware", command_name="show_running_config"
    )

    assert result.ok is True
    assert result.detail["output"] == output


async def test_linux_sudo_refusal_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    connection.send_command.return_value = (
        "sudo: a terminal is required to read the password; "
        "either use the -S option to read from standard input\n"
    )

    result = _run(monkeypatch, connection, vendor="linux", command_name="reboot")

    assert result.ok is False
    assert result.dispatched is True
    assert "设备拒绝" in result.message
