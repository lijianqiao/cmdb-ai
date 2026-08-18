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
    assert response.json()["data"] == {"suggested": 1, "skipped": 0, "unchanged": 0, "no_match": 0}

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

    assert response.json()["data"] == {"suggested": 0, "skipped": 1, "unchanged": 0, "no_match": 0}


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
    await _create_category(client, auth_headers, "topology")
    # 文档放在 topology、模型答 sop：这样围栏被正确剥掉时结果才是一条真建议，
    # 而不是「维持原分类」——否则本用例会被 unchanged 分支掩盖掉解析失败
    document = await _upload(
        client, auth_headers, title="围栏", filename="h.md",
        content=b"fenced", category_code="topology",
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

    assert response.json()["data"] == {"suggested": 1, "skipped": 0, "unchanged": 0, "no_match": 0}


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


async def test_classify_does_not_persist_a_suggestion_equal_to_current_category(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    """模型认为当前分类正确时不落库，单独计入 unchanged。

    落一条「建议 == 现分类」的记录会在管理页留下一个死结：应用它分类不会变，
    不应用它又一直挂在「待确认」里清不掉。
    """
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="已经归好类", filename="s.md",
        content=b"sop content", category_code="sop",
    )
    # 模型给出的正是它当前所在的分类
    _stub_chat(
        monkeypatch,
        '{"category":"sop","confidence":0.95,"reason":"就是 SOP"}',
    )

    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [document["id"]]},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"suggested": 0, "skipped": 0, "unchanged": 1, "no_match": 0}

    row = await knowledge_document_crud.get(db_session, int(document["id"]))
    assert row is not None
    await db_session.refresh(row)
    assert row.suggested_category_id is None


async def test_duplicate_upload_error_names_the_conflicting_document(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    stub_embedding: None,
) -> None:
    """重复上传要说清撞的是哪一份，只报 id 等于没报。"""
    await _grant_knowledge_permissions(db_session, test_user)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", tmp_path)
    await _create_category(client, auth_headers, "sop")
    await _upload(
        client, auth_headers, title="交换机重启手册", filename="a.md",
        content=b"identical body", category_code="sop",
    )

    # 同样内容、不同文件名、换个分类，仍应命中去重（去重是全库范围的）
    await _create_category(client, auth_headers, "topology")
    duplicate = await client.post(
        "/api/v1/knowledge/documents",
        data={"title": "另一个名字", "category_code": "topology"},
        files={"file": ("b.md", b"identical body", "text/markdown")},
        headers=auth_headers,
    )

    assert duplicate.status_code == 409, duplicate.text
    detail = duplicate.text
    assert "交换机重启手册" in detail
    assert "换个分类" in detail


@pytest.fixture
def knowledge_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """把正文目录和回收站目录都指到临时目录，别写进仓库里的 backend/knowledge*。"""
    root = tmp_path / "knowledge"  # type: ignore[operator]
    trash = tmp_path / "knowledge_trash"  # type: ignore[operator]
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_ROOT", root)
    monkeypatch.setattr("app.services.knowledge_storage.KNOWLEDGE_TRASH_ROOT", trash)


async def test_delete_moves_file_out_of_knowledge_root_and_hides_document(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """删除要同时挡住两条检索路径：列表看不到，且正文文件移出 KNOWLEDGE_ROOT。

    只软删数据库行的话，kb_glob / kb_grep / kb_read 直接扫文件系统，
    Agent 照样能读到并引用这份"已删除"的文档。
    """
    from app.services import knowledge_storage

    await _grant_knowledge_permissions(db_session, test_user)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="要删掉的", filename="del.md",
        content=b"secret runbook", category_code="sop",
    )
    relative_path = str(document["file_path"])
    assert (knowledge_storage.KNOWLEDGE_ROOT / relative_path).is_file()

    deleted = await client.delete(
        f"/api/v1/knowledge/documents/{document['id']}", headers=auth_headers
    )
    assert deleted.status_code == 200, deleted.text

    # 列表里消失
    listed = await client.get("/api/v1/knowledge/documents", headers=auth_headers)
    assert listed.json()["data"]["total"] == 0
    # 文件移出 KNOWLEDGE_ROOT，kb_glob/kb_grep/kb_read 再也扫不到
    assert not (knowledge_storage.KNOWLEDGE_ROOT / relative_path).exists()
    assert (knowledge_storage.KNOWLEDGE_TRASH_ROOT / relative_path).is_file()
    # 回收站里能看到
    trashed = await client.get(
        "/api/v1/knowledge/documents/deleted", headers=auth_headers
    )
    assert trashed.json()["data"]["total"] == 1


