"""Agent-facing tools for the knowledge base (docs/AGENT_ARCHITECTURE.md §4.2).

Every function here returns a `ToolResult` (app.agent.loop's contract) so
they are ready to be wired into a real `ToolDispatcher` closure once a caller
with a live `db` session and role exists to invoke `run_loop` with them — that
wiring itself is out of scope for this plan (see T07's header).
"""

import asyncio
import shutil

from app.agent.loop import ToolResult
from app.services.knowledge_storage import (
    KNOWLEDGE_ROOT,
    PathTraversalError,
    category_dir,
    glob_documents,
    read_document_file,
)

_RIPGREP_TIMEOUT_SECONDS = 10.0
_MAX_GREP_OUTPUT_BYTES = 32_000


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


async def kb_grep(
    pattern: str,
    *,
    category: str | None = None,
    context_lines: int = 0,
) -> ToolResult:
    """Search knowledge-base documents with ripgrep, scoped to KNOWLEDGE_ROOT or one category."""
    if shutil.which("rg") is None:
        return ToolResult(control="failed", content="ripgrep(rg) 未安装或不在 PATH 中")

    try:
        search_root = category_dir(category) if category else KNOWLEDGE_ROOT
    except PathTraversalError:
        return ToolResult(control="rejected", content=f"分类越界: {category}")

    args = ["rg", "--line-number", "--no-heading"]
    if context_lines:
        args += ["-C", str(context_lines)]
    args += [pattern, str(search_root)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_RIPGREP_TIMEOUT_SECONDS)
    except TimeoutError:
        return ToolResult(control="failed", content="kb_grep 超时")

    if proc.returncode not in (0, 1):  # 1 = ripgrep ran fine, just no matches
        return ToolResult(
            control="failed", content=f"ripgrep 出错: {stderr.decode('utf-8', errors='replace')}"
        )

    output = stdout.decode("utf-8", errors="replace")
    if len(output.encode("utf-8")) > _MAX_GREP_OUTPUT_BYTES:
        truncated = output.encode("utf-8")[:_MAX_GREP_OUTPUT_BYTES]
        output = truncated.decode("utf-8", errors="ignore") + "\n...(结果已截断)"
    return ToolResult(control="ok", content=output.strip() or "没有匹配")
