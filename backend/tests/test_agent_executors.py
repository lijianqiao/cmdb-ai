"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: test_agent_executors.py
@DateTime: 2026-08-12
@Docs: T10 HITL 执行器单元测试（notify + DeviceQueryExecutor 管控分支）。
"""

import inspect
import re
from unittest.mock import MagicMock, patch

import pytest
from netmiko.base_connection import BaseConnection
from netmiko.exceptions import ConfigInvalidException
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import executors
from app.agent.device_commands import get_device_command, list_device_commands
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
        arguments=None,
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
    """port_disable 走 send_config_set，每个接口名都要正确代入模板。"""
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
            arguments={"interface_names": ["GigabitEthernet0/1", "GigabitEthernet0/2"]},
        )
    assert result.ok is True
    sent_batches = [call.args[0] for call in fake_connection.send_config_set.call_args_list]
    assert sent_batches == [
        ["interface GigabitEthernet0/1", "shutdown"],
        ["interface GigabitEthernet0/2", "shutdown"],
    ]


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
            arguments={"interface_names": ["GigabitEthernet0/1", "eth0; reload"]},
        )
    assert result.ok is False
    mock_connect.assert_not_called()


@pytest.mark.parametrize(
    ("command_name", "arguments"),
    [
        ("port_disable", None),  # 端口命令缺接口
        ("reboot", {"interface_names": ["GigabitEthernet0/1"]}),  # 重启不接受接口
    ],
)
async def test_device_query_executor_checks_interface_argument_before_connecting(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    arguments: dict[str, object] | None,
) -> None:
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(db_session, credential_password_encrypted=ciphertext)
    executor = DeviceQueryExecutor()
    with patch("app.agent.executors._open_netmiko_connection") as mock_connect:
        result = await executor.execute(
            db_session,
            asset=asset,
            command_name=command_name,
            dynamic_password=None,
            arguments=arguments,  # type: ignore[arg-type]
        )
    assert result.ok is False
    assert result.dispatched is False
    mock_connect.assert_not_called()


async def test_device_query_executor_rejects_unsupported_vendor_before_connecting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """other 厂商没有登记任何模板，要在连接之前就失败。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="other",
    )
    executor = DeviceQueryExecutor()
    with patch("app.agent.executors._open_netmiko_connection") as mock_connect:
        result = await executor.execute(
            db_session,
            asset=asset,
            command_name="show_version",
            dynamic_password=None,
        )
    assert result.ok is False
    assert result.message == "该设备厂商不支持这个命令"
    mock_connect.assert_not_called()


async def test_read_only_command_renders_its_interface_argument(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    single_interface_read_only_command: str,
) -> None:
    """exec 路径也要走渲染：带 {interface} 的模板不能原样发到设备上。"""
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(db_session, credential_password_encrypted=ciphertext)
    connection = MagicMock()
    connection.send_command.return_value = "GigabitEthernet1/0/15 is up"
    executor = DeviceQueryExecutor()

    with patch("app.agent.executors._open_netmiko_connection", return_value=connection):
        result = await executor.execute(
            db_session,
            asset=asset,
            command_name=single_interface_read_only_command,
            dynamic_password=None,
            arguments={"interface_name": "GigabitEthernet1/0/15"},
        )

    assert result.ok is True
    assert connection.send_command.call_args.args[0] == "show interfaces GigabitEthernet1/0/15"


