"""Knowledge-base routes: category management, document upload, preview, and AI classification."""

import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_client_ip, require_permission
from app.crud.knowledge_category import knowledge_category_crud
from app.crud.knowledge_document import knowledge_document_crud
from app.models.knowledge_category import UNCATEGORIZED_CODE, UNCATEGORIZED_NAME
from app.models.user import User
from app.schemas.common import (
    PaginatedData,
    ResponseEnvelope,
    paginated_response,
    success_response,
)
from app.schemas.knowledge import (
    KnowledgeCategoryCreate,
    KnowledgeCategoryResponse,
    KnowledgeClassifyRequest,
    KnowledgeClassifyResponse,
    KnowledgeDocumentCategoryUpdate,
    KnowledgeDocumentContentResponse,
    KnowledgeDocumentResponse,
)
from app.services.knowledge_classification import suggest_categories
from app.services.knowledge_ingestion import DuplicateDocumentError, ingest_document
from app.services.knowledge_storage import (
    PathTraversalError,
    move_document_to_category,
    move_document_to_trash,
    purge_document_from_trash,
    read_document_preview,
    restore_document_from_trash,
)
from app.utils.audit import log_audit

logger = logging.getLogger(__name__)

router = APIRouter()

_SUPPORTED_FILE_TYPES = {"md", "txt"}
# 单次预览返回的最大字符数。超出部分由前端提示"已截断"，避免把一份超大文档
# 整个塞进 HTTP 响应和浏览器内存。
_PREVIEW_CHAR_LIMIT = 200_000


@router.post(
    "/categories",
    response_model=ResponseEnvelope[KnowledgeCategoryResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_category(
    category_in: KnowledgeCategoryCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:manage")),
) -> ResponseEnvelope[KnowledgeCategoryResponse]:
    """Create a knowledge category."""
    existing = await knowledge_category_crud.get_by_code(db, category_in.code)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="分类代码已存在")

    category = await knowledge_category_crud.create(db, category_in.model_dump())
    await log_audit(
        db,
        current_user.id,
        "create_knowledge_category",
        target=f"knowledge_category:{category.id}",
        detail=f"创建知识库分类: {category.code}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(
        KnowledgeCategoryResponse.model_validate(category), message="创建成功", code=status.HTTP_201_CREATED
    )


@router.get("/categories", response_model=ResponseEnvelope[list[KnowledgeCategoryResponse]])
async def list_categories(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("knowledge:read")),
) -> ResponseEnvelope[list[KnowledgeCategoryResponse]]:
    """List every knowledge category."""
    categories = await knowledge_category_crud.list_all(db)
    return success_response([KnowledgeCategoryResponse.model_validate(c) for c in categories])


@router.post(
    "/documents",
    response_model=ResponseEnvelope[KnowledgeDocumentResponse],
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    request: Request,
    title: str = Form(...),
    file: UploadFile = File(...),
    category_code: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:upload")),
) -> ResponseEnvelope[KnowledgeDocumentResponse]:
    """Upload a document (.md/.txt only), chunk it, embed it, and store it.

    `category_code` 可省略：省略时落到「未分类」，之后可在知识库管理页用
    AI 建议或人工归类。这样批量导入历史文档时不必逐份先想清楚分类。
    """
    filename = file.filename or "unnamed"
    file_type = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if file_type not in _SUPPORTED_FILE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"不支持的文件类型: .{file_type}（仅支持 .md/.txt）",
        )

    if category_code:
        category = await knowledge_category_crud.get_by_code(db, category_code)
        if category is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分类不存在")
    else:
        category = await knowledge_category_crud.get_by_code(db, UNCATEGORIZED_CODE)
        if category is None:
            category = await knowledge_category_crud.create(
                db,
                {
                    "code": UNCATEGORIZED_CODE,
                    "name": UNCATEGORIZED_NAME,
                    "description": "上传时未指定分类的文档，等待 AI 建议或人工归类",
                },
            )
            await db.flush()

    content = await file.read()

    try:
        document = await ingest_document(
            db,
            category_id=category.id,
            category_code=category.code,
            title=title,
            original_filename=filename,
            file_type=file_type,
            content=content,
            uploaded_by=current_user.id,
        )
    except DuplicateDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"这份内容已存在：《{exc.title}》(ID {exc.document_id})。"
                "知识库按正文去重，同一份内容换个分类重传也会命中；"
                "如果确实要替换，请先删除原文档。"
            ),
        ) from exc

    await log_audit(
        db,
        current_user.id,
        "upload_knowledge_document",
        target=f"knowledge_document:{document.id}",
        detail=f"上传知识文档: {document.title}",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(
        KnowledgeDocumentResponse.model_validate(document), message="上传成功", code=status.HTTP_201_CREATED
    )


@router.get(
    "/documents",
    response_model=ResponseEnvelope[PaginatedData[KnowledgeDocumentResponse]],
)
async def list_documents(
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=20, ge=1, le=100),
    category_id: int | None = Query(default=None, ge=1),
    search: str | None = Query(default=None, max_length=200),
    pending_suggestion: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("knowledge:read")),
) -> ResponseEnvelope[PaginatedData[KnowledgeDocumentResponse]]:
    """分页列出知识文档，支持按分类、标题关键词与「有无待确认 AI 建议」筛选。"""
    documents, total = await knowledge_document_crud.list_filtered(
        db,
        category_id=category_id,
        search=search,
        pending_suggestion=pending_suggestion,
        skip=(page - 1) * page_size,
        limit=page_size,
    )
    items = [KnowledgeDocumentResponse.model_validate(item) for item in documents]
    return paginated_response(items, total, page, page_size)


