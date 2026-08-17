"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: chat_turn.py
@DateTime: 2026-08-12 12:50
@Docs: 根运维助手一轮对话编排：落库用户消息、包装 chat/dispatch 推 WS、复用 run_loop。

实现流程：
1. 用户消息由 API 层在调用本函数前落库并 commit，本函数不再保存用户消息。
2. 构造 WsHitlEventPublisher + root dispatcher（可注入替身便于单测）；工具 Schema 用 root_tool_schemas。
3. 不改 loop.py：包装 chat_fn——默认走 llm.chat(stream=True)：
   - 文本 token 经 on_delta 实时 broadcast(assistant_delta, done=false)；
   - 回合结束若有 tool_calls 则广播 tool_call（仅 id/name）；
   - 无工具且有正文：若已推过增量则再推 done=true 空片；若 mock 未走流式则整段一次 done=true。
4. 包装 dispatch_tool 只做透传；pending_approval 的 hitl_* 由 dispatcher 内 publisher 入队，
   由 API 层在 db.commit() 之后 flush，避免前端抢跑 GET 未提交提案。
5. 调用既有 run_loop，注入中文 ROOT_OPS_SYSTEM_PROMPT；model_key 使用 MODELS 登记键 local-chat。
6. 正常/early_exit 后广播 turn_done；异常广播中文 error（无堆栈）后原样抛出，由 API 层 commit。
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import Budget
from app.agent.hitl_gate import HitlGateHook
from app.agent.loop import ChatFn, LoopOutcome, ToolDispatcher, ToolResult, run_loop
from app.agent.spawn import spawn_manager
from app.agent.spawn_tools import (
    SPAWN_TOOL_NAMES,
    build_spawn_tool_dispatcher,
    spawn_tool_schemas,
)
from app.agent.tool_dispatch import build_root_tool_dispatcher, root_tool_schemas
from app.agent.ws_hub import AgentWsHub, WsHitlEventPublisher, hub
from app.core.llm import ChatResult, chat
from app.crud.agent_message import agent_message_crud
from app.crud.agent_registry import agent_registry_crud
from app.schemas.agent_ws import AgentWsServerMessage

# 架构 §8：根指令每轮从代码注入，不参与压缩摘要
ROOT_OPS_SYSTEM_PROMPT = """你是企业统一运维助手（OpsAssistant）。
你帮助用户做运维知识问答、设备/网段在线状态查询，以及基于 CMDB 的关联排查。
请优先通过已提供的工具取证，再给出有依据的中文回答；不要编造未查到的主机、告警或文档内容。
需要发送站内通知时，调用 notify；
需要对某台已在 CMDB 登记凭据的设备做会改变状态的操作（重启、关机、启用/禁用接口）时，
调用 device_control；
port_enable/port_disable 必须提供 interface_name。
是否当场执行取决于当前会话审批档位（默认请求审批）。以 list_device_commands
返回的策略句和工具结果为准；若返回 pending_approval，必须如实告知已提交审批，
不得编造设备已重启/端口已切换/通知已发送。
若工具返回等待审批（pending_approval），必须停止并如实告知用户「已提交审批、等待结果」，
禁止杜撰「已执行成功」或伪造执行输出。
需要对某台已在 CMDB 登记凭据的设备做只读诊断（查版本、查配置、查接口、连通性测试）时，
调用 query_device_command；不确定这台设备支持哪些命令、命令是否需要审批时，
先调用 list_device_commands 查看可用命令与策略，不要靠猜。
是否当场执行取决于当前会话审批档位（默认请求审批）。以 list_device_commands
返回的策略句和工具结果为准；若返回 pending_approval，必须如实告知已提交审批，
不得编造设备输出；用户后续追问结果时用 get_device_query_result 回查，不确定是否
已执行完成就不要编造已经查到的内容。
工具报错或被拒绝时，先根据错误提示修正参数重试（如换命令名）；无法解决时，
用中文向用户解释具体原因和下一步建议（如去 CMDB 补充厂商/凭据信息）。
回答简洁、可操作；涉及风险操作时明确说明需要审批。
简单查询（单设备在线状态、单次 CMDB/知识检索）由你直接调用只读工具。
需要同时查多个彼此独立的方面再汇总时，用 investigate_root_cause：
你自己定义 2~10 个分支（name + objective），服务端并行取证后返回汇总。
它不限于故障排查，多业务系统健康度核查、多设备配置比对等同样适用。
批量文档分类（至少 2 份）用 classify_documents。
单一方面的查询不要用这两个工作流，直接调对应只读工具更快。
任何会改变设备状态的操作只能由你通过 device_control 经 HITL 发起。"""

