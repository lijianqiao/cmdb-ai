"""Tests for the filesystem-backed Agent tools: kb_glob, kb_read."""

from pathlib import Path

import pytest

from app.agent.knowledge_tools import kb_glob, kb_read
from app.services.knowledge_storage import write_document_file

pytestmark = pytest.mark.asyncio


async def test_kb_glob_returns_matching_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(category_code="sop", document_id=1, filename="a.md", content=b"x")
    write_document_file(category_code="sop", document_id=2, filename="b.md", content=b"x")

    result = await kb_glob("*.md", category="sop")

    assert result.control == "ok"
    assert "sop/1_a.md" in result.content
    assert "sop/2_b.md" in result.content


async def test_kb_glob_returns_ok_with_no_matches_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    result = await kb_glob("*.md", category="empty")

    assert result.control == "ok"
    assert result.content == "没有匹配的文件"


async def test_kb_read_returns_file_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    relative_path = write_document_file(
        category_code="sop", document_id=1, filename="a.md", content="重启步骤".encode()
    )

    result = await kb_read(relative_path)

    assert result.control == "ok"
    assert result.content == "重启步骤"


async def test_kb_read_rejects_path_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    result = await kb_read("../../etc/passwd")

    assert result.control == "rejected"


async def test_kb_read_reports_missing_file_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    result = await kb_read("sop/does-not-exist.md")

    assert result.control == "failed"
