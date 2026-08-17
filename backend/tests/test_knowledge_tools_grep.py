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


async def test_kb_grep_treats_leading_dash_pattern_as_literal_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """以 - 开头的模型可控 pattern 不得被 ripgrep 当成选项解析。

    没有 -e / -- 隔离时，pattern="--files" 会让 rg 列出全部文件（无需模式），
    pattern="-f<路径>" 会让 rg 从任意文件读取模式并在报错里回显其内容。
    这里断言这类 pattern 被当作普通搜索词处理。
    """
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(
        category_code="sop",
        document_id=1,
        filename="flags.md",
        content="正常内容\n包含 --files 字面量的一行\n".encode(),
    )

    result = await kb_grep("--files", category="sop")

    # 被当成搜索词：命中那一行，而不是把目录下文件名列出来
    assert result.control == "ok"
    assert "--files 字面量" in result.content


async def test_kb_grep_pattern_cannot_load_patterns_from_arbitrary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """-f<路径> 形式不得生效，否则等于把宿主机文件内容泄漏给模型。"""
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("SUPER_SECRET_TOKEN\n", encoding="utf-8")
    write_document_file(
        category_code="sop",
        document_id=1,
        filename="doc.md",
        content=b"SUPER_SECRET_TOKEN\n",
    )

    result = await kb_grep(f"-f{secret}", category="sop")

    # 关键安全属性：secret.txt 的**内容**绝不能出现在返回给模型的结果里。
    # pattern 被当字面量正则处理，命不中就是命不中；Windows 路径里的反斜杠
    # 还会让它成为非法正则从而 control=failed——两种结果都可接受，
    # 不可接受的只有「rg 把 secret.txt 当成模式文件」这一种。
    assert "SUPER_SECRET_TOKEN" not in result.content
