"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: executors.py
@DateTime: 2026-08-12
@Docs: HITL 动作执行器：notify 写审计日志，device_query/device_control 走 Scrapli。

实现流程：
1. ExecutionResult 是 hitl.resume 与各类执行器之间的统一返回契约（ok/message/detail）。
2. NotifyExecutor 从 payload 读取 message，校验非空后调用 log_audit(action=hitl_notify_executed)。
3. DeviceQueryExecutor 同时服务只读诊断与变更管控：按目录分派 send_command、
   send_interactive（reboot/shutdown 确认）或 send_configs（接口启停）。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from scrapli.driver.core import AsyncIOSXEDriver, AsyncJunosDriver
from scrapli.driver.generic import AsyncGenericDriver
from scrapli_community.huawei.vrp.async_driver import AsyncHuaweiVRPDriver
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.device_commands import (
    UnknownDeviceCommandError,
    VendorName,
    command_supports_vendor,
    get_command_template,
    get_device_command,
    validate_interface_name,
)
from app.core.cmdb_credential import decrypt_credential_password
from app.core.config import settings
from app.models.cmdb_asset import CmdbAsset
from app.utils.audit import log_audit


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """HITL 执行器统一返回结构。"""

    ok: bool
    message: str
    detail: dict[str, object] = field(default_factory=dict)


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


_OUTPUT_TRUNCATE_LIMIT = 4000


def _truncate_output(text: str, *, limit: int = _OUTPUT_TRUNCATE_LIMIT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "…(截断)", True


def _scrapli_driver_class_for_vendor(vendor: str) -> type[Any]:
    """按 CMDB 厂商字段选择 Scrapli 异步驱动类。"""
    if vendor == "cisco_iosxe":
        return AsyncIOSXEDriver
    if vendor == "juniper_junos":
        return AsyncJunosDriver
    if vendor == "huawei_vrp":
        return AsyncHuaweiVRPDriver
    return AsyncGenericDriver


async def _open_scrapli_connection(
    *, host: str, vendor: str, username: str, password: str, timeout_seconds: float
) -> Any:
    """建立一个已认证的 Scrapli 异步连接；抽成独立函数方便测试打桩。"""
    driver_class = _scrapli_driver_class_for_vendor(vendor)
    connection = driver_class(
        host=host,
        auth_username=username,
        auth_password=password,
        auth_strict_key=False,
        timeout_socket=timeout_seconds,
        timeout_transport=timeout_seconds,
    )
    await connection.open()
    return connection


class DeviceQueryExecutor:
    """设备诊断/管控命令执行器：解析凭据、按厂商选真实命令、跑 Scrapli、截断输出。"""

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

        vendor = cast(VendorName, asset.vendor)
        connection = None
        try:
            connection = await _open_scrapli_connection(
                host=asset.ip_address,
                vendor=asset.vendor,
                username=asset.credential_username,
                password=password,
                timeout_seconds=settings.DEVICE_COMMAND_TIMEOUT_SECONDS,
            )

            if definition.config_templates is not None and vendor in definition.config_templates:
                rendered = [
                    line.format(interface=interface_name)
                    for line in definition.config_templates[vendor]
                ]
                if any("<" in line or ">" in line for line in rendered):
                    return ExecutionResult(ok=False, message="命令模板含未解析占位符")
                responses = await connection.send_configs(rendered)
                failed = any(getattr(item, "failed", False) for item in responses)
                output = "\n".join(str(getattr(item, "result", "")) for item in responses)
            elif definition.confirmation is not None and vendor in definition.confirmation:
                confirm = definition.confirmation[vendor]
                template = definition.templates[vendor]
                response = await connection.send_interactive(
                    [(template, confirm.prompt_pattern, False), (confirm.response, r".*", True)]
                )
                failed = getattr(response, "failed", False)
                output = str(getattr(response, "result", ""))
            else:
                template = get_command_template(command_name, asset.vendor)
                if "<" in template or ">" in template:
                    return ExecutionResult(ok=False, message="命令模板含未解析占位符")
                response = await connection.send_command(template)
                failed = getattr(response, "failed", False)
                output = str(getattr(response, "result", ""))
        except Exception:
            return ExecutionResult(
                ok=False,
                message="连接或执行命令失败；如果是重启/关机类命令，设备可能已经生效，请人工核实",
            )
        finally:
            if connection is not None:
                try:
                    await connection.close()
                except Exception:
                    pass

        if failed:
            return ExecutionResult(ok=False, message="设备返回命令执行失败")

        rendered_output, truncated = _truncate_output(output)
        return ExecutionResult(
            ok=True,
            message="命令执行完成",
            detail={"output": rendered_output, "truncated": truncated},
        )
