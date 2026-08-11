"""Knowledge-base routes: category management and document upload."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_client_ip, require_permission
from app.crud.knowledge_category import knowledge_category_crud
from app.models.user import User
from app.schemas.common import ResponseEnvelope, success_response
from app.schemas.knowledge import (
    KnowledgeCategoryCreate,
    KnowledgeCategoryResponse,
    KnowledgeDocumentResponse,
)
from app.services.knowledge_ingestion import DuplicateDocumentError, ingest_document
from app.utils.audit import log_audit

router = APIRouter()

_SUPPORTED_FILE_TYPES = {"md", "txt"}


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
    category_code: str = Form(...),
    title: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:upload")),
) -> ResponseEnvelope[KnowledgeDocumentResponse]:
    """Upload a document (.md/.txt only), chunk it, embed it, and store it."""
    filename = file.filename or "unnamed"
    file_type = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if file_type not in _SUPPORTED_FILE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"不支持的文件类型: .{file_type}（仅支持 .md/.txt）",
        )

    category = await knowledge_category_crud.get_by_code(db, category_code)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分类不存在")

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
            status_code=status.HTTP_409_CONFLICT, detail=f"内容重复的文档已存在: id={exc.document_id}"
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