async def test_device_query_executor_refuses_vendor_without_netmiko_platform(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录给厂商登记了模板、却漏了 Netmiko 平台映射：宁可不连，也不按 generic 硬连。

    generic 平台不关分页，大输出会卡在分页提示符上读超时；连上之后失败又只能落
    UNKNOWN 等人工核实。一条命令都没发，所以 dispatched 必须是 False。
    """
    monkeypatch.setattr(settings, "CMDB_CREDENTIAL_KEY", SecretStr(_generate_fernet_key()))
    ciphertext = encrypt_credential_password("whatever")
    asset = await _make_asset(
        db_session,
        credential_password_encrypted=ciphertext,
        vendor="hp_comware",
    )
    without_comware = {
        vendor: device_type
        for vendor, device_type in executors._NETMIKO_DEVICE_TYPES.items()
        if vendor != "hp_comware"
    }
    monkeypatch.setattr(executors, "_NETMIKO_DEVICE_TYPES", without_comware)
    executor = DeviceQueryExecutor()
    with patch("app.agent.executors._open_netmiko_connection") as mock_connect:
        result = await executor.execute(
            db_session,
            asset=asset,
            command_name="show_version",
            dynamic_password=None,
        )
    assert result.ok is False
    assert result.dispatched is False
    assert "Netmiko" in result.message
    mock_connect.assert_not_called()


async def test_every_catalog_vendor_has_a_netmiko_platform() -> None:
    """目录里登记了模板的厂商都要有 Netmiko 平台，漏了就在测试阶段暴露，而不是上线后连不上。"""
    catalog_vendors = {
        vendor
        for item in list_device_commands()
        for vendor in (*item.templates, *(item.config_templates or {}))
    }
    assert catalog_vendors <= set(executors._NETMIKO_DEVICE_TYPES)


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
# 重启拿不到成功证据时如实交给人工核实，而不是标成已执行。
# ---------------------------------------------------------------------------


class _FakeConfigConnection:
    """按 Netmiko 4.7.0 的行为模拟一次配置会话：先进配置模式，再逐口 send_config_set。

    responses 按这一批的第一行取回显，没登记的用 default；传了 error_pattern 且
    回显命中就抛 ConfigInvalidException，消息格式照抄 Netmiko。drop_at 模拟发到
    某一批时连接断开。
    """

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        *,
        default: str = "",
        config_mode_error: Exception | None = None,
        drop_at: str | None = None,
    ) -> None:
        self.responses = responses or {}
        self.default = default
        self.config_mode_error = config_mode_error
        self.drop_at = drop_at
        self.config_mode_commands: list[str] = []
        self.batches: list[list[str]] = []
        self.mode_flags: list[tuple[bool, bool]] = []
        self.error_patterns: list[str] = []
        self.exited = False

    def config_mode(self, config_command: str = "", **_: object) -> str:
        self.config_mode_commands.append(config_command)
        if self.config_mode_error is not None:
            raise self.config_mode_error
        return ""

    def send_config_set(
        self,
        lines: list[str],
        *,
        read_timeout: float,
        error_pattern: str = "",
        enter_config_mode: bool = True,
        exit_config_mode: bool = True,
        **_: object,
    ) -> str:
        self.batches.append(list(lines))
        self.mode_flags.append((enter_config_mode, exit_config_mode))
        self.error_patterns.append(error_pattern)
        if lines[0] == self.drop_at:
            raise OSError("Socket is closed")
        output = self.responses.get(lines[0], self.default)
        if error_pattern:
            match = re.search(error_pattern, output, flags=re.M)
            if match:
                raise ConfigInvalidException(
                    f"Invalid input detected at command: {lines[-1]}, "
                    f"matched error: {match.group(0)}"
                )
        return output

    def exit_config_mode(self, **_: object) -> str:
        self.exited = True
        return ""

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
    interface_names: tuple[str, ...] | None = None,
) -> executors.ExecutionResult:
    monkeypatch.setattr(executors, "_open_netmiko_connection", lambda **_: connection)
    return executors._run_device_command(
        host="10.0.0.1",
        vendor=vendor,  # type: ignore[arg-type]
        username="admin",
        password="one-use-password",
        command_name=command_name,
        definition=get_device_command(command_name),
        arguments={"interface_names": list(interface_names)} if interface_names else None,
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
    connection = _FakeConfigConnection(default=output)

    result = _run(
        monkeypatch, connection, vendor=vendor, command_name="port_disable",
        interface_names=("GigabitEthernet0/0/1",),
    )

    assert result.ok is False
    assert result.dispatched is True
    assert "设备拒绝" in result.message
    assert all(connection.error_patterns)


async def test_config_command_without_device_error_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConfigConnection(
        default="SW(config)#interface Gi0/1\nSW(config-if)#shutdown\n"
    )

    result = _run(
        monkeypatch, connection, vendor="cisco_iosxe", command_name="port_disable",
        interface_names=("GigabitEthernet0/1",),
    )

    assert result.ok is True
    assert all(connection.error_patterns)
    # 配置模式由执行器自己进、自己退：每一批都不能再让 Netmiko 进出一次。
    assert connection.config_mode_commands == [""]
    assert connection.mode_flags == [(False, False)]
    assert connection.exited is True


async def test_junos_config_needs_commit_complete_as_success_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Junos 的配置只有 commit 成功才生效：没看到 commit complete 就不能算成功。"""
    no_commit = _run(
        monkeypatch,
        _FakeConfigConnection({"commit": "commit\n"}),
        vendor="juniper_junos",
        command_name="port_disable",
        interface_names=("ge-0/0/1",),
    )
    committed = _run(
        monkeypatch,
        _FakeConfigConnection({"commit": "commit\ncommit complete\n"}),
        vendor="juniper_junos",
        command_name="port_disable",
        interface_names=("ge-0/0/1",),
    )

    assert no_commit.ok is False
    assert no_commit.dispatched is True
    assert committed.ok is True


# ---------------------------------------------------------------------------
# P1：一条提案一组接口。每个口单独一批 send_config_set（同一条连接、同一次配置
# 模式）：Cisco/华为/H3C 每个口的第二行都是一样的 shutdown，只看 Netmiko 报出的
# 出错命令分不清是哪个口；逐口发送时出错在哪一批就是哪个口。
# ---------------------------------------------------------------------------


async def test_junos_uses_private_configuration_and_commits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Junos 进私有候选配置：中途失败时改动随会话丢弃，不会留在所有人共享的候选库里。"""
    connection = _FakeConfigConnection({"commit": "commit complete\n"})

    result = _run(
        monkeypatch,
        connection,
        vendor="juniper_junos",
        command_name="port_disable",
        interface_names=("ge-0/0/1", "ge-0/0/2", "ge-0/0/3"),
    )

    assert result.ok is True
    assert connection.config_mode_commands == ["configure private"]
    assert connection.batches == [
        ["set interfaces ge-0/0/1 disable"],
        ["set interfaces ge-0/0/2 disable"],
        ["set interfaces ge-0/0/3 disable"],
        ["commit"],
    ]


async def test_batch_partial_failure_names_each_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二个口被设备拒绝：第一个口已下发，第三个口一条都没发，结论要逐口说清。"""
    connection = _FakeConfigConnection(
        {"interface Gi1/0/16": "SW(config)#interface Gi1/0/16\n% Invalid input detected at '^' marker.\n"}
    )

    result = _run(
        monkeypatch,
        connection,
        vendor="cisco_iosxe",
        command_name="port_disable",
        interface_names=("Gi1/0/15", "Gi1/0/16", "Gi1/0/17"),
    )

    assert result.ok is False
    assert result.dispatched is True
    assert result.detail["applied_interfaces"] == ["Gi1/0/15"]
    assert result.detail["failed_interface"] == "Gi1/0/16"
    assert result.detail["not_sent_interfaces"] == ["Gi1/0/17"]
    assert len(connection.batches) == 2
    assert "Gi1/0/15" in result.message
    assert "Gi1/0/16 被设备拒绝" in result.message
    assert "% Invalid input detected at '^' marker." in result.message
    assert "Gi1/0/17" in result.message


async def test_junos_failure_before_commit_applies_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Junos 要 commit 才生效：提交前就出错，这一批一个口都没改，也不会发 commit。"""
    connection = _FakeConfigConnection(
        {"set interfaces ge-0/0/2 disable": "set interfaces ge-0/0/2 disable\nsyntax error.\n"}
    )

    result = _run(
        monkeypatch,
        connection,
        vendor="juniper_junos",
        command_name="port_disable",
        interface_names=("ge-0/0/1", "ge-0/0/2", "ge-0/0/3"),
    )

    assert result.ok is False
    assert result.dispatched is True
    assert result.detail["applied_interfaces"] == []
    assert result.detail["failed_interface"] == "ge-0/0/2"
    assert ["commit"] not in connection.batches
    assert "没有提交" in result.message


async def test_entering_configuration_mode_failure_is_not_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进不了配置模式（例如 Junos 共享候选库里有别人未提交的改动）时一条配置都没发，可以直接重试。"""
    connection = _FakeConfigConnection(
        config_mode_error=ValueError("Failed to enter configuration mode.")
    )

    result = _run(
        monkeypatch,
        connection,
        vendor="juniper_junos",
        command_name="port_disable",
        interface_names=("ge-0/0/1",),
    )

    assert result.ok is False
    assert result.dispatched is False
    assert connection.batches == []


async def test_connection_lost_mid_batch_reports_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发到第二个口时连接断了：第二个口状态不明，第三个口没发，都要写进结论。"""
    connection = _FakeConfigConnection(drop_at="interface Gi1/0/16")

    result = _run(
        monkeypatch,
        connection,
        vendor="cisco_iosxe",
        command_name="port_disable",
        interface_names=("Gi1/0/15", "Gi1/0/16", "Gi1/0/17"),
    )

    assert result.ok is False
    assert result.dispatched is True
    assert result.detail["applied_interfaces"] == ["Gi1/0/15"]
    assert result.detail["failed_interface"] == "Gi1/0/16"
    assert result.detail["not_sent_interfaces"] == ["Gi1/0/17"]
    assert "状态不明" in result.message


async def test_netmiko_config_error_message_format_is_pinned() -> None:
    """设备回的报错行是从 Netmiko 的异常消息里取的：它改了措辞，这里先失败提醒同步。"""
    source = inspect.getsource(BaseConnection.send_config_set)
    assert (
        'f"Invalid input detected at command: {cmd}, matched error: {error_msg}"' in source
    )
    error = ConfigInvalidException(
        "Invalid input detected at command: shutdown, "
        "matched error: % Invalid input detected at '^' marker."
    )
    assert executors._config_error_line(error) == "% Invalid input detected at '^' marker."
    assert executors._config_error_line(ConfigInvalidException("changed wording")) is None


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
