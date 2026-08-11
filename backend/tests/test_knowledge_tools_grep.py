"""Tests for kb_grep — the ripgrep-backed Agent tool.

These tests genuinely shell out to ripgrep (rg must be on PATH — confirmed
present in this environment, `rg 15.0.0`). They do not mock the subprocess.
"""

from pathlib import Path

import pytest

from app.agent.knowledge_tools import kb_grep
from app.services.knowledge_storage import write_document_file

pytestmark = pytest.mark.asyncio


async def test_kb_grep_finds_matches_with_line_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(
        category_code="sop",
        document_id=1,
        filename="reboot.md",
        content="第一行\n交换机重启步骤\n第三行\n".encode(),
    )

    result = await kb_grep("重启", category="sop")

    assert result.control == "ok"
    assert "交换机重启步骤" in result.content
    assert ":2:" in result.content  # line number of the match


async def test_kb_grep_returns_ok_with_no_matches_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(category_code="sop", document_id=1, filename="a.md", content="无关内容".encode())

    result = await kb_grep("不存在的关键词", category="sop")

    assert result.control == "ok"
    assert result.content == "没有匹配"


async def test_kb_grep_scoped_to_category_excludes_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(category_code="sop", document_id=1, filename="a.md", content="重启".encode())
    write_document_file(category_code="topology", document_id=2, filename="b.md", content="重启".encode())

    result = await kb_grep("重启", category="sop")

    assert "sop" in result.content
    assert "topology" not in result.content


async def test_kb_grep_reports_missing_binary_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr("app.agent.knowledge_tools.shutil.which", lambda name: None)

    result = await kb_grep("anything")

    assert result.control == "failed"


async def test_kb_grep_without_category_uses_live_knowledge_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(category_code="sop", document_id=1, filename="a.md", content="重启内容".encode())

    result = await kb_grep("重启")

    assert result.control == "ok"
    assert "重启内容" in result.content
