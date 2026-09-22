"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: executors.py
@DateTime: 2026-08-12
@Docs: HITL 动作执行器：notify 写审计日志，device_query/device_control 走 Netmiko。

实现流程：
1. ExecutionResult 是 hitl.resume 与各类执行器之间的统一返回契约（ok/message/detail）。
2. NotifyExecutor 从 payload 读取 message，校验非空后调用 log_audit(action=hitl_notify_executed)。
3. DeviceQueryExecutor 同时服务只读诊断与变更管控：按目录分派 send_command、
   send_command_timing（reboot 的确认提示）或 send_config_set（接口启停）。
4. ExecutionResult.dispatched 回答"这次失败有没有可能已经把命令发到设备上"：
   连接建立之前的任何失败都是 False（确定没下发，上层可安全回退重试），连接一旦
   建立就置 True（之后失败无法确定命令是否已生效，上层必须走 UNKNOWN 人工核实）。
   设备回了报错文本也保持 True：配置可能已部分生效，不能因为看到错误就当成没下发。
5. 失败时把真实异常堆栈写进服务端日志（logger.exception），只把异常类名放进
   detail["error_class"] 供上层展示——既能定位问题，又不把原始异常文本泄漏给模型。
6. 设备回了文本不等于成功（R3），ok=True 只给有明确成功证据的结果：
   - 配置命令把厂商报错句式交给 Netmiko 的 error_pattern 逐行检查，命中即失败；
     需要提交回显的厂商（Junos 的 commit complete）没看到回显也不算成功；
   - 普通命令只检查输出开头几行的厂商报错句式，避免把配置正文里的 error 字样误判；
   - 确认流程按目录逐轮匹配，设备问了没登记的问题就停下、不替人回答；
   - 重启发出后拿不到成功证据，返回「待人工核实」，由上层落 UNKNOWN。

为什么用 Netmiko 而不是 Scrapli：本项目要同时管思科/华三/华为/锐捷等多厂商设备，
而"关闭分页"这一步各厂商命令完全不同（华为 screen-length 0 temporary、华三
screen-length disable、锐捷 terminal width 256 + terminal length 0）。Netmiko 按
device_type 自动发对应命令，Scrapli 的社区驱动覆盖面则要窄得多——曾经因为分页没
关掉，show running-config 输出第一屏后卡在 --More--，表现为读超时。