async def test_restore_brings_back_row_and_file(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """恢复要把数据库行和正文文件一起还原。"""
    from app.services import knowledge_storage

    await _grant_knowledge_permissions(db_session, test_user)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="恢复我", filename="res.md",
        content=b"restore me", category_code="sop",
    )
    relative_path = str(document["file_path"])
    await client.delete(
        f"/api/v1/knowledge/documents/{document['id']}", headers=auth_headers
    )

    restored = await client.post(
        f"/api/v1/knowledge/documents/{document['id']}/restore", headers=auth_headers
    )

    assert restored.status_code == 200, restored.text
    assert (knowledge_storage.KNOWLEDGE_ROOT / relative_path).is_file()
    listed = await client.get("/api/v1/knowledge/documents", headers=auth_headers)
    assert listed.json()["data"]["total"] == 1


async def test_restore_rejects_when_same_content_was_re_uploaded(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """删除后又重新传了同样内容，恢复会造出重复，必须拦住并说清楚。"""
    await _grant_knowledge_permissions(db_session, test_user)
    await _create_category(client, auth_headers, "sop")
    first = await _upload(
        client, auth_headers, title="原件", filename="a.md",
        content=b"same body", category_code="sop",
    )
    await client.delete(
        f"/api/v1/knowledge/documents/{first['id']}", headers=auth_headers
    )
    # 删掉之后同样内容可以重新上传（去重只看活跃文档）
    await _upload(
        client, auth_headers, title="重新传的", filename="b.md",
        content=b"same body", category_code="sop",
    )

    conflict = await client.post(
        f"/api/v1/knowledge/documents/{first['id']}/restore", headers=auth_headers
    )

    assert conflict.status_code == 409, conflict.text
    assert "重新传的" in conflict.text


async def test_purge_removes_row_chunks_and_file(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """永久删除要清掉行、切片与正文文件。"""
    from sqlalchemy import func as sa_func

    from app.models.knowledge_chunk import KnowledgeChunk
    from app.services import knowledge_storage

    await _grant_knowledge_permissions(db_session, test_user)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="彻底删掉", filename="p.md",
        content=b"purge me", category_code="sop",
    )
    document_id = int(document["id"])
    relative_path = str(document["file_path"])
    await client.delete(
        f"/api/v1/knowledge/documents/{document_id}", headers=auth_headers
    )

    purged = await client.delete(
        f"/api/v1/knowledge/documents/{document_id}/purge", headers=auth_headers
    )

    assert purged.status_code == 200, purged.text
    db_session.expire_all()
    remaining_chunks = (
        await db_session.execute(
            sa_func.count().select().select_from(KnowledgeChunk).where(
                KnowledgeChunk.document_id == document_id
            )
        )
    ).scalar_one()
    assert remaining_chunks == 0
    assert not (knowledge_storage.KNOWLEDGE_TRASH_ROOT / relative_path).exists()
    trashed = await client.get(
        "/api/v1/knowledge/documents/deleted", headers=auth_headers
    )
    assert trashed.json()["data"]["total"] == 0


async def test_trash_routes_require_manage_permission(
    client: AsyncClient,
    auth_headers: Headers,
) -> None:
    """删除/回收站/恢复/永久删除都要 knowledge:manage。"""
    assert (
        await client.get("/api/v1/knowledge/documents/deleted", headers=auth_headers)
    ).status_code == 403
    assert (
        await client.delete("/api/v1/knowledge/documents/1", headers=auth_headers)
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/knowledge/documents/1/restore", headers=auth_headers
        )
    ).status_code == 403
    assert (
        await client.delete(
            "/api/v1/knowledge/documents/1/purge", headers=auth_headers
        )
    ).status_code == 403


async def test_model_saying_no_suitable_category_is_not_reported_as_failure(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """提示词允许模型把 category 留空表示「没有合适的分类」。

    那是一条**有信息量的结论**（该去新建分类），不是故障。先前它和「调用失败 /
    输出解析不了」一起被计进 skipped，界面上显示成"未能给出建议"，
    用户完全看不出下一步该做什么。
    """
    await _grant_knowledge_permissions(db_session, test_user)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="八竿子打不着的文档", filename="x.md",
        content=b"unrelated content", category_code=None,
    )
    _stub_chat(monkeypatch, '{"category":"","confidence":0.2,"reason":"都不沾边"}')

    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [document["id"]]},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "suggested": 0,
        "skipped": 0,
        "unchanged": 0,
        "no_match": 1,
    }


async def test_unparseable_model_output_is_still_a_failure(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """输出压根解析不了才算 skipped——不能和「模型说没有合适分类」混为一谈。"""
    await _grant_knowledge_permissions(db_session, test_user)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="解析不了", filename="y.md",
        content=b"whatever", category_code=None,
    )
    _stub_chat(monkeypatch, "这不是 JSON")

    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [document["id"]]},
        headers=auth_headers,
    )

    assert response.json()["data"] == {
        "suggested": 0,
        "skipped": 1,
        "unchanged": 0,
        "no_match": 0,
    }


