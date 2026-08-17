"""知识库管理页相关接口的测试。

覆盖三件事：
1. 上传可以不指定分类（落「未分类」），批量导入历史文档时不必逐份先想清楚归类；
2. 列表的筛选组合正确，尤其是「只看有待确认建议的」这个管理页默认视图；
3. AI 建议只写建议字段、不动真实归属，且采纳后建议被清空。

单文档分类**不得**创建子 Agent（AGENT_ARCHITECTURE §5 反模式红线），
这一条用打桩 spawn_agent 的方式硬断言，防止以后重构时被无声破坏。
"""

from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import ChatResult, EmbeddingResult
from app.crud.knowledge_document import knowledge_document_crud
from app.models.permission import Permission
from app.models.role import role_permissions
from app.models.user import User, user_roles

pytestmark = pytest.mark.asyncio

type Headers = dict[str, str]
type LoginUser = Callable[[str, str], Awaitable[Headers]]


async def _grant_knowledge_permissions(db_session: AsyncSession, test_user: User) -> None:
    """Attach knowledge:* permissions to test_user's existing role."""
    permissions = [
        Permission(name="查看知识库", code="knowledge:read", module="知识库"),
        Permission(name="上传知识文档", code="knowledge:upload", module="知识库"),
        Permission(name="管理知识库", code="knowledge:manage", module="知识库"),
    ]
    db_session.add_all(permissions)
    await db_session.flush()

    role_id = (
        await db_session.execute(
            select(user_roles.c.role_id).where(user_roles.c.user_id == test_user.id)
        )
    ).scalar_one()
    for permission in permissions:
        await db_session.execute(
            role_permissions.insert().values(role_id=role_id, permission_id=permission.id)
        )
    await db_session.commit()


