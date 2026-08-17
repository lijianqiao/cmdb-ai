/** 知识库 REST 封装（复用 `@/lib/api`，不新建 axios 实例） */

import api from "@/lib/api"
import type { ApiResponse } from "@/types/api"

/** 知识库分类 */
export interface KnowledgeCategory {
  id: number
  code: string
  name: string
  description: string
  created_at: string
}

/** 知识文档。`suggested_*` 是待人工确认的 AI 建议，未生成或已应用时为空 */
export interface KnowledgeDocument {
  id: number
  category_id: number
  title: string
  original_filename: string
  file_path: string
  file_type: string
  status: string
  created_at: string
  suggested_category_id: number | null
  suggestion_confidence: number | null
  suggestion_reason: string
  suggested_at: string | null
}

/**
 * 列出全部知识库分类。
 *
 * 需要 `knowledge:read`；无权限时接口返回 403。
 *
 * Returns:
 *   分类数组
 */
export async function listCategories(): Promise<KnowledgeCategory[]> {
  const response = await api.get<ApiResponse<KnowledgeCategory[]>>(
    "/knowledge/categories",
  )
  return response.data.data
}

/** 创建分类请求体 */
export interface KnowledgeCategoryCreate {
  code: string
  name: string
  description?: string
}

/**
 * 创建知识库分类。
 *
 * 需要 `knowledge:manage`（超管可绕过）。
 *
 * Args:
 *   body: code / name / 可选 description
 *
 * Returns:
 *   新建分类
 */
export async function createCategory(
  body: KnowledgeCategoryCreate,
): Promise<KnowledgeCategory> {
  const response = await api.post<ApiResponse<KnowledgeCategory>>(
    "/knowledge/categories",
    body,
  )
  return response.data.data
}

/**
 * 上传知识文档（multipart）。
 *
 * 字段：`category_code`、`title`、`file`（仅 .md/.txt）。
 * 勿手动设置 Content-Type，由浏览器带 multipart boundary。
 *
 * Args:
 *   categoryCode: 分类代码
 *   title: 文档标题
 *   file: 本地文件
 *
 * Returns:
 *   上传成功后的文档
 */
export async function uploadDocument(
  categoryCode: string,
  title: string,
  file: File,
): Promise<KnowledgeDocument> {
  const formData = new FormData()
  // 空字符串表示「不指定分类」，后端会落到「未分类」等待归类。
  if (categoryCode) formData.append("category_code", categoryCode)
  formData.append("title", title)
  formData.append("file", file)

  const response = await api.post<ApiResponse<KnowledgeDocument>>(
    "/knowledge/documents",
    formData,
  )
  return response.data.data
}

/**
 * 把文档归到指定分类（采纳 AI 建议或人工覆盖）。
 *
 * 需要 `knowledge:manage`。应用后该文档的建议会被清空。
 *
 * Args:
 *   documentId: 文档 ID
 *   categoryId: 目标分类 ID
 *
 * Returns:
 *   更新后的文档
 */
export async function applyDocumentCategory(
  documentId: number,
  categoryId: number,
): Promise<KnowledgeDocument> {
  const response = await api.patch<ApiResponse<KnowledgeDocument>>(
    `/knowledge/documents/${documentId}/category`,
    { category_id: categoryId },
  )
  return response.data.data
}

/** 批量建议结果统计 */
export interface KnowledgeClassifyResult {
  suggested: number
  skipped: number
}

/**
 * 为选中的文档生成 AI 分类建议。
 *
 * 需要 `knowledge:manage`。只写建议，不改变文档当前归属——
 * 真正归类要用户在列表里点「应用」。
 *
 * Args:
 *   documentIds: 文档 ID 列表（1~50 个）
 *
 * Returns:
 *   生成与跳过的份数
 */
export async function classifyDocuments(
  documentIds: number[],
): Promise<KnowledgeClassifyResult> {
  const response = await api.post<ApiResponse<KnowledgeClassifyResult>>(
    "/knowledge/documents/classify",
    { document_ids: documentIds },
  )
  return response.data.data
}