async def test_uncategorized_document_gets_a_normal_suggestion(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """未分类文档拿到匹配的分类时，就是一条普通建议——这条路径本来就不特殊。"""
    await _grant_knowledge_permissions(db_session, test_user)
    sop_id = await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="待归类的 SOP", filename="z.md",
        content=b"sop body", category_code=None,
    )
    _stub_chat(monkeypatch, '{"category":"sop","confidence":0.9,"reason":"是 SOP"}')

    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [document["id"]]},
        headers=auth_headers,
    )

    assert response.json()["data"]["suggested"] == 1
    listed = await client.get(
        "/api/v1/knowledge/documents",
        params={"pending_suggestion": "true"},
        headers=auth_headers,
    )
    assert listed.json()["data"]["items"][0]["suggested_category_id"] == sop_id


@pytest.mark.parametrize(
    "raw",
    [
        '根据文档内容，我认为它属于 SOP。\n{"category":"sop","confidence":0.9,"reason":"是 SOP"}',
        '{"category":"sop","confidence":0.9,"reason":"是 SOP"}\n以上是我的判断。',
        '```json\n{"category":"sop","confidence":0.9,"reason":"是 SOP"}\n```',
    ],
)
async def test_prose_around_json_still_yields_a_suggestion(
    raw: str,
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """模型在 JSON 前后加解释文字时不能丢掉结果。

    提示词已写明「不要任何解释性文字」，但模型经常照加不误。那次调用的钱已经
    花了、答案其实也给了，为格式洁癖把它算成"分析失败"既误导用户又浪费预算。
    """
    await _grant_knowledge_permissions(db_session, test_user)
    sop_id = await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="带解释的输出", filename="w.md",
        content=raw.encode()[:40] + b" filler", category_code=None,
    )
    _stub_chat(monkeypatch, raw)

    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [document["id"]]},
        headers=auth_headers,
    )

    assert response.json()["data"]["suggested"] == 1, response.text
    listed = await client.get(
        "/api/v1/knowledge/documents",
        params={"pending_suggestion": "true"},
        headers=auth_headers,
    )
    assert listed.json()["data"]["items"][0]["suggested_category_id"] == sop_id


async def test_output_without_any_json_object_is_still_a_failure(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """兜底提取不能宽到把纯文本也当成建议。"""
    await _grant_knowledge_permissions(db_session, test_user)
    await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="纯文本", filename="v.md",
        content=b"plain", category_code=None,
    )
    _stub_chat(monkeypatch, "我觉得这份文档属于 SOP 分类。")

    response = await client.post(
        "/api/v1/knowledge/documents/classify",
        json={"document_ids": [document["id"]]},
        headers=auth_headers,
    )

    assert response.json()["data"]["skipped"] == 1


async def test_applying_a_category_moves_the_file_so_both_lookups_agree(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """改分类必须连正文文件一起搬。

    kb_glob / kb_grep 按**目录**限定分类，向量检索按**数据库列**限定。只改
    category_id 不搬文件，两条检索路径就会对"这份文档属于哪个分类"给出相反答案：
    归到 sop 之后，向量检索认 sop，而 kb_grep(category="sop") 找不到它，
    kb_grep(category="uncategorized") 反而还能找到。分类的意义正是让按分类检索
    有效，这种错位等于让 AI 建议分类白做。
    """
    from app.services.knowledge_storage import glob_documents

    await _grant_knowledge_permissions(db_session, test_user)
    sop_id = await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="待归类 SOP", filename="run.md",
        content=b"sop body", category_code=None,
    )
    assert str(document["file_path"]).startswith("uncategorized/")

    applied = await client.patch(
        f"/api/v1/knowledge/documents/{document['id']}/category",
        json={"category_id": sop_id},
        headers=auth_headers,
    )
    assert applied.status_code == 200, applied.text

    # 数据库侧：分类与路径都指向 sop
    assert applied.json()["data"]["category_id"] == sop_id
    assert str(applied.json()["data"]["file_path"]).startswith("sop/")

    # 文件系统侧：sop 目录里找得到，原来的 uncategorized 目录里找不到
    assert glob_documents("*.md", category_code="sop")
    assert glob_documents("*.md", category_code="uncategorized") == []


async def test_applying_the_same_category_is_a_no_op_on_disk(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    auth_headers: Headers,
    knowledge_dirs: None,
    stub_embedding: None,
) -> None:
    """归到它已经在的分类时，文件路径不变，也不该把文件搬丢。"""
    from app.services.knowledge_storage import glob_documents

    await _grant_knowledge_permissions(db_session, test_user)
    sop_id = await _create_category(client, auth_headers, "sop")
    document = await _upload(
        client, auth_headers, title="已经在 sop", filename="keep.md",
        content=b"stay", category_code="sop",
    )

    applied = await client.patch(
        f"/api/v1/knowledge/documents/{document['id']}/category",
        json={"category_id": sop_id},
        headers=auth_headers,
    )

    assert applied.status_code == 200, applied.text
    assert applied.json()["data"]["file_path"] == document["file_path"]
    assert glob_documents("*.md", category_code="sop")
