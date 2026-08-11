"""Agent-facing tools for the knowledge base (docs/AGENT_ARCHITECTURE.md §4.2).

Every function here returns a `ToolResult` (app.agent.loop's contract) so
they are ready to be wired into a real `ToolDispatcher` closure once a caller
with a live `db` session and role exists to invoke `run_loop` with them — that
wiring itself is out of scope for this plan (see T07's header).
"""

from app.agent.loop import ToolResult
from app.services.knowledge_storage import (
    PathTraversalError,
    glob_documents,
    read_document_file,
)


async def kb_glob(pattern: str, *, category: str | None = None) -> ToolResult:
    """List document paths (relative to KNOWLEDGE_ROOT) matching a glob pattern."""
    try:
        paths = glob_documents(pattern, category_code=category)
    except PathTraversalError:
        return ToolResult(control="rejected", content=f"分类越界: {category}")
    if not paths:
        return ToolResult(control="ok", content="没有匹配的文件")
    return ToolResult(control="ok", content="\n".join(paths))


async def kb_read(path: str, *, offset: int = 0, limit: int | None = 4000) -> ToolResult:
    """Read a document's content, paginated by character offset/limit (大结果截断)."""
    try:
        content = read_document_file(path, offset=offset, limit=limit)
    except PathTraversalError:
        return ToolResult(control="rejected", content=f"路径越界: {path}")
    except FileNotFoundError:
        return ToolResult(control="failed", content=f"文件不存在: {path}")
    return ToolResult(control="ok", content=content)