# 根对话用平衡档：日常问答 + 普通工具调用，既不该用便宜档降质量，
# 也不该每一轮都烧强档
_DEFAULT_MODEL_KEY = "chat-balanced"


def _empty_model_usage() -> dict[str, float]:
    return {"prompt_tokens": 0.0, "completion_tokens": 0.0, "cost_usd": 0.0}


def _usage_number(value: object) -> float:
    """从 registry 的 JSON 预算里取一个用量数字；取不到就按 0 算。

    这列是 dict[str, object]，值是从 JSON 反序列化来的，不能假设类型。
    用量统计不是关键路径，脏数据按 0 处理即可，不值得为它中断一整轮对话。
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


async def _record_turn_usage(
    db: AsyncSession,
    *,
    session_id: int,
    message_id: int,
    budget: Budget,
    turn_started_at: datetime,
) -> None:
    """把根循环 + 本轮子 Agent 的用量合计写到最终回复那一行上。

    **必须带上子 Agent**：一次 investigate_root_cause 会并行拉起好几个子 Agent，
    它们各有独立预算，不并进来的话界面上只显示根循环那点开销，会严重低估。

    子 Agent 按「本轮之后创建」筛选。派生子 Agent 的工具会等它们全部跑完才返回
    （orchestration._run_wave 里 gather 了 wait 结果），所以读到的用量是终值。
    """
    prompt_tokens = budget.prompt_tokens_used
    completion_tokens = budget.completion_tokens_used
    cost_usd = budget.cost_used_usd
    by_model: dict[str, dict[str, float]] = {
        key: {
            "prompt_tokens": float(usage.prompt_tokens),
            "completion_tokens": float(usage.completion_tokens),
            "cost_usd": usage.cost_usd,
        }
        for key, usage in budget.usage_by_model.items()
    }

    children = await agent_registry_crud.list_created_since(db, session_id, turn_started_at)
    for child in children:
        snapshot = child.budget if isinstance(child.budget, dict) else {}
        child_prompt = int(_usage_number(snapshot.get("prompt_tokens_used")))
        child_completion = int(_usage_number(snapshot.get("completion_tokens_used")))
        child_cost = _usage_number(snapshot.get("cost_used_usd"))
        if not (child_prompt or child_completion or child_cost):
            continue
        prompt_tokens += child_prompt
        completion_tokens += child_completion
        cost_usd += child_cost
        entry = by_model.setdefault(child.model, _empty_model_usage())
        entry["prompt_tokens"] += child_prompt
        entry["completion_tokens"] += child_completion
        entry["cost_usd"] += child_cost

    await agent_message_crud.set_turn_usage(
        db,
        message_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        usage_by_model=by_model,
    )


async def run_chat_turn(
    db: AsyncSession,
    *,
    session_id: int,
    actor_user_id: int,
    chat_fn: ChatFn | None = None,
    dispatch_tool: ToolDispatcher | None = None,
    hub_instance: AgentWsHub | None = None,
    publisher: WsHitlEventPublisher | None = None,
    model_key: str | None = None,
) -> LoopOutcome:
    """
    执行一轮 Agent turn：包装推送 → run_loop → turn_done/error。

    Args:
        db: 异步数据库会话（本函数不 commit）
        session_id: Agent 会话 ID
        actor_user_id: 当前用户 ID（绑进 root dispatcher）
        chat_fn: 可选注入的模型调用（单测 mock；默认 llm.chat）
        dispatch_tool: 可选工具调度
        hub_instance: 可选 WS hub
        publisher: 可选 HITL 发布器
        model_key: 可选模型键

    Returns:
        LoopOutcome
    """
    active_hub = hub_instance if hub_instance is not None else hub
    active_publisher = (
        publisher
        if publisher is not None
        else WsHitlEventPublisher(hub=active_hub)
    )

    gate_hook: HitlGateHook | None = None
    spawn_dispatch: ToolDispatcher | None = None
    if dispatch_tool is None:
        gate_hook = HitlGateHook(
            db,
            session_id=session_id,
            actor_user_id=actor_user_id,
            publisher=active_publisher,
        )
        base_dispatch: ToolDispatcher = build_root_tool_dispatcher(
            db,
            session_id=session_id,
            actor_user_id=actor_user_id,
            publisher=active_publisher,
            gate_hook=gate_hook,
        )
        spawn_dispatch = build_spawn_tool_dispatcher(spawn_manager, session_id=session_id)
    else:
        base_dispatch = dispatch_tool

    resolved_chat: ChatFn = chat_fn if chat_fn is not None else chat
    resolved_model = model_key or _DEFAULT_MODEL_KEY
    tools = (
        root_tool_schemas() + spawn_tool_schemas()
        if spawn_dispatch is not None
        else root_tool_schemas()
    )

    async def wrapped_chat(
        mk: str,
        messages: list[Any],
        **kwargs: Any,
    ) -> ChatResult:
        """真 token 流推送 assistant_delta；工具轮广播 tool_call。"""
        streamed_text = False

        async def on_delta(text: str) -> None:
            nonlocal streamed_text
            if not text:
                return
            streamed_text = True
            await active_hub.broadcast(
                session_id,
                AgentWsServerMessage(
                    type="assistant_delta",
                    payload={"text": text, "done": False},
                ),
            )

        # 默认开启流式；注入的 mock 若忽略 stream/on_delta，仍返回整段 ChatResult
        # stream/on_delta 放在 kwargs 之后，避免被调用方透传覆盖
        call_kwargs = dict(kwargs)
        call_kwargs.pop("stream", None)
        call_kwargs.pop("on_delta", None)
        if chat_fn is None:
            call_kwargs["db"] = db
        result = await resolved_chat(
            mk,
            messages,
            stream=True,
            on_delta=on_delta,
            **call_kwargs,
        )

        if result.tool_calls:
            for tool_call in result.tool_calls:
                await active_hub.broadcast(
                    session_id,
                    AgentWsServerMessage(
                        type="tool_call",
                        payload={"id": tool_call.id, "name": tool_call.name},
                    ),
                )
        elif result.content and result.finish_reason != "error":
            if streamed_text:
                await active_hub.broadcast(
                    session_id,
                    AgentWsServerMessage(
                        type="assistant_delta",
                        payload={"text": "", "done": True},
                    ),
                )
            else:
                # mock / 非流式 chat_fn：整段一次推送并标记完成
                await active_hub.broadcast(
                    session_id,
                    AgentWsServerMessage(
                        type="assistant_delta",
                        payload={"text": result.content, "done": True},
                    ),
                )
        return result

    async def wrapped_dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        """根工具与 Spawn 工具分流；HITL 事件由 dispatcher 内 publisher 负责。"""
        if spawn_dispatch is not None and name in SPAWN_TOOL_NAMES:
            return await spawn_dispatch(name, arguments)
        return await base_dispatch(name, arguments)

    # 预算对象由本函数持有，run_loop 结束后还要读它的累计用量；
    # 交给 run_loop 自己 new 的话用完就丢了，界面上就没有数字可显示。
    turn_budget = Budget()
    turn_started_at = datetime.now(UTC)
    try:
        outcome = await run_loop(
            db,
            session_id=session_id,
            model_key=resolved_model,
            dispatch_tool=wrapped_dispatch,
            tools=tools,
            budget=turn_budget,
            chat_fn=wrapped_chat,
            system_prompt=ROOT_OPS_SYSTEM_PROMPT,
            before_tool_call=gate_hook.before if gate_hook is not None else None,
            after_tool_call=gate_hook.after if gate_hook is not None else None,
        )
    except Exception:
        await active_hub.broadcast(
            session_id,
            AgentWsServerMessage(
                type="error",
                payload={"message": "本轮对话处理失败，请稍后重试"},
            ),
        )
        raise

    if outcome.usage_message_id is not None:
        await _record_turn_usage(
            db,
            session_id=session_id,
            message_id=outcome.usage_message_id,
            budget=turn_budget,
            turn_started_at=turn_started_at,
        )

    if outcome.reason == "llm_error":
        await active_hub.broadcast(
            session_id,
            AgentWsServerMessage(
                type="error",
                payload={"message": "模型调用失败，请稍后重试"},
            ),
        )

    await active_hub.broadcast(
        session_id,
        AgentWsServerMessage(
            type="turn_done",
            payload={
                "reason": outcome.reason,
                "control": outcome.control,
            },
        ),
    )
    return outcome
