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

/** 上传后的知识文档 */
export interface KnowledgeDocument {
  id: number
  category_id: number
  title: string
  original_filename: string
  file_path: string
  file_type: string
  status: string
  created_at: string
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
  formData.append("category_code", categoryCode)
  formData.append("title", title)
  formData.append("file", file)

  const response = await api.post<ApiResponse<KnowledgeDocument>>(
    "/knowledge/documents",
    formData,
  )
  return response.data.data
}