@router.get(
    "/documents/{document_id}/content",
    response_model=ResponseEnvelope[KnowledgeDocumentContentResponse],
)
async def get_document_content(
    document_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=_PREVIEW_CHAR_LIMIT, ge=1, le=_PREVIEW_CHAR_LIMIT),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("knowledge:read")),
) -> ResponseEnvelope[KnowledgeDocumentContentResponse]:
    """读取文档正文用于预览（只读，不改任何状态）。

    只按 document_id 取，路径来自库里的记录而不是请求参数——用户无法指定路径，
    加上 knowledge_storage 自身的 KNOWLEDGE_ROOT 包含校验，目录穿越走不通。
    """
    document = await knowledge_document_crud.get(db, document_id)
    if document is None or document.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    try:
        content, total_chars = read_document_preview(
            document.file_path, offset=offset, limit=limit
        )
    except (FileNotFoundError, OSError, PathTraversalError, UnicodeDecodeError) as exc:
        # 记录在库里但文件读不出来：多半是磁盘上的文件被手工删了或改坏了。
        # 对调用方是 404（这份文档现在没有可展示的正文），不是 500。
        logger.warning(
            "文档正文读取失败 document_id=%s path=%r", document_id, document.file_path
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="文档正文不可读"
        ) from exc

    return success_response(
        KnowledgeDocumentContentResponse(
            document_id=document.id,
            title=document.title,
            file_type=document.file_type,
            content=content,
            total_chars=total_chars,
            offset=offset,
            truncated=offset + len(content) < total_chars,
        )
    )


@router.get(
    "/documents/deleted",
    response_model=ResponseEnvelope[PaginatedData[KnowledgeDocumentResponse]],
)
async def list_deleted_documents(
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission("knowledge:manage")),
) -> ResponseEnvelope[PaginatedData[KnowledgeDocumentResponse]]:
    """回收站：分页列出已删除的知识文档。"""
    documents, total = await knowledge_document_crud.list_deleted(
        db, skip=(page - 1) * page_size, limit=page_size
    )
    items = [KnowledgeDocumentResponse.model_validate(item) for item in documents]
    return paginated_response(items, total, page, page_size)


@router.delete("/documents/{document_id}", response_model=ResponseEnvelope[None])
async def delete_document(
    document_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:manage")),
) -> ResponseEnvelope[None]:
    """把文档移入回收站：软删除数据库行，并把正文文件移出 KNOWLEDGE_ROOT。

    **两件事必须一起做。** 只软删数据库行的话，kb_glob / kb_grep / kb_read 仍然
    直接扫文件系统，Agent 照样能读到并引用这份"已删除"的文档；只移文件的话，
    切片还留在向量库里。两条检索路径各堵一条才算真的删掉。
    """
    document = await knowledge_document_crud.get(db, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    title = document.title
    file_path = document.file_path
    if not await knowledge_document_crud.soft_delete(db, document_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    await log_audit(
        db,
        current_user.id,
        "delete_knowledge_document",
        target=f"knowledge_document:{document_id}",
        detail=f"删除知识文档: {title}",
        ip=get_client_ip(request),
    )
    # **先动盘再提交**：删除的目的就是让 Agent 立刻看不到它。如果放在 commit 之后
    # 而移动失败，库里已标删、文件却还留在 KNOWLEDGE_ROOT 里，kb_grep 照样读得到
    # ——正是这个功能要防的情况。反过来先搬文件、提交失败再搬回来，最坏也只是
    # 这次删除没生效，不会留下"已删除但仍可被检索"的文档。
    move_document_to_trash(file_path)
    try:
        await db.commit()
    except Exception:
        restore_document_from_trash(file_path)
        raise
    return success_response(None, message="已移入回收站")


@router.post(
    "/documents/{document_id}/restore",
    response_model=ResponseEnvelope[KnowledgeDocumentResponse],
)
async def restore_document(
    document_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:manage")),
) -> ResponseEnvelope[KnowledgeDocumentResponse]:
    """从回收站恢复文档；同样内容已被重新上传时返回 409。"""
    document = await knowledge_document_crud.get_deleted(db, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="回收站中不存在该文档"
        )

    conflict = await knowledge_document_crud.active_with_content_hash(
        db, document.content_hash, exclude_id=document_id
    )
    if conflict is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"同样内容的文档已重新上传：《{conflict.title}》(ID {conflict.id})，"
                "恢复会产生重复。请先删除那一份再恢复。"
            ),
        )

    restored = await knowledge_document_crud.restore(db, document_id)
    if restored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="回收站中不存在该文档"
        )

    await log_audit(
        db,
        current_user.id,
        "restore_knowledge_document",
        target=f"knowledge_document:{document_id}",
        detail=f"恢复知识文档: {restored.title}",
        ip=get_client_ip(request),
    )
    restore_document_from_trash(restored.file_path)
    try:
        await db.commit()
    except Exception:
        move_document_to_trash(restored.file_path)
        raise
    return success_response(
        KnowledgeDocumentResponse.model_validate(restored), message="恢复成功"
    )