Netmiko 是同步库，所有阻塞调用统一用 asyncio.to_thread 丢进工作线程，避免卡住事件
循环。代价是这些线程不可取消：turn 被取消时线程仍会把命令跑完，所以上层必须按
UNKNOWN（可能已下发）处理，这与既有的 HITL 状态机语义一致。
"""

import asyncio
import functools
import logging
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, cast

from netmiko import ConnectHandler
from netmiko.exceptions import ConfigInvalidException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.device_commands import (
    CONFIG_SUCCESS_MARKERS,
    DEVICE_ERROR_PATTERNS,
    UnknownDeviceCommandError,
    VendorName,
    command_supports_vendor,
    get_command_template,
    get_device_command,
    rendered_command_lines,
    validate_interface_name,
)
from app.core.cmdb_credential import decrypt_credential_password
from app.core.config import settings
from app.models.cmdb_asset import CmdbAsset
from app.utils.audit import log_audit

logger = logging.getLogger(__name__)

# 设备命令专用线程池，绝不能用 asyncio 的默认执行器。
# asyncio.to_thread 走的是默认池（容量 min(32, cpu_count+4)，8 核上只有 12），
# 而 core/security.py 的密码哈希也用 asyncio.to_thread。单条设备命令最长占用
# CONN(15s)+READ(60s)，十几个并发设备命令就能占满默认池，导致所有用户登录
# 因为排不进线程池而 503——且此时密码限流信号量是空闲的，指标上完全看不出原因。
# 物理隔离两个池，让设备命令的慢只影响设备命令。
_DEVICE_EXECUTOR = ThreadPoolExecutor(
    max_workers=settings.DEVICE_COMMAND_MAX_CONCURRENCY,
    thread_name_prefix="device-cmd",
)


def shutdown_device_executor() -> None:
    """释放设备命令线程池；由 app.main 的 lifespan 在关停时调用。

    ``wait=False`` 是刻意的：Netmiko 线程不可取消，正在跑的命令可能还要几十秒，
    关停流程不应被它阻塞。``cancel_futures=True`` 只丢弃尚未开始的排队任务。
    """
    _DEVICE_EXECUTOR.shutdown(wait=False, cancel_futures=True)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """HITL 执行器统一返回结构。

    Attributes:
        ok: 执行是否成功。
        message: 面向人的分类结论，不含原始异常文本。
        detail: 成功时含 output/truncated；失败时可含 error_class。
        dispatched: 失败时命令是否可能已下发到设备。默认 False（确定没下发），
            只有真正建立连接之后才置 True，保证默认取值永远偏保守可回退的一侧。
    """

    ok: bool
    message: str
    detail: dict[str, object] = field(default_factory=dict)
    dispatched: bool = False


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


# CMDB 厂商字段 → Netmiko device_type。device_type 决定 Netmiko 登录后发哪条
# "关闭分页"命令，标错厂商会导致大输出命令卡在分页提示符上读超时。cisco_s300 会启用
# ANSI 清洗并发送 terminal datadump；cisco_xe 则使用 IOS-XE 的会话初始化。
_NETMIKO_DEVICE_TYPES: Mapping[str, str] = {
    "cisco_iosxe": "cisco_xe",
    "cisco_small_business": "cisco_s300",
    "huawei_vrp": "huawei_vrp",
    "hp_comware": "hp_comware",
    "juniper_junos": "juniper_junos",
}


def _netmiko_device_type_for_vendor(vendor: str) -> str:
    """按 CMDB 厂商字段选择 Netmiko device_type；调用方已确认厂商有映射（见 execute）。"""
    return _NETMIKO_DEVICE_TYPES[vendor]


def _open_netmiko_connection(
    *,
    host: str,
    vendor: str,
    username: str,
    password: str,
    conn_timeout: float,
) -> Any:
    """建立一个已认证的 Netmiko 连接；同步阻塞，抽成独立函数方便测试打桩。

    ConnectHandler 构造过程里就完成了 TCP 连接、认证和 session_preparation
    （含按 device_type 关闭分页），所以它一返回就意味着"已经跟设备说过话了"。

    DEVICE_SSH_STRICT_HOST_KEY 开启时 Paramiko 改用 RejectPolicy：未登记在
    known_hosts 里的设备直接拒连，而不是静默接受任意主机密钥。这条连接会把
    特权账号明文口令发给对端，不校验主机密钥等于允许中间人直接窃取该口令。
    """
    kwargs: dict[str, Any] = {
        "device_type": _netmiko_device_type_for_vendor(vendor),
        "host": host,
        "username": username,
        "password": password,
        "conn_timeout": conn_timeout,
        "auth_timeout": conn_timeout,
        "banner_timeout": conn_timeout,
    }
    if settings.DEVICE_SSH_STRICT_HOST_KEY:
        kwargs["ssh_strict"] = True
        kwargs["system_host_keys"] = True
        if settings.DEVICE_SSH_KNOWN_HOSTS_FILE:
            kwargs["alt_host_keys"] = True
            kwargs["alt_key_file"] = settings.DEVICE_SSH_KNOWN_HOSTS_FILE
    return ConnectHandler(**kwargs)


# 普通命令的报错总是紧跟在命令回显之后：只看输出开头这么多行，
# 避免把 show running-config 正文里的 logging errors、横幅里的 % 行误判成失败。
_ERROR_SCAN_HEAD_LINES = 5
_MAX_ERROR_LINE_CHARS = 160


def _device_error_line(vendor: VendorName, output: str, *, head_only: bool) -> str | None:
    """返回设备报错的那一行；没有明确报错返回 None。"""
    pattern = DEVICE_ERROR_PATTERNS.get(vendor)
    if pattern is None:
        return None
    lines = [line for line in output.splitlines() if line.strip()]
    if head_only:
        lines = lines[:_ERROR_SCAN_HEAD_LINES]
    for line in lines:
        if re.search(pattern, line):
            return line.strip()[:_MAX_ERROR_LINE_CHARS]
    return None


def _rejected(message: str) -> ExecutionResult:
    """设备明确拒绝了命令。已连上设备，可能部分生效，所以 dispatched 仍为 True。"""
    return ExecutionResult(
        ok=False,
        message=message,
        detail={"error_class": "DeviceRejected"},
        dispatched=True,
    )


def _unconfirmed(message: str) -> ExecutionResult:
    """命令已下发，但拿不到明确的成功证据：交给上层按 UNKNOWN 人工核实。"""
    return ExecutionResult(
        ok=False,
        message=message,
        detail={"error_class": "Unconfirmed"},
        dispatched=True,
    )


def _run_confirmation_flow(
    connection: Any,
    *,
    vendor: VendorName,
    template: str,
    steps: tuple[Any, ...],
    read_timeout: float,
) -> ExecutionResult | str:
    """按目录逐轮走确认提示；成功返回完整回显，任何一步不对都返回失败结果。

    设备问了目录里没登记的问题（例如「要不要保存配置」）就停下，不替人回答——
    替人回答 y 可能悄悄存盘或丢弃改动。停下时命令还在等输入，断开后通常会取消，
    但无法百分之百确认，所以同样按「已下发、未确认」报告。
    """
    # 确认提示不是标准提示符，send_command 会一直等不到而超时；
    # send_command_timing 按「读到安静为止」返回，才能拿到提示并应答。
    chunk = connection.send_command_timing(
        template, read_timeout=read_timeout, strip_prompt=False, strip_command=False
    )
    transcript = chunk
    for step in steps:
        error_line = _device_error_line(vendor, chunk, head_only=False)
        if error_line is not None:
            return _rejected(f"设备拒绝了命令：{error_line}")
        if not re.search(step.prompt_pattern, chunk):
            return _unconfirmed(
                "未出现预期的确认提示（设备可能在问目录里没登记的问题），已停止、没有替人回答；"
                "请人工核实设备状态"
            )
        chunk = connection.send_command_timing(
            step.response, read_timeout=read_timeout, strip_prompt=False, strip_command=False
        )
        transcript += chunk
    error_line = _device_error_line(vendor, chunk, head_only=False)
    if error_line is not None:
        return _rejected(f"设备拒绝了命令：{error_line}")
    return str(transcript)


def _run_device_command(
    *,
    host: str,
    vendor: VendorName,
    username: str,
    password: str,
    command_name: str,
    definition: Any,
    interface_name: str | None,
    conn_timeout: float,
    read_timeout: float,
) -> ExecutionResult:
    """在工作线程里跑完整条 Netmiko 会话：连接 → 按类型分派 → 判定结果 → 断开。

    全程同步阻塞，由 DeviceQueryExecutor.execute 用 asyncio.to_thread 调用。
    连接建立后置 dispatched=True，之后任何异常都无法确定命令是否已生效。
    """
    connection = None
    dispatched = False
    try:
        connection = _open_netmiko_connection(
            host=host,
            vendor=vendor,
            username=username,
            password=password,
            conn_timeout=conn_timeout,
        )
        # 连接已建立：从这里开始，任何异常都无法确定命令是否已经下发到设备。
        dispatched = True

        if definition.config_templates is not None and vendor in definition.config_templates:
            rendered = list(
                rendered_command_lines(command_name, vendor, interface_name=interface_name)
            )
            if any("<" in line or ">" in line for line in rendered):
                return ExecutionResult(ok=False, message="命令模板含未解析占位符")
            try:
                # error_pattern 让 Netmiko 每发一行就检查回显，命中立即抛异常，
                # 不会在第一行就失败的情况下继续往下发。
                output = connection.send_config_set(
                    rendered,
                    read_timeout=read_timeout,
                    error_pattern=DEVICE_ERROR_PATTERNS[vendor],
                )
            except ConfigInvalidException:
                logger.warning(
                    "设备拒绝了配置命令 host=%s vendor=%s command=%s", host, vendor, command_name
                )
                return _rejected("设备拒绝了配置命令，可能已部分生效，请人工核实")
            marker = CONFIG_SUCCESS_MARKERS.get(vendor)
            if marker is not None and not re.search(marker, output):
                return _unconfirmed("未看到设备确认配置已提交，配置可能未生效或部分生效，请人工核实")
        elif definition.confirmation is not None and vendor in definition.confirmation:
            flow = _run_confirmation_flow(
                connection,
                vendor=vendor,
                template=definition.templates[vendor],
                steps=definition.confirmation[vendor],
                read_timeout=read_timeout,
            )
            if isinstance(flow, ExecutionResult):
                return flow
            output = flow
        else:
            template = get_command_template(command_name, vendor)
            if "<" in template or ">" in template:
                return ExecutionResult(ok=False, message="命令模板含未解析占位符")
            output = connection.send_command(template, read_timeout=read_timeout)
            error_line = _device_error_line(vendor, output, head_only=True)
            if error_line is not None:
                return _rejected(f"设备拒绝了命令：{error_line}")
    except Exception as exc:
        # 真实堆栈只进服务端日志：既能定位平台/认证/分页类故障，又不外泄异常文本。
        logger.exception(
            "设备命令执行失败 host=%s vendor=%s command=%s dispatched=%s",
            host,
            vendor,
            command_name,
            dispatched,
        )
        message = (
            "连接或执行命令失败；如果是重启类命令，设备可能已经生效，请人工核实"
            if dispatched
            else "无法建立设备连接，命令未下发"
        )
        return ExecutionResult(
            ok=False,
            message=message,
            detail={"error_class": type(exc).__name__},
            dispatched=dispatched,
        )
    finally:
        if connection is not None:
            try:
                connection.disconnect()
            except Exception:
                pass

    if definition.verify_manually:
        # 重启发出后设备就断开了：断线或没有报错都不是成功证据。
        return _unconfirmed(
            "命令已发送，设备正在重启；这期间无法自动确认结果，请在设备恢复后人工核实"
        )
    return ExecutionResult(
        ok=True,
        message="命令执行完成",
        detail={"output": str(output), "truncated": False},
        dispatched=True,
    )


class DeviceQueryExecutor:
    """设备诊断/管控命令执行器：解析凭据、按厂商选真实命令、跑 Netmiko、返回完整输出。"""

    async def execute(
        self,
        db: AsyncSession,
        *,
        asset: CmdbAsset,
        command_name: str,
        dynamic_password: str | None,
        interface_name: str | None = None,
    ) -> ExecutionResult:
        """执行一次设备命令并返回安全结果。

        Args:
            db: 当前事务的数据库会话（目前未使用，保留是为了跟其它执行器
                签名一致，也方便未来加执行前后的额外落库操作）。
            asset: 目标 CMDB 资产，须已配置 vendor 与凭据。
            command_name: 目录里的命令名，调用方保证已通过白名单/校验。
            dynamic_password: 动态凭据时的一次性明文密码；静态凭据时忽略。
            interface_name: port_enable/port_disable 等需要接口名的命令参数。

        Returns:
            ok=True 时 detail 含 output/truncated；ok=False 时 message 只给
            分类信息，不透传任何原始异常文本或设备侧细节。
        """
        if asset.credential_type == "static":
            if not asset.credential_password_encrypted:
                return ExecutionResult(ok=False, message="资产未配置静态密码")
            password = decrypt_credential_password(asset.credential_password_encrypted)
        elif asset.credential_type == "dynamic":
            if not dynamic_password:
                return ExecutionResult(ok=False, message="动态凭据缺少本次输入的密码")
            password = dynamic_password
        else:
            return ExecutionResult(ok=False, message="资产未配置登录凭据")

        try:
            definition = get_device_command(command_name)
        except UnknownDeviceCommandError:
            return ExecutionResult(ok=False, message="未知命令名")

        if definition.requires_argument == "interface_name":
            if not interface_name or not validate_interface_name(interface_name):
                return ExecutionResult(ok=False, message="接口名参数无效")
        elif interface_name is not None:
            return ExecutionResult(ok=False, message="该命令不接受接口名参数")

        if not command_supports_vendor(command_name, asset.vendor):
            return ExecutionResult(ok=False, message="该设备厂商不支持这个命令")
        if asset.vendor not in _NETMIKO_DEVICE_TYPES:
            # 目录登记了模板却漏了平台映射：宁可不连，也不按 generic 硬连——generic 不关分页，
            # 大输出会卡在分页提示符上读超时，连上之后的失败又只能落 UNKNOWN 等人工核实。
            return ExecutionResult(ok=False, message="该厂商没有对应的 Netmiko 平台，命令未下发")

        vendor = cast(VendorName, asset.vendor)
        # Netmiko 全同步，丢到工作线程避免阻塞事件循环。注意线程不可取消：
        # 调用方取消时命令仍会跑完，因此失败一律按 dispatched 语义交给上层判定。
        # 用专用池而非 asyncio.to_thread：见 _DEVICE_EXECUTOR 的说明。
        # run_in_executor 不接受关键字参数，所以用 partial 绑定。
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _DEVICE_EXECUTOR,
            functools.partial(
                _run_device_command,
                host=asset.ip_address,
                vendor=vendor,
                username=asset.credential_username,
                password=password,
                command_name=command_name,
                definition=definition,
                interface_name=interface_name,
                conn_timeout=settings.DEVICE_COMMAND_CONN_TIMEOUT_SECONDS,
                read_timeout=settings.DEVICE_COMMAND_READ_TIMEOUT_SECONDS,
            ),
        )
