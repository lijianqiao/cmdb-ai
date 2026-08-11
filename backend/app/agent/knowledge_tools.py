"""Agent-facing tools for the knowledge base (docs/AGENT_ARCHITECTURE.md §4.2).

Every function here returns a `ToolResult` (app.agent.loop's contract) so
they are ready to be wired into a real `ToolDispatcher` closure once a caller
with a live `db` session and role exists to invoke `run_loop` with them — that
wiring itself is out of scope for this plan (see T07's header).
"""

import asyncio
import shutil

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import ToolResult
from app.core.llm import LlmRequestError, embed
from app.crud.knowledge_chunk import knowledge_chunk_crud
from app.services import knowledge_storage
from app.services.knowledge_storage import (
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
        search_root = category_dir(category) if category else knowledge_storage.KNOWLEDGE_ROOT
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


async def kb_semantic_search(
    db: AsyncSession,
    query: str,
    *,
    category_id: int | None = None,
    top_k: int = 5,
    embedding_model_key: str = "local-embedding",
) -> ToolResult:
    """Embed `query` and return the top_k most similar knowledge chunks.

    Requires a real Postgres+pgvector backend for `search_similar()` — see
    app/crud/knowledge_chunk.py.
    """
    try:
        embedding_result = await embed(embedding_model_key, [query])
    except LlmRequestError as exc:
        return ToolResult(control="failed", content=f"embedding 失败: {exc}")

    results = await knowledge_chunk_crud.search_similar(
        db, query_embedding=embedding_result.vectors[0], category_id=category_id, top_k=top_k
    )
    if not results:
        return ToolResult(control="ok", content="没有找到相关内容")

    lines = [
        f"[document_id={chunk.document_id} chunk_index={chunk.chunk_index} "
        f"distance={distance:.4f}] {chunk.content}"
        for chunk, distance in results
    ]
    return ToolResult(control="ok", content="\n\n".join(lines))
