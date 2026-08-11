"""Tests for the knowledge-base file storage service (path safety + I/O)."""

from pathlib import Path

import pytest

from app.services.knowledge_storage import (
    PathTraversalError,
    glob_documents,
    read_document_file,
    resolve_safe_path,
    sanitize_filename,
    write_document_file,
)


def test_sanitize_filename_strips_directory_components() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("normal.md") == "normal.md"
    assert sanitize_filename("..hidden") == "hidden"


def test_resolve_safe_path_rejects_traversal() -> None:
    with pytest.raises(PathTraversalError):
        resolve_safe_path("../../etc/passwd")


def test_resolve_safe_path_accepts_nested_path_within_root() -> None:
    path = resolve_safe_path("sop/1_a.md")
    from app.services.knowledge_storage import KNOWLEDGE_ROOT

    assert KNOWLEDGE_ROOT.resolve() in path.parents or path == KNOWLEDGE_ROOT.resolve()


def test_write_and_read_document_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    relative_path = write_document_file(
        category_code="sop", document_id=1, filename="reboot.md", content="重启步骤：先...".encode()
    )

    assert relative_path == "sop/1_reboot.md"
    content = read_document_file(relative_path)
    assert content == "重启步骤：先..."


def test_read_document_file_supports_offset_and_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    relative_path = write_document_file(
        category_code="sop", document_id=1, filename="a.md", content=b"0123456789"
    )

    assert read_document_file(relative_path, offset=2, limit=3) == "234"


def test_read_document_file_raises_for_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        read_document_file("sop/does-not-exist.md")


def test_glob_documents_scoped_to_category(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    write_document_file(category_code="sop", document_id=1, filename="a.md", content=b"x")
    write_document_file(category_code="topology", document_id=2, filename="b.md", content=b"x")

    sop_only = glob_documents("*.md", category_code="sop")

    assert sop_only == ["sop/1_a.md"]
