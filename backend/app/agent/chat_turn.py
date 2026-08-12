"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: chat_turn.py
@DateTime: 2026-08-12 12:50
@Docs: 根运维助手一轮对话编排：落库用户消息、包装 chat/dispatch 推 WS、复用 run_loop。

实现流程：
1. 先 append_user_message，保证即使后续模型失败，用户原话仍可被一次 commit 保留。
2. 构造 WsHitlEventPublisher + root dispatcher（可注入替身便于单测）；工具 Schema 用 root_tool_schemas。
3. 不改 loop.py：包装 chat_fn——默认走 llm.chat(stream=True)：
   - 文本 token 经 on_delta 实时 broadcast(assistant_delta, done=false)；
   - 回合结束若有 tool_calls 则广播 tool_call（仅 id/name）；
   - 无工具且有正文：若已推过增量则再推 done=true 空片；若 mock 未走流式则整段一次 done=true。
4. 包装 dispatch_tool 只做透传；pending_approval 的 hitl_* 仍由 dispatcher 内 publisher 发出。
5. 调用既有 run_loop，注入中文 ROOT_OPS_SYSTEM_PROMPT；model_key 使用 MODELS 登记键 local-chat。
6. 正常/early_exit 后广播 turn_done；异常广播中文 error（无堆栈）后原样抛出，由 API 层 commit。
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import ChatFn, LoopOutcome, ToolDispatcher, ToolResult, run_loop
from app.agent.session import append_user_message
from app.agent.tool_dispatch import build_root_tool_dispatcher, root_tool_schemas
from app.agent.ws_hub import AgentWsHub, WsHitlEventPublisher, hub
from app.core.llm import ChatResult, chat
from app.schemas.agent_ws import AgentWsServerMessage

# 架构 §8：根指令每轮从代码注入，不参与压缩摘要
ROOT_OPS_SYSTEM_PROMPT = """你是企业统一运维助手（OpsAssistant）。
你帮助用户做运维知识问答、设备/网段在线状态查询，以及基于 CMDB 的关联排查。
请优先通过已提供的工具取证，再给出有依据的中文回答；不要编造未查到的主机、告警或文档内容。
需要整改或写操作时，只能调用 propose_remediation 提交提案，等待人工审批；
若工具返回等待审批（pending_approval），必须停止并如实告知用户「已提交审批、等待结果」，
禁止杜撰「已执行成功」或伪造执行输出。
回答简洁、可操作；涉及风险操作时明确说明需要审批。"""

# settings 无专用 Agent 模型键；MODELS 默认 chat 登记键为 local-chat
_DEFAULT_MODEL_KEY = "local-chat"


async def run_chat_turn(
    db: AsyncSession,
    *,
    session_id: int,
    actor_user_id: int,
    content: str,
    chat_fn: ChatFn | None = None,
    dispatch_tool: ToolDispatcher | None = None,
    hub_instance: AgentWsHub | None = None,
    publisher: WsHitlEventPublisher | None = None,
    model_key: str | None = None,
) -> LoopOutcome:
    """
    执行一轮用户发消息：落库 → 包装推送 → run_loop → turn_done/error。

    Args:
        db: 异步数据库会话（本函数不 commit）
        session_id: Agent 会话 ID
        actor_user_id: 当前用户 ID（绑进 root dispatcher）
        content: 用户消息正文
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

    await append_user_message(db, session_id, content)

    if dispatch_tool is None:
        base_dispatch: ToolDispatcher = build_root_tool_dispatcher(
            db,
            session_id=session_id,
            actor_user_id=actor_user_id,
            publisher=active_publisher,
        )
    else:
        base_dispatch = dispatch_tool

    resolved_chat: ChatFn = chat_fn if chat_fn is not None else chat
    resolved_model = model_key or _DEFAULT_MODEL_KEY
    tools = root_tool_schemas()

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
        elif result.content:
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
        """透传工具调度；HITL 事件由 dispatcher 内 publisher 负责。"""
        return await base_dispatch(name, arguments)

    try:
        outcome = await run_loop(
            db,
            session_id=session_id,
            model_key=resolved_model,
            dispatch_tool=wrapped_dispatch,
            tools=tools,
            chat_fn=wrapped_chat,
            system_prompt=ROOT_OPS_SYSTEM_PROMPT,
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
