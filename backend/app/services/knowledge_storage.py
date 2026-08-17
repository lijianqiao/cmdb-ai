"""File storage for knowledge-base documents.

Documents are stored as real files under KNOWLEDGE_ROOT/{category_code}/
{document_id}_{filename} (docs/AGENT_ARCHITECTURE.md §4.3, mirroring
docs/guide.md §4.3's knowledge/ convention). Every path-accepting function
resolves and validates containment within KNOWLEDGE_ROOT before touching the
filesystem, to block directory traversal (docs/AGENT_ARCHITECTURE.md §9, L1).
"""

from pathlib import Path

from app.core.config import BACKEND_ROOT

KNOWLEDGE_ROOT = BACKEND_ROOT / "knowledge"


class PathTraversalError(ValueError):
    """Raised when a resolved path would escape KNOWLEDGE_ROOT."""


def sanitize_filename(filename: str) -> str:
    """Strip path separators and leading dots so a filename can't smuggle a path."""
    name = Path(filename).name
    name = name.lstrip(".")
    return name or "unnamed"


def resolve_safe_path(relative_path: str) -> Path:
    """Resolve a path relative to KNOWLEDGE_ROOT, rejecting any escape attempt."""
    KNOWLEDGE_ROOT.mkdir(parents=True, exist_ok=True)
    root = KNOWLEDGE_ROOT.resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise PathTraversalError(f"path {relative_path!r} escapes KNOWLEDGE_ROOT")
    return candidate


def category_dir(category_code: str) -> Path:
    """Return (and ensure exists) the directory for one category."""
    path = resolve_safe_path(category_code)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_document_file(
    *,
    category_code: str,
    document_id: int,
    filename: str,
    content: bytes,
) -> str:
    """Write document content to disk and return its path relative to KNOWLEDGE_ROOT."""
    safe_name = sanitize_filename(filename)
    directory = category_dir(category_code)
    target = directory / f"{document_id}_{safe_name}"
    target.write_bytes(content)
    return str(target.relative_to(KNOWLEDGE_ROOT.resolve())).replace("\\", "/")


def delete_document_file(relative_path: str) -> None:
    """Delete a document's file. A missing file is treated as already-deleted, not an error."""
    path = resolve_safe_path(relative_path)
    path.unlink(missing_ok=True)


def read_document_file(relative_path: str, *, offset: int = 0, limit: int | None = None) -> str:
    """Read a document's text content, optionally paginated by character offset/limit."""
    path = resolve_safe_path(relative_path)
    if not path.is_file():
        raise FileNotFoundError(f"no such document file: {relative_path}")
    text = path.read_text(encoding="utf-8")
    if limit is None:
        return text[offset:]
    return text[offset : offset + limit]


def read_document_preview(
    relative_path: str, *, offset: int = 0, limit: int
) -> tuple[str, int]:
    """Read a bounded window of a document, plus its total character count.

    比 read_document_file 多返回一个总长度，调用方才能判断"是否被截断"——
    没有这个数，前端只能把截断后的正文当成全文展示。

    只读一次整份文件后切片，与 read_document_file 的内存开销相同：文档限定
    .md/.txt 且由管理员上传，实际大小是几 MB 级别。这里真正要挡住的是把几十 MB
    正文丢给浏览器，那由 limit 负责。
    """
    path = resolve_safe_path(relative_path)
    if not path.is_file():
        raise FileNotFoundError(f"no such document file: {relative_path}")
    text = path.read_text(encoding="utf-8")
    return text[offset : offset + limit], len(text)


def glob_documents(pattern: str, *, category_code: str | None = None) -> list[str]:
    """Return paths (relative to KNOWLEDGE_ROOT) of files matching a glob pattern.

    Matches that resolve outside KNOWLEDGE_ROOT (e.g. via a pattern containing
    ``..``) are silently excluded, not raised — this is a listing operation.
    """
    base = category_dir(category_code) if category_code else KNOWLEDGE_ROOT
    base.mkdir(parents=True, exist_ok=True)
    root = KNOWLEDGE_ROOT.resolve()
    matches: list[str] = []
    for candidate in base.glob(pattern):
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            continue
        matches.append(str(resolved.relative_to(root)).replace("\\", "/"))
    return sorted(matches)
