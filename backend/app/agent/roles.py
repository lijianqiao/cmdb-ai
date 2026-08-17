"""Versioned, code-owned child-Agent role catalog for T09.

Descriptions tell the root orchestrator when delegation is appropriate;
instructions constrain how the selected child works. Tool permissions are
separate immutable allowlists and are rechecked by tool_dispatch.py, so a
prompt injection cannot expand a child's capabilities.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

from app.schemas.system_config import ChatTier

type RoleName = Literal[
    "classifier",
    "kb_explorer",
    "ops_explorer",
    "investigator",
    "reviewer",
]
type ToolName = Literal[
    "kb_glob",
    "kb_grep",
    "kb_read",
    "kb_semantic_search",
    "query_cmdb",
    "query_cmdb_dependencies",
    "query_monitor_status",
]
# 与配置层的档位是同一套东西，共用一个类型避免两处枚举各自漂移。
# 旧值 "reasoning" 已并入 "strong"（配置键是 LLM_CHAT_STRONG_*）。
type ModelTier = ChatTier

# t09-v2：档位从"声明了但没人用的元数据"变成真正决定用哪个模型
ROLE_CATALOG_VERSION = "t09-v2"

_KNOWLEDGE_TOOLS: tuple[ToolName, ...] = (
    "kb_glob",
    "kb_grep",
    "kb_read",
    "kb_semantic_search",
)
_OPS_TOOLS: tuple[ToolName, ...] = (
    "query_cmdb",
    "query_cmdb_dependencies",
    "query_monitor_status",
)


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    """One immutable role contract selected by `spawn_agent`."""

    name: RoleName
    version: str
    description: str
    instructions: str
    model_tier: ModelTier
    sandbox_mode: Literal["read-only"]
    tools_allowlist: tuple[ToolName, ...]

    @property
    def model_key(self) -> str:
        """档位对应的 MODELS 登记键。

        档位是唯一事实来源，键由它派生——两处各写一遍迟早会对不上，
        而且这个字段以前就是"声明了却没人读"的状态，正是这么烂掉的。
        """
        return f"chat-{self.model_tier}"


class UnknownAgentRoleError(ValueError):
    """Raised before spawning when a role is not in the hosted catalog."""


_ROLE_CATALOG: dict[RoleName, RoleDefinition] = {
    "classifier": RoleDefinition(
        name="classifier",
        version=ROLE_CATALOG_VERSION,
        description=(
            "仅用于两份及以上知识文档的并行归类建议；单文档分类应由根 Agent "
            "直接完成，不创建子 Agent。"
        ),
        instructions="""你是运维知识文档分类器，只读取 task_brief 指定的文档。
先用 kb_read 获取正文；只有路径或关键词不明确时才使用 kb_glob/kb_grep。
不得修改文件、数据库或分类。只能从 allowed_categories 选择；若均不合适，
把 recommended_category 写为建议的新 code，并把 needs_review 设为 true。
最终回答必须是一个 JSON 对象，不要 Markdown 代码围栏：
{"document_id":整数,"recommended_category":"code","confidence":0到1,
"needs_review":布尔值,"reason":"有正文证据的简短理由"}。""",
        # 便宜档：从正文里抽结构化 JSON，是分类不是判断
        model_tier="fast",
        sandbox_mode="read-only",
        tools_allowlist=("kb_glob", "kb_grep", "kb_read"),
    ),
    "kb_explorer": RoleDefinition(
        name="kb_explorer",
        version=ROLE_CATALOG_VERSION,
        description=(
            "用于需要独立知识检索上下文的只读取证，例如跨多份 SOP 查证；单次 "
            "Glob/Grep/Read 查询不要委派。"
        ),
        instructions="""你是运维知识库检索员。只处理 task_brief 明确的问题，
优先 Glob/Grep/Read 获取可引用原文，关键词检索不足时才用 semantic search。
不得修改知识文件或数据库。结论必须区分“文档明确说明”“根据证据推断”与
“当前资料没有覆盖”，并在摘要中写出文件路径或 document_id。""",
        model_tier="fast",
        sandbox_mode="read-only",
        tools_allowlist=_KNOWLEDGE_TOOLS,
    ),
    "ops_explorer": RoleDefinition(
        name="ops_explorer",
        version=ROLE_CATALOG_VERSION,
        description=(
            "用于一个独立 CMDB 或监控数据源的结构化取证；单设备在线状态查询 "
            "应由根 Agent 直接调用工具。"
        ),
        instructions="""你是运维结构化数据检索员。只调用 CMDB、依赖图和监控
状态只读工具，围绕 task_brief 返回资产身份、归属、拓扑或状态证据。
“尚未探测”不等于“离线”；监控当前状态必须以最新事件派生结果为准。
不得修改资产、目标或状态记录，证据不足时明确列出缺少的筛选条件。""",
        model_tier="fast",
        sandbox_mode="read-only",
        tools_allowlist=_OPS_TOOLS,
    ),
    "investigator": RoleDefinition(
        name="investigator",
        version=ROLE_CATALOG_VERSION,
        description=(
            "用于根因排查中的一个独立假设分支，可跨知识、CMDB 和监控只读取证；"
            "同一事故的多个分支适合并行。"
        ),
        instructions="""你是根因调查员，只验证 task_brief 指定的一个假设分支，
不要提前综合兄弟分支。允许跨知识、CMDB、依赖和监控工具取只读证据。
不得把时间相关性表述为因果，也不得伪造当前工具没有提供的 CMDB 变更日志。
最终回答必须是一个 JSON 对象，不要 Markdown 代码围栏：
{"branch":"分支名","hypothesis":"被检验的假设","confidence":0到1,
"evidence":["证据"],"gaps":["证据缺口"],"next_checks":["下一步只读检查"]}。""",
        model_tier="balanced",
        sandbox_mode="read-only",
        tools_allowlist=_KNOWLEDGE_TOOLS + _OPS_TOOLS,
    ),
    "reviewer": RoleDefinition(
        name="reviewer",
        version=ROLE_CATALOG_VERSION,
        description=(
            "只在分类冲突、新分类建议、调查分支矛盾或需要最终证据复核时使用；"
            "它不执行写入，也不替代根 Agent 面向用户。"
        ),
        instructions="""你是只读复核员。task_brief 会说明 workflow 和精确输出
契约；检查每条结论是否有工具证据、不同分支是否冲突、是否把未知写成已知。
必要时可调用只读知识/CMDB/监控工具复核，但不得修改数据或发起处置。
严格按 task_brief 的 output_contract 返回一个 JSON 对象，不要 Markdown 围栏，
并把无法验证、缺少变更日志或解析失败的内容列入 evidence_gaps。""",
        model_tier="strong",
        sandbox_mode="read-only",
        tools_allowlist=_KNOWLEDGE_TOOLS + _OPS_TOOLS,
    ),
}

ROLE_CATALOG: Mapping[RoleName, RoleDefinition] = MappingProxyType(_ROLE_CATALOG)


def get_role(name: str) -> RoleDefinition:
    """Return one hosted role or fail closed before allocating any resources."""
    if name not in ROLE_CATALOG:
        raise UnknownAgentRoleError(f"unknown child-Agent role {name!r}")
    return ROLE_CATALOG[cast(RoleName, name)]


def list_roles() -> tuple[RoleDefinition, ...]:
    """Return role definitions in stable catalog order."""
    return tuple(ROLE_CATALOG.values())
