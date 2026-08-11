"""为七个 T07/T08 只读 Agent 工具提供带参数校验的安全调度。

实现流程：
1. 为每个既有工具签名定义严格参数模型，并生成提供给模型的 JSON Schema。
2. 将角色持久化的工具白名单冻结到调度闭包中，调用时再次检查权限。
3. 校验通过后才按既有函数签名转发；参数问题要求澄清，未知或越权工具拒绝。
4. 工具自身的业务结果原样返回，意外异常只返回异常类型，避免泄露内部信息。
"""

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.knowledge_tools import kb_glob, kb_grep, kb_read, kb_semantic_search
from app.agent.loop import ToolDispatcher, ToolResult
from app.agent.ops_tools import query_cmdb, query_cmdb_dependencies, query_monitor_status
from app.agent.roles import ToolName

TOOL_SCHEMA_VERSION = "t09-v1"


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

    @model_validator(mode="after")
    def at_most_one_filter(self) -> QueryCmdbArgs:
        selected = sum(
            value is not None
            for value in (self.asset_ids, self.ip, self.business_system)
        )
        if selected > 1:
            raise ValueError("asset_ids, ip, business_system 最多提供一个")
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
    def at_most_one_filter(self) -> QueryMonitorStatusArgs:
        if self.target_ids is not None and self.ip_prefix is not None:
            raise ValueError("target_ids 与 ip_prefix 最多提供一个")
        return self


_ARGUMENT_MODELS: dict[ToolName, type[_Args]] = {
    "kb_glob": KbGlobArgs,
    "kb_grep": KbGrepArgs,
    "kb_read": KbReadArgs,
    "kb_semantic_search": KbSemanticSearchArgs,
    "query_cmdb": QueryCmdbArgs,
    "query_cmdb_dependencies": QueryCmdbDependenciesArgs,
    "query_monitor_status": QueryMonitorStatusArgs,
}

_DESCRIPTIONS: dict[ToolName, str] = {
    "kb_glob": "按 glob 列出 knowledge/ 内文档路径。",
    "kb_grep": "在 knowledge/ 授权范围内用 ripgrep 搜索正文并返回行号。",
    "kb_read": "分页读取 knowledge/ 内一个文档的正文。",
    "kb_semantic_search": "关键词不足时对知识分块做向量语义检索。",
    "query_cmdb": "按资产 ID、IP 或业务系统读取 CMDB 资产。",
    "query_cmdb_dependencies": "按方向和有限深度遍历一个资产的依赖图。",
    "query_monitor_status": "读取目标当前派生状态和有限条最近探测历史。",
}


def tool_schemas_for(allowlist: Iterable[str]) -> list[dict[str, Any]]:
    """Build stable OpenAI-compatible schemas for one role's exact allowlist."""
    schemas: list[dict[str, Any]] = []
    for name in allowlist:
        if name not in _ARGUMENT_MODELS:
            raise ValueError(f"unknown child tool definition: {name!r}")
        tool_name: ToolName = name
        parameters = deepcopy(_ARGUMENT_MODELS[tool_name].model_json_schema())
        parameters.pop("title", None)
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"[{TOOL_SCHEMA_VERSION}] {_DESCRIPTIONS[tool_name]}",
                    "parameters": parameters,
                },
            }
        )
    return schemas


async def _dispatch_validated(
    db: AsyncSession,
    name: ToolName,
    parsed: _Args,
) -> ToolResult:
    if isinstance(parsed, KbGlobArgs):
        return await kb_glob(parsed.pattern, category=parsed.category)
    if isinstance(parsed, KbGrepArgs):
        return await kb_grep(
            parsed.pattern,
            category=parsed.category,
            context_lines=parsed.context_lines,
        )
    if isinstance(parsed, KbReadArgs):
        return await kb_read(parsed.path, offset=parsed.offset, limit=parsed.limit)
    if isinstance(parsed, KbSemanticSearchArgs):
        return await kb_semantic_search(
            db,
            parsed.query,
            category_id=parsed.category_id,
            top_k=parsed.top_k,
        )
    if isinstance(parsed, QueryCmdbArgs):
        return await query_cmdb(
            db,
            asset_ids=parsed.asset_ids,
            ip=parsed.ip,
            business_system=parsed.business_system,
        )
    if isinstance(parsed, QueryCmdbDependenciesArgs):
        return await query_cmdb_dependencies(
            db,
            parsed.asset_id,
            direction=parsed.direction,
            max_depth=parsed.max_depth,
        )
    if isinstance(parsed, QueryMonitorStatusArgs):
        return await query_monitor_status(
            db,
            target_ids=parsed.target_ids,
            ip_prefix=parsed.ip_prefix,
            since_limit=parsed.since_limit,
        )
    return ToolResult(control="failed", content=f"工具 {name!r} 参数模型未绑定执行器")


def build_tool_dispatcher(
    db: AsyncSession,
    allowlist: Iterable[str],
) -> ToolDispatcher:
    """Bind one DB session and immutable role allowlist into a loop dispatcher."""
    allowed = frozenset(allowlist)

    async def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in allowed:
            return ToolResult(control="rejected", content=f"工具 {name!r} 不在角色白名单")
        if name not in _ARGUMENT_MODELS:
            return ToolResult(control="rejected", content=f"未知工具 {name!r}")
        tool_name: ToolName = name
        argument_model = _ARGUMENT_MODELS[tool_name]
        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                control="clarification",
                content=f"工具 {name!r} 参数无效: {exc.error_count()} 处错误",
            )
        try:
            return await _dispatch_validated(db, tool_name, parsed)
        except Exception as exc:
            return ToolResult(
                control="failed",
                content=f"工具 {name!r} 执行失败: {type(exc).__name__}",
            )

    return dispatch
