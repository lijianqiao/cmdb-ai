"""API tests for the knowledge base upload endpoints."""

from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permission import Permission
from app.models.user import User, user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]
type LoginUser = Callable[[str, str], Awaitable[Headers]]


async def _grant_knowledge_permissions(db_session: AsyncSession, test_user: User) -> None:
    """Attach knowledge:* permissions to test_user's existing role."""
    from app.models.role import role_permissions

    permissions = [
        Permission(name="查看知识库", code="knowledge:read", module="知识库"),
        Permission(name="上传知识文档", code="knowledge:upload", module="知识库"),
        Permission(name="管理知识库", code="knowledge:manage", module="知识库"),
    ]
    db_session.add_all(permissions)
    await db_session.flush()

    role_id = (
        await db_session.execute(select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id))
    ).scalar_one()
    for permission in permissions:
        await db_session.execute(
            role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
        )
    await db_session.commit()


async def test_create_and_list_categories(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)

    create_response = await client.post(
        "/api/v1/knowledge/categories",
        json={"code": "sop", "name": "故障处理 SOP", "description": "运维故障处理手册"},
        headers=auth_headers,
    )
    assert create_response.status_code == 201, create_response.text

    list_response = await client.get("/api/v1/knowledge/categories", headers=auth_headers)
    assert list_response.status_code == 200, list_response.text
    codes = [item["code"] for item in list_response.json()["data"]]
    assert "sop" in codes


async def test_upload_document_succeeds(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    async def fake_embed(model_key: str, inputs: list[str], **kwargs: object) -> object:
        from app.core.llm import EmbeddingResult

        return EmbeddingResult(vectors=[[0.1] * 1024 for _ in inputs], prompt_tokens=10)

    monkeypatch.setattr("app.services.knowledge_ingestion.embed", fake_embed)

    await client.post(
        "/api/v1/knowledge/categories",
        json={"code": "sop", "name": "SOP", "description": ""},
        headers=auth_headers,
    )

    response = await client.post(
        "/api/v1/knowledge/documents",
        data={"category_code": "sop", "title": "重启流程"},
        files={"file": ("reboot.md", b"switch reboot: step one, step two", "text/markdown")},
        headers=auth_headers,
    )

    assert response.status_code == 201, response.text
    payload = response.json()["data"]
    assert payload["status"] == "ready"
    assert payload["file_path"].startswith("sop/")


async def test_upload_document_without_permission_returns_403(
    client: AsyncClient, auth_headers: Headers
) -> None:
    response = await client.post(
        "/api/v1/knowledge/documents",
        data={"category_code": "sop", "title": "无权限上传"},
        files={"file": ("a.md", b"content", "text/markdown")},
        headers=auth_headers,
    )
    assert response.status_code == 403, response.text


async def test_upload_document_rejects_unsupported_file_type(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)
    await client.post(
        "/api/v1/knowledge/categories",
        json={"code": "sop", "name": "SOP", "description": ""},
        headers=auth_headers,
    )

    response = await client.post(
        "/api/v1/knowledge/documents",
        data={"category_code": "sop", "title": "不支持的格式"},
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        headers=auth_headers,
    )

    assert response.status_code == 422, response.text
