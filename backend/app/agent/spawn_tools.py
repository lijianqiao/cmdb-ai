"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: spawn_tools.py
@DateTime: 2026-08-14
@Docs: 根 Agent 的服务端确定性编排工具 schema 与安全 dispatcher。

实现流程：
1. 向根 Agent 暴露两个确定性编排工作流；**不再暴露 spawn/wait/send/list/close
   五个原语**（见下方「为什么收起原语」）。
2. 模型只能指定工作流的领域参数；session_id、角色、模型、工具白名单、预算
   由 SpawnManager 与角色目录决定，不得由模型覆盖。
3. dispatcher 校验参数后调用 orchestration 工作流，回执只含安全字段。
4. 与 tool_dispatch 分离，避免 spawn 包与 tool_dispatch 形成循环导入。

为什么收起五个原语（2026-08-17）：

- **并行能力没有损失**。investigate_root_cause 的 branches 完全由模型控制
  （2~10 个自定义 name + objective），每个分支跑一个 investigator，而
  investigator 持有全部 7 个只读工具，是其它所有子 Agent 角色的能力超集。
  「开 N 个子 Agent 并行查不同的东西」这件事一次工具调用就能完成。
- **少掉一段极易出错的三步舞**。原语要求模型正确完成 spawn → wait → close：
  漏 wait 会拿不到结果就编造回答；漏 close 会让并发槽被占满 TTL（默认 300 秒，
  而总槽位只有 5 个）。系统提示词曾用三行专门叮嘱这套流程——需要靠提示词
  反复叮嘱的接口本身就是设计负担。
- **给模型腾出工具预算**。工具面从 19 个降到 14 个，省下约 2000 tokens。
  本地小模型在工具数超过十个之后选择准确率下降明显。
- **代价**：模型不能再自定义子 Agent 的角色，也不能做「先 spawn 一个、看结果
  再决定下一个」的单体粒度自适应编排。前者因为 investigator 是能力超集所以
  几乎无影响；后者可以通过多次调用工作流在工作流粒度上达成。

