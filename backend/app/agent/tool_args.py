"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: tool_args.py
@DateTime: 2026-08-17
@Docs: Agent 工具的严格参数模型与统一执行管线。

实现流程：
1. 本模块是依赖图上的**叶子**：只依赖 pydantic、device_commands 和 loop 的
   ToolResult，不 import 任何调度器或门控模块。
2. tool_dispatch 与 hitl_gate 都从这里取参数模型。此前两者互相 import
   （tool_dispatch 顶部 import hitl_gate，hitl_gate 在函数体内 import
   tool_dispatch 绕开循环），导致每次门控工具调用都要重跑一次 import 并
   重建一次模型字典。把共享的数据定义下沉到叶子模块后，环自然消失。
3. validate_and_run 收拢「校验 → 执行 → 收敛异常」这条固定管线，保证所有
   工具的失败语义一致：参数问题回 clarification（模型可自行纠正后重试），
   意外异常回 failed 且只带异常类名，不透传原始文本。
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent.device_commands import CommandName
from app.agent.loop import ToolResult


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class KbGlobArgs(_Args):
    pattern: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=100)


class KbGrepArgs(_Args):
    pattern: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=100)
    context_lines: int = Field(default=0, ge=0, le=20)


class KbReadArgs(_Args):
    path: str = Field(min_length=1, max_length=500)
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=4000, ge=1, le=32_000)


class KbSemanticSearchArgs(_Args):
    query: str = Field(min_length=1, max_length=2000)
    category_id: int | None = Field(default=None, ge=1)
    top_k: int = Field(default=5, ge=1, le=20)


class QueryCmdbArgs(_Args):
    asset_ids: list[int] | None = Field(default=None, max_length=100)
    ip: str | None = Field(default=None, min_length=1, max_length=45)
    business_system: str | None = Field(default=None, min_length=1, max_length=100)
    # 运维人开口就是设备名。没有这一项时模型只能回「无法按名称检索，
    # 请你先自己查到 IP 再来问」——eval 实测撞上过。
    hostname: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def exactly_one_filter(self) -> Self:
        """要求恰好一个过滤条件。

        一个都不给会退化成「返回全部资产」，把整个 CMDB 灌进模型上下文——
        千台规模就是几万 token，直接击穿压缩阈值并触发额外的摘要调用。
        校验失败会以 clarification 回灌给模型，它能自行补参数重试。
        """
        selected = sum(
            value is not None
            for value in (self.asset_ids, self.ip, self.business_system, self.hostname)
        )
        if selected == 0:
            raise ValueError("必须提供 asset_ids、ip、business_system 或 hostname 之一")
        if selected > 1:
            raise ValueError("asset_ids, ip, business_system, hostname 最多提供一个")
        return self


class QueryCmdbDependenciesArgs(_Args):
    asset_id: int = Field(ge=1)
    direction: Literal["up", "down"] = "down"
    max_depth: int = Field(default=3, ge=1, le=5)


class QueryMonitorStatusArgs(_Args):
    target_ids: list[int] | None = Field(default=None, max_length=100)
    ip_prefix: str | None = Field(default=None, min_length=1, max_length=45)
    since_limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def at_most_one_filter(self) -> Self:
        if self.target_ids is not None and self.ip_prefix is not None:
            raise ValueError("target_ids 与 ip_prefix 最多提供一个")
        return self


class NotifyPayloadArgs(_Args):
    """notify 工具 payload 内的 message 字段。"""

    message: str = Field(min_length=1, max_length=2000)


class NotifyArgs(_Args):
    """根 Agent 通知工具的模型可控参数。"""

    asset_id: int = Field(ge=1)
    payload: NotifyPayloadArgs
    reason: str = Field(min_length=1, max_length=2000)


class DeviceControlArgs(_Args):
    """根 Agent 设备管控工具的模型可控参数。"""

    asset_id: int = Field(ge=1)
    command_name: CommandName
    interface_name: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)


class QueryDeviceCommandArgs(_Args):
    asset_id: int = Field(ge=1)
    command_name: CommandName
    reason: str = Field(min_length=1, max_length=2000)


class GetDeviceQueryResultArgs(_Args):
    proposal_id: int = Field(ge=1)


class ListDeviceCommandsArgs(_Args):
    asset_id: int = Field(ge=1)


def validation_reason_for_tool(name: str, exc: ValidationError) -> str:
    """把校验错误变成模型可自我纠正的提示：字段名 + 期望约束，不回显输入值。"""
    details: list[str] = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(part) for part in error.get("loc", ())) or "(根)"
        details.append(f"{loc}: {error.get('msg', 'invalid')}")
    joined = "; ".join(dict.fromkeys(details))[:1000]
    return f"工具 {name!r} 参数无效: {joined}"


async def validate_and_run[M: BaseModel](
    name: str,
    arguments: dict[str, Any],
    model: type[M],
    run: Callable[[M], Awaitable[ToolResult]],
) -> ToolResult:
    """统一的工具执行管线：校验参数 → 执行 → 收敛异常。

    收拢在一处是为了保证所有工具的失败语义一致——模型靠 control 字段决定是
    重试纠错（clarification）还是放弃（failed），语义漂移会让它行为不稳。
    异常只回类型名，不透传原始文本。

    Args:
        name: 工具名，用于错误信息。
        arguments: 模型给出的原始参数字典。
        model: 该工具的严格参数模型。
        run: 校验通过后的执行体。

    Returns:
        执行结果，或 clarification / failed 的安全工具结果。
    """
    try:
        parsed = model.model_validate(arguments)
    except ValidationError as exc:
        return ToolResult(control="clarification", content=validation_reason_for_tool(name, exc))
    try:
        return await run(parsed)
    except Exception as exc:
        return ToolResult(
            control="failed",
            content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
        )