@router.delete("/documents/{document_id}/purge", response_model=ResponseEnvelope[None])
async def purge_document(
    document_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:manage")),
) -> ResponseEnvelope[None]:
    """永久删除回收站中的文档：行、切片与正文文件一并清除，不可恢复。"""
    document = await knowledge_document_crud.get_deleted(db, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="回收站中不存在该文档"
        )

    title = document.title
    file_path = document.file_path
    # 切片靠 knowledge_chunks 的 ON DELETE CASCADE 一起消失
    await knowledge_document_crud.hard_delete(db, document_id)
    await log_audit(
        db,
        current_user.id,
        "purge_knowledge_document",
        target=f"knowledge_document:{document_id}",
        detail=f"永久删除知识文档: {title}",
        ip=get_client_ip(request),
    )
    await db.commit()
    purge_document_from_trash(file_path)
    return success_response(None, message="已永久删除")


@router.patch(
    "/documents/{document_id}/category",
    response_model=ResponseEnvelope[KnowledgeDocumentResponse],
)
async def update_document_category(
    document_id: int,
    body: KnowledgeDocumentCategoryUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:manage")),
) -> ResponseEnvelope[KnowledgeDocumentResponse]:
    """把文档归到指定分类（采纳 AI 建议或人工覆盖），并清空已消费的建议。"""
    document = await knowledge_document_crud.get(db, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    category = await knowledge_category_crud.get(db, body.category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分类不存在")

    previous_category_id = document.category_id
    previous_path = document.file_path
    # 提前取好旧分类 code：补偿要用它，而补偿发生在 commit 失败之后，
    # 那时再去查库等于拿一个刚出过错的会话冒险
    previous_category = await knowledge_category_crud.get(db, previous_category_id)
    previous_code = previous_category.code if previous_category else None
    adopted_suggestion = document.suggested_category_id == body.category_id
    await knowledge_document_crud.apply_category(db, document, body.category_id)
    # 文件必须跟着分类走：kb_glob / kb_grep 按目录限定分类，只改 category_id
    # 会让文件工具和向量检索对"这份文档在哪个分类"给出相反答案。
    document.file_path = move_document_to_category(
        previous_path,
        category_code=category.code,
        document_id=document.id,
        filename=document.original_filename,
    )
    await log_audit(
        db,
        current_user.id,
        "update_knowledge_document_category",
        target=f"knowledge_document:{document.id}",
        detail=(
            f"分类 {previous_category_id} → {body.category_id}"
            f"（{'采纳 AI 建议' if adopted_suggestion else '人工指定'}）"
        ),
        ip=get_client_ip(request),
    )
    try:
        await db.commit()
    except Exception:
        # 文件已经搬走了，提交却失败：把它搬回原处，别留下库与盘不一致
        if previous_code is not None:
            move_document_to_category(
                document.file_path,
                category_code=previous_code,
                document_id=document.id,
                filename=document.original_filename,
            )
        raise
    return success_response(
        KnowledgeDocumentResponse.model_validate(document), message="已更新分类"
    )


@router.post(
    "/documents/classify",
    response_model=ResponseEnvelope[KnowledgeClassifyResponse],
)
async def classify_documents_endpoint(
    body: KnowledgeClassifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:manage")),
) -> ResponseEnvelope[KnowledgeClassifyResponse]:
    """为选中的文档生成 AI 分类建议。

    只写建议字段，不改变文档当前归属——用户在管理页确认后才会真正归类。
    单份直接调模型，多份走并行编排工作流。
    """
    outcome = await suggest_categories(
        db, body.document_ids, actor_user_id=current_user.id
    )
    await log_audit(
        db,
        current_user.id,
        "suggest_knowledge_document_categories",
        target=f"knowledge_document:{','.join(str(i) for i in body.document_ids[:10])}",
        detail=f"请求 {len(body.document_ids)} 份，生成建议 {outcome.suggested} 份",
        ip=get_client_ip(request),
    )
    await db.commit()
    return success_response(
        KnowledgeClassifyResponse(
            suggested=outcome.suggested,
            skipped=outcome.skipped,
            unchanged=outcome.unchanged,
            no_match=outcome.no_match,
        ),
        message=f"已生成 {outcome.suggested} 份建议",
    )