SpawnManager 本身完全保留——它是两个工作流的执行引擎，只是不再直接暴露给模型。
"""

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.agent.loop import ToolDispatcher, ToolResult
from app.agent.orchestration import (
    DEFAULT_ROOT_CAUSE_BRANCHES,
    BatchClassificationOutcome,
    ClassificationDocument,
    RootCauseBranch,
    RootCauseOutcome,
    classify_documents,
    investigate_root_cause,
)
from app.agent.spawn import (
    ChildRuntimeUnavailableError,
    SpawnManager,
    SpawnRejectedError,
)
from app.agent.tool_args import validation_reason_for_tool

SPAWN_TOOL_SCHEMA_VERSION = "t10-v2"
ORCHESTRATION_TOOL_NAMES: frozenset[str] = frozenset(
    {"classify_documents", "investigate_root_cause"}
)
# 保留这个名字是为了不改 chat_turn 的分流判断；现在它与 ORCHESTRATION_TOOL_NAMES 等价。
SPAWN_TOOL_NAMES: frozenset[str] = ORCHESTRATION_TOOL_NAMES
_WORKFLOW_VALIDATION_REASON = "工作流输入不满足执行条件，请修正参数后重试"
_BATCH_OUTCOME_ADAPTER = TypeAdapter(BatchClassificationOutcome)
_ROOT_CAUSE_OUTCOME_ADAPTER = TypeAdapter(RootCauseOutcome)
_SAFE_ERROR_CLASSES: frozenset[str] = frozenset(
    {"model", "tool", "policy_reject", "infra", "budget_exceeded"}
)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ClassifyDocumentsArgs(_Args):
    """classify_documents 批量文档分类工作流参数。"""

    documents: list[ClassificationDocument] = Field(min_length=2, max_length=50)
    allowed_categories: list[str] = Field(default_factory=list, max_length=50)


class InvestigateRootCauseArgs(_Args):
    """investigate_root_cause 多分支并行调查工作流参数。"""

    incident_context: str = Field(min_length=1, max_length=8000)
    branches: list[RootCauseBranch] | None = Field(default=None, min_length=2, max_length=10)


def _inline_json_schema_defs(parameters: dict[str, Any]) -> dict[str, Any]:
    """
    把 Pydantic $defs 内联进 parameters，便于 OpenAI 工具 schema 自包含。

    Args:
        parameters: model_json_schema 产出的 parameters 字典。

    Returns:
        去掉顶层 title/$defs 且已内联引用的 parameters。
    """
    parameters.pop("title", None)
    defs = parameters.pop("$defs", None)
    if not defs:
        return parameters

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                def_name = ref.removeprefix("#/$defs/")
                if def_name in defs:
                    return _resolve(defs[def_name])
            return {key: _resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        return node

    return cast(dict[str, Any], _resolve(parameters))


def spawn_tool_schemas() -> list[dict[str, Any]]:
    """
    返回根 Agent 可用的确定性编排工具 Schema。

    Returns:
        OpenAI 兼容的两个严格函数工具定义。
    """
    classify_parameters = _inline_json_schema_defs(ClassifyDocumentsArgs.model_json_schema())
    root_cause_parameters = _inline_json_schema_defs(
        InvestigateRootCauseArgs.model_json_schema()
    )
    return [
        {
            "type": "function",
            "function": {
                "name": "classify_documents",
                "description": (
                    f"[{SPAWN_TOOL_SCHEMA_VERSION}] 批量文档分类（至少 2 份）；"
                    "服务端负责分波并发、结果解析、复核与清理，仅返回只读建议。"
                ),
                "parameters": classify_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "investigate_root_cause",
                "description": (
                    f"[{SPAWN_TOOL_SCHEMA_VERSION}] 并行只读调查：把一件事拆成 2~10 个"
                    "彼此独立的分支同时取证，服务端负责并发、结果解析、复核与清理。"
                    "不限于故障根因——任何需要「同时查多个方面再汇总」的问题都用它，"
                    "例如同时核查多个业务系统的健康度、多台设备的配置差异、"
                    "多个网段的影响面。branches 由你自己定义（name 是分支名，"
                    "objective 是这个分支要查清楚的事）；省略 branches 时使用"
                    "监控历史/CMDB 拓扑/同网段影响面这三个默认分支。"
                    "每个分支的子 Agent 持有全部只读工具（知识库、CMDB、依赖图、监控）。"
                    "单一方面的查询不要用这个工具，直接调对应的只读工具更快。"
                ),
                "parameters": root_cause_parameters,
            },
        },
    ]


def _spawn_rejected_message(exc: SpawnRejectedError) -> str:
    """把 Spawn 拒绝原因格式化为模型可理解的固定分类。"""
    if exc.limit_name is not None:
        return f"子 Agent 创建被拒绝: {exc.limit_name}"
    return f"子 Agent 创建被拒绝: {exc.reason}"


def build_spawn_tool_dispatcher(
    manager: SpawnManager,
    session_id: int,
) -> ToolDispatcher:
    """
    绑定 SpawnManager 与可信 session_id，生成根 Agent 编排工具调度器。

    Args:
        manager: 进程内 Spawn 运行时，作为工作流的执行引擎。
        session_id: 根会话 ID，不允许由模型覆盖。

    Returns:
        仅处理 ORCHESTRATION_TOOL_NAMES 内工具的异步调度函数。
    """

    async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in ORCHESTRATION_TOOL_NAMES:
            return ToolResult(control="rejected", content=f"未知编排工具：{name}")
        try:
            if name == "classify_documents":
                classify_args = ClassifyDocumentsArgs.model_validate(arguments)
                batch_outcome = await classify_documents(
                    manager,
                    session_id=session_id,
                    documents=classify_args.documents,
                    allowed_categories=classify_args.allowed_categories,
                )
                return ToolResult(
                    control="ok",
                    content=_BATCH_OUTCOME_ADAPTER.dump_json(batch_outcome).decode("utf-8"),
                )

            root_cause_args = InvestigateRootCauseArgs.model_validate(arguments)
            branches = (
                tuple(root_cause_args.branches)
                if root_cause_args.branches is not None
                else DEFAULT_ROOT_CAUSE_BRANCHES
            )
            root_cause_outcome = await investigate_root_cause(
                manager,
                session_id=session_id,
                incident_context=root_cause_args.incident_context,
                branches=branches,
            )
            return ToolResult(
                control="ok",
                content=_ROOT_CAUSE_OUTCOME_ADAPTER.dump_json(root_cause_outcome).decode(
                    "utf-8"
                ),
            )
        except ValidationError as exc:
            return ToolResult(
                control="clarification",
                content=validation_reason_for_tool(name, exc),
            )
        except SpawnRejectedError as exc:
            return ToolResult(control="rejected", content=_spawn_rejected_message(exc))
        except ValueError:
            return ToolResult(
                control="clarification",
                content=_WORKFLOW_VALIDATION_REASON,
            )
        except ChildRuntimeUnavailableError as exc:
            safe_reason = exc.reason if exc.reason in _SAFE_ERROR_CLASSES else "infra"
            return ToolResult(
                control="failed",
                content=f"工具 {name!r} 执行失败: {safe_reason}",
            )
        except Exception as exc:
            return ToolResult(
                control="failed",
                content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
            )

    return dispatch