@pytest.fixture
def stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 ingest 不真的去调 embedding 服务。"""

    async def fake_embed(model_key: str, inputs: list[str], **kwargs: object) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1] * 1024 for _ in inputs], prompt_tokens=10)

    monkeypatch.setattr("app.services.knowledge_ingestion.embed", fake_embed)


def _stub_chat(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    """把分类服务用的 llm.chat 换成固定输出。"""

    async def fake_chat(model_key: str, messages: object, **kwargs: object) -> ChatResult:
        return ChatResult(
            content=content,
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    monkeypatch.setattr("app.services.knowledge_classification.chat", fake_chat)


async def _create_category(client: AsyncClient, headers: Headers, code: str) -> int:
    response = await client.post(
        "/api/v1/knowledge/categories",
        json={"code": code, "name": code, "description": ""},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return int(response.json()["data"]["id"])


async def _upload(
    client: AsyncClient,
    headers: Headers,
    *,
    title: str,
    filename: str,
    content: bytes,
    category_code: str | None,
) -> dict[str, object]:
    data: dict[str, str] = {"title": title}
    if category_code is not None:
        data["category_code"] = category_code
    response = await client.post(
        "/api/v1/knowledge/documents",
        data=data,
        files={"file": (filename, content, "text/markdown")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


async def test_upload_without_category_falls_back_to_uncategorized(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)

    payload = await _upload(
        client,
        auth_headers,
        title="未归类文档",
        filename="draft.md",
        content=b"some ops notes",
        category_code=None,
    )

    assert str(payload["file_path"]).startswith("uncategorized/")
    categories = (
        await client.get("/api/v1/knowledge/categories", headers=auth_headers)
    ).json()["data"]
    assert "uncategorized" in [item["code"] for item in categories]


async def test_list_documents_filters_by_category_and_search(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    category_id = await _create_category(client, auth_headers, "sop")
    await _upload(
        client, auth_headers, title="交换机重启", filename="a.md",
        content=b"reboot switch", category_code="sop",
    )
    await _upload(
        client, auth_headers, title="防火墙策略", filename="b.md",
        content=b"firewall policy", category_code="sop",
    )

    listed = await client.get(
        "/api/v1/knowledge/documents",
        params={"category_id": category_id, "search": "重启"},
        headers=auth_headers,
    )

    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["title"] == "交换机重启"


async def test_pending_suggestion_filter_selects_only_documents_with_suggestions(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    category_id = await _create_category(client, auth_headers, "sop")
    with_suggestion = await _upload(
        client, auth_headers, title="有建议", filename="d.md",
        content=b"aaa", category_code="sop",
    )
    await _upload(
        client, auth_headers, title="无建议", filename="e.md",
        content=b"bbb", category_code="sop",
    )

    row = await knowledge_document_crud.get(db_session, int(with_suggestion["id"]))
    assert row is not None
    await knowledge_document_crud.save_suggestion(
        db_session, row, suggested_category_id=category_id, confidence=0.5, reason="r"
    )
    await db_session.commit()

    response = await client.get(
        "/api/v1/knowledge/documents",
        params={"pending_suggestion": "true"},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["title"] == "有建议"


async def test_apply_category_moves_document_and_clears_suggestion(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    await _create_category(client, auth_headers, "sop")
    target_id = await _create_category(client, auth_headers, "topology")
    document = await _upload(
        client, auth_headers, title="待归类", filename="c.md",
        content=b"content", category_code="sop",
    )

    row = await knowledge_document_crud.get(db_session, int(document["id"]))
    assert row is not None
    await knowledge_document_crud.save_suggestion(
        db_session, row, suggested_category_id=target_id, confidence=0.9, reason="正文讲拓扑"
    )
    await db_session.commit()

    response = await client.patch(
        f"/api/v1/knowledge/documents/{document['id']}/category",
        json={"category_id": target_id},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["category_id"] == target_id
    assert payload["suggested_category_id"] is None
    assert payload["suggestion_reason"] == ""
    assert payload["suggestion_confidence"] is None


async def test_apply_category_rejects_unknown_category(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="随便", filename="x.md",
        content=b"x", category_code="sop",
    )

    response = await client.patch(
        f"/api/v1/knowledge/documents/{document['id']}/category",
        json={"category_id": 999999},
        headers=auth_headers,
    )

    assert response.status_code == 404, response.text


async def test_classify_single_document_writes_suggestion_without_spawning(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    """单份文档直接调模型，不创建子 Agent（架构反模式红线）。"""
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    await _create_category(client, auth_headers, "sop")
    await _create_category(client, auth_headers, "topology")
    document = await _upload(
        client, auth_headers, title="拓扑说明", filename="f.md",
        content=b"core switch topology", category_code="sop",
    )

    async def fail_if_spawned(*args: object, **kwargs: object) -> object:
        raise AssertionError("单文档分类不得创建子 Agent")

    monkeypatch.setattr(
        "app.agent.spawn.manager.SpawnManager.spawn_agent", fail_if_spawned
    )
    _stub_chat(
        monkeypatch,
        '{"category":"topology","confidence":0.88,"reason":"正文讲核心交换机拓扑"}',
    )

    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [document["id"]]},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"suggested": 1, "skipped": 0}

    listed = await client.get(
        "/api/v1/knowledge/documents",
        params={"pending_suggestion": "true"},
        headers=auth_headers,
    )
    item = listed.json()["data"]["items"][0]
    assert item["suggestion_confidence"] == 0.88
    assert item["suggestion_reason"] == "正文讲核心交换机拓扑"
    # 建议不得改变真实归属
    assert item["category_id"] != item["suggested_category_id"]


async def test_classify_skips_unknown_category_from_model(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    """模型编造不存在的分类 code 时不落库，避免管理页出现指向空分类的建议。"""
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="随便", filename="g.md",
        content=b"whatever", category_code="sop",
    )
    _stub_chat(monkeypatch, '{"category":"does_not_exist","confidence":0.9,"reason":"编的"}')

    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [document["id"]]},
        headers=auth_headers,
    )

    assert response.json()["data"] == {"suggested": 0, "skipped": 1}


async def test_classify_strips_markdown_fence_from_model_output(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    """小模型常无视「不要代码围栏」的指令，解析前要主动剥一层。"""
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="围栏", filename="h.md",
        content=b"fenced", category_code="sop",
    )
    _stub_chat(
        monkeypatch,
        '```json\n{"category":"sop","confidence":0.7,"reason":"带围栏"}\n```',
    )

    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [document["id"]]},
        headers=auth_headers,
    )

    assert response.json()["data"] == {"suggested": 1, "skipped": 0}


async def test_classify_requires_manage_permission(
    client: AsyncClient, auth_headers: Headers
) -> None:
    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [1]},
        headers=auth_headers,
    )
    assert response.status_code == 403, response.text


async def test_classify_never_offers_uncategorized_as_a_candidate(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    """「未分类」是收纳桶，不能进候选清单。

    放进去就等于允许模型回答"保持原地不动"，而待归类文档本来就在未分类里——
    那种建议落库后在管理页看着可点，点下去分类纹丝不动，只是把建议清空了。
    """
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    await _create_category(client, auth_headers, "sop")
    # 不带 category_code 上传 → 落到「未分类」
    document = await _upload(
        client, auth_headers, title="待归类", filename="u.md",
        content=b"some runbook", category_code=None,
    )

    prompts: list[str] = []

    async def capture_chat(
        model_key: str, messages: list[object], **kwargs: object
    ) -> ChatResult:
        prompts.append("\n".join(str(m.content) for m in messages))  # type: ignore[attr-defined]
        return ChatResult(
            content='{"category":"sop","confidence":0.9,"reason":"是 SOP"}',
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
        )

    monkeypatch.setattr("app.services.knowledge_classification.chat", capture_chat)

    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [document["id"]]},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert prompts, "模型未被调用"
    assert "uncategorized" not in prompts[0]


async def test_document_content_preview_returns_text_and_truncation_flag(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    """预览接口返回正文、总长度与截断标志。"""
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="交换机手册", filename="m.md",
        content="# 标题\n\n正文内容".encode(), category_code="sop",
    )

    full = await client.get(
        f"/api/v1/knowledge/documents/{document['id']}/content",
        headers=auth_headers,
    )
    assert full.status_code == 200, full.text
    data = full.json()["data"]
    assert data["content"] == "# 标题\n\n正文内容"
    assert data["total_chars"] == len("# 标题\n\n正文内容")
    assert data["truncated"] is False
    assert data["title"] == "交换机手册"
    assert data["file_type"] == "md"

    # 截断窗口：只取前 3 个字符，truncated 必须为真，否则前端会把片段当成全文
    partial = await client.get(
        f"/api/v1/knowledge/documents/{document['id']}/content",
        params={"limit": 3},
        headers=auth_headers,
    )
    assert partial.json()["data"]["content"] == "# 标"
    assert partial.json()["data"]["truncated"] is True


async def test_document_content_requires_read_permission_and_404s_when_missing(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
) -> None:
    """无 knowledge:read 时 403；文档不存在时 404。"""
    denied = await client.get(
        "/api/v1/knowledge/documents/1/content", headers=auth_headers
    )
    assert denied.status_code == 403

    await _grant_knowledge_permissions(db_session, test_user)
    missing = await client.get(
        "/api/v1/knowledge/documents/999999/content", headers=auth_headers
    )
    assert missing.status_code == 404
