"""Knowledge-base request/response schemas."""

from datetime import datetime

from pydantic import ConfigDict, Field

from app.schemas.common import ApiModel


class KnowledgeCategoryCreate(ApiModel):
    """Request body for creating a category."""

    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)


class KnowledgeCategoryResponse(ApiModel):
    """A category as returned to clients."""

    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    id: int
    code: str
    name: str
    description: str
    created_at: datetime


class KnowledgeDocumentResponse(ApiModel):
    """A document as returned to clients after upload."""

    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    id: int
    category_id: int
    title: str
    original_filename: str
    file_path: str
    file_type: str
    status: str
    created_at: datetime
    # 回收站按删除时间排序、并展示这一列；软删除只改 is_deleted，
    # 所以 updated_at 就是删除发生的时刻
    updated_at: datetime
    # AI 分类建议；未生成或已被应用时全部为空。
    suggested_category_id: int | None = None
    suggestion_confidence: float | None = None
    suggestion_reason: str = ""
    suggested_at: datetime | None = None


class KnowledgeDocumentCategoryUpdate(ApiModel):
    """把一份文档归到指定分类（采纳建议或人工覆盖）。"""

    category_id: int = Field(ge=1)


class KnowledgeClassifyRequest(ApiModel):
    """为一批文档生成 AI 分类建议。"""

    document_ids: list[int] = Field(min_length=1, max_length=50)


class KnowledgeClassifyResponse(ApiModel):
    """建议生成结果统计。"""

    suggested: int
    skipped: int
    # 模型认为当前分类就是对的：既不是建议也不是失败，单列一档才说得清
    unchanged: int = 0


class KnowledgeDocumentContentResponse(ApiModel):
    """文档正文预览。

    只读接口，正文按字符窗口返回。`truncated` 为真时前端要提示"仅预览前 N 字符"，
    否则用户会以为文档就这么长。
    """

    document_id: int
    title: str
    file_type: str
    content: str
    total_chars: int
    offset: int
    truncated: bool
