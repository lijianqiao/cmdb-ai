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
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, cast

from netmiko import ConnectHandler
from netmiko.exceptions import ConfigInvalidException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.device_commands import (
    CONFIG_COMMIT_COMMANDS,
    CONFIG_MODE_COMMANDS,
    CONFIG_SUCCESS_MARKERS,
    DEVICE_ERROR_PATTERNS,
    SAVE_BEFORE_REBOOT_QUESTION,
    CommandArguments,
    UnknownDeviceCommandError,
    VendorName,
    command_supports_vendor,
    config_command_blocks,
    filter_exact_ip_lines,
    get_device_command,
    normalize_command_arguments,
    rendered_command_lines,
    truncate_output_lines,
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


def device_read_timeout_seconds(command_name: str) -> float:
    """按命令登记的读超时档位返回秒数。

    执行器用它决定单条命令等多久；hitl_execution 的「等别人跑完」窗口也按同一个值取，
    否则一条长命令还在跑，另一个调用方已经判定等待超时了。
    未知命令名按短档：未知命令会在执行前被拒，这里不该顺便放宽超时。
    """
    try:
        definition = get_device_command(command_name)
    except UnknownDeviceCommandError:
        return settings.DEVICE_COMMAND_READ_TIMEOUT_SECONDS
    if definition.read_timeout_class == "long":
        return settings.DEVICE_COMMAND_LONG_READ_TIMEOUT_SECONDS
    return settings.DEVICE_COMMAND_READ_TIMEOUT_SECONDS


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


# Netmiko 4.7.0 的 ConfigInvalidException 消息格式（base_connection.send_config_set）：
# "Invalid input detected at command: {cmd}, matched error: {error_msg}"。
# 只用来取设备回的那一行做展示；哪个接口失败靠「逐口一批」确定，不依赖这段文案。
_MATCHED_ERROR_MARKER = ", matched error: "


def _config_error_line(exc: ConfigInvalidException) -> str | None:
    """取出设备回的报错行；Netmiko 改了措辞就返回 None，只说被拒绝、不猜原因。"""
    _, marker, tail = str(exc).partition(_MATCHED_ERROR_MARKER)
    if not marker:
        return None
    return tail.strip()[:_MAX_ERROR_LINE_CHARS] or None


def _whole_line_error_pattern(vendor: VendorName) -> str:
    """交给 Netmiko 的报错正则：在目录正则后接 .*$，异常消息里就能带上整行报错。"""
    return f"(?:{DEVICE_ERROR_PATTERNS[vendor]}).*$"


def _batch_failure(
    message: str,
    *,
    error_class: str,
    applied: Sequence[str],
    failed: str,
    not_sent: Sequence[str],
) -> ExecutionResult:
    """一批接口中途失败：逐口记清楚谁已下发、谁失败、谁没发，交给上层按 UNKNOWN 处置。"""
    return ExecutionResult(
        ok=False,
        message=message,
        detail={
            "error_class": error_class,
            "applied_interfaces": list(applied),
            "failed_interface": failed,
            "not_sent_interfaces": list(not_sent),
        },
        dispatched=True,
    )


def _batch_failure_message(
    *,
    applied: Sequence[str],
    failed: str,
    not_sent: Sequence[str],
    failed_reason: str,
    needs_commit: bool,
) -> str:
    """把逐口结果拼成一句人话，直接进审批卡片、模型工具结果和对话结论。"""
    if needs_commit:
        # Junos 要 commit 才生效：提交之前失败，这一批一个口都没落到设备上。
        return f"{failed} {failed_reason}；配置没有提交，这一批接口都没有生效"
    parts: list[str] = []
    if applied:
        parts.append(f"已下发：{'、'.join(applied)}")
    parts.append(f"{failed} {failed_reason}")
    if not_sent:
        parts.append(f"未下发：{'、'.join(not_sent)}")
    return "；".join(parts)


def _run_config_batch(
    connection: Any,
    *,
    vendor: VendorName,
    command_name: str,
    interface_names: Sequence[str],
    read_timeout: float,
) -> ExecutionResult | str:
    """在已经进入配置模式的连接上逐口下发；成功返回完整回显，失败返回逐口结果。

    每个接口单独一批 send_config_set（同一条连接、同一次配置模式）：Cisco/华为/H3C
    每个口的第二行都是一样的 shutdown，只看 Netmiko 报出的出错命令分不清是哪个口；
    逐口发送时出错在哪一批就是哪个口。Junos 的 commit 整批只发一次。
    """
    blocks = config_command_blocks(command_name, vendor, interface_names)
    if any("<" in line or ">" in line for _, lines in blocks for line in lines):
        return ExecutionResult(ok=False, message="命令模板含未解析占位符", dispatched=True)

    commit_lines = CONFIG_COMMIT_COMMANDS.get(vendor, ())
    error_pattern = _whole_line_error_pattern(vendor)
    applied: list[str] = []
    outputs: list[str] = []

    for index, (interface_name, lines) in enumerate(blocks):
        not_sent = [name for name, _ in blocks[index + 1 :]]
        try:
            outputs.append(
                str(
                    connection.send_config_set(
                        list(lines),
                        read_timeout=read_timeout,
                        error_pattern=error_pattern,
                        enter_config_mode=False,
                        exit_config_mode=False,
                    )
                )
            )
        except ConfigInvalidException as exc:
            logger.warning(
                "设备拒绝了配置命令 vendor=%s command=%s interface=%s",
                vendor,
                command_name,
                interface_name,
            )
            error_line = _config_error_line(exc)
            reason = f"被设备拒绝（{error_line}）" if error_line else "被设备拒绝"
            return _batch_failure(
                _batch_failure_message(
                    applied=applied,
                    failed=interface_name,
                    not_sent=not_sent,
                    failed_reason=reason,
                    needs_commit=bool(commit_lines),
                ),
                error_class="DeviceRejected",
                applied=[] if commit_lines else applied,
                failed=interface_name,
                not_sent=not_sent,
            )
        except Exception as exc:
            logger.exception(
                "配置命令执行中断 vendor=%s command=%s interface=%s", vendor, command_name, interface_name
            )
            return _batch_failure(
                _batch_failure_message(
                    applied=applied,
                    failed=interface_name,
                    not_sent=not_sent,
                    failed_reason="执行中断，状态不明",
                    needs_commit=bool(commit_lines),
                ),
                error_class=type(exc).__name__,
                applied=[] if commit_lines else applied,
                failed=interface_name,
                not_sent=not_sent,
            )
        applied.append(interface_name)

    if commit_lines:
        try:
            outputs.append(
                str(
                    connection.send_config_set(
                        list(commit_lines),
                        read_timeout=read_timeout,
                        error_pattern=error_pattern,
                        enter_config_mode=False,
                        exit_config_mode=False,
                    )
                )
            )
        except ConfigInvalidException as exc:
            error_line = _config_error_line(exc)
            reason = f"（{error_line}）" if error_line else ""
            return _batch_failure(
                f"提交被设备拒绝{reason}；这一批接口都没有生效",
                error_class="DeviceRejected",
                applied=[],
                failed="commit",
                not_sent=[],
            )
        except Exception as exc:
            logger.exception("提交配置中断 vendor=%s command=%s", vendor, command_name)
            return _batch_failure(
                "提交过程中断，这一批接口是否生效无法确定，请人工核实",
                error_class=type(exc).__name__,
                applied=[],
                failed="commit",
                not_sent=[],
            )

    output = "\n".join(outputs)
    marker = CONFIG_SUCCESS_MARKERS.get(vendor)
    if marker is not None and not re.search(marker, output):
        return _unconfirmed("未看到设备确认配置已提交，配置可能未生效或部分生效，请人工核实")
    return output


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
            if re.search(SAVE_BEFORE_REBOOT_QUESTION, chunk):
                # D5：保存与否是人的决定。认出这个问题就把下一步说清楚，而不是只说「没登记」。
                return _unconfirmed(
                    "设备在重启前询问是否保存配置（说明有未保存的改动），已停止、没有替人回答。"
                    "要保留这些改动，请先执行 save_config 保存配置再重启；确定不保存的话，"
                    "请到设备上手动重启。请人工核实设备当前没有在重启"
                )
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
    arguments: CommandArguments | None,
    conn_timeout: float,
    read_timeout: float,
) -> ExecutionResult:
    """在工作线程里跑完整条 Netmiko 会话：连接 → 按类型分派 → 判定结果 → 断开。

    全程同步阻塞，由 DeviceQueryExecutor.execute 用 asyncio.to_thread 调用。
    普通命令在连接建立后就置 dispatched=True；配置命令要等进了配置模式才置位——
    进不去配置模式时一条配置都没发，可以直接重试。
    """
    connection = None
    dispatched = False
    truncated = False
    try:
        connection = _open_netmiko_connection(
            host=host,
            vendor=vendor,
            username=username,
            password=password,
            conn_timeout=conn_timeout,
        )

        if definition.config_templates is not None and vendor in definition.config_templates:
            # 进配置模式之前一条配置都没发：进不去就按未下发处理（例如 Junos 的共享候选库里
            # 有别人未提交的改动时，configure private 会拒绝进入）。
            mode_command = CONFIG_MODE_COMMANDS.get(vendor)
            if mode_command is None:
                connection.config_mode()
            else:
                connection.config_mode(config_command=mode_command)
            dispatched = True
            batch = _run_config_batch(
                connection,
                vendor=vendor,
                command_name=command_name,
                interface_names=(arguments or {}).get("interface_names", ()),
                read_timeout=read_timeout,
            )
            if isinstance(batch, ExecutionResult):
                return batch
            output = batch
            connection.exit_config_mode()
        else:
            # 连接已建立：从这里开始，任何异常都无法确定命令是否已经下发到设备。
            dispatched = True
            if definition.confirmation is not None and vendor in definition.confirmation:
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
                # exec 路径也走渲染：带 {interface} 这类占位符的模板不能原样发到设备上。
                (template,) = rendered_command_lines(command_name, vendor, arguments=arguments)
                if "<" in template or ">" in template:
                    return ExecutionResult(ok=False, message="命令模板含未解析占位符")
                output = connection.send_command(template, read_timeout=read_timeout)
                error_line = _device_error_line(vendor, output, head_only=True)
                if error_line is not None:
                    return _rejected(f"设备拒绝了命令：{error_line}")
                # 按 IP 查表的命令在有的厂商上只能子串匹配：交回去之前按完整地址再滤一遍。
                target_ip = (arguments or {}).get("ip_address")
                if definition.exact_ip_filter and target_ip is not None:
                    output = filter_exact_ip_lines(output, target_ip)
                # 整张表只交回前面一段：存库、预览、AI 总结都不必处理上万行。
                if definition.max_output_lines is not None:
                    output, truncated = truncate_output_lines(output, definition.max_output_lines)
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
        detail={"output": str(output), "truncated": truncated},
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
        arguments: CommandArguments | None = None,
    ) -> ExecutionResult:
        """执行一次设备命令并返回安全结果。

        Args:
            db: 当前事务的数据库会话（目前未使用，保留是为了跟其它执行器
                签名一致，也方便未来加执行前后的额外落库操作）。
            asset: 目标 CMDB 资产，须已配置 vendor 与凭据。
            command_name: 目录里的命令名，调用方保证已通过白名单/校验。
            dynamic_password: 动态凭据时的一次性明文密码；静态凭据时忽略。
            arguments: 目录登记的命令参数（如端口启停的 interface_names）。

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

        try:
            # 执行前最后一道校验：参数只认目录登记过的，值必须合法。
            normalized_arguments = normalize_command_arguments(command_name, arguments or {})
        except ValueError:
            return ExecutionResult(ok=False, message="命令参数无效")

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
                arguments=normalized_arguments,
                conn_timeout=settings.DEVICE_COMMAND_CONN_TIMEOUT_SECONDS,
                read_timeout=device_read_timeout_seconds(command_name),
            ),
        )
