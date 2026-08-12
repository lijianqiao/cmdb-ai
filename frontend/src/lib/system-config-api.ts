/** 系统运行配置 REST 封装（复用 `@/lib/api`，不新建 axios 实例） */

import api from "@/lib/api"
import type { ApiResponse } from "@/types/api"
import type {
  LlmSystemConfigUpdate,
  OperationsSystemConfigUpdate,
  SystemConfigData,
} from "@/types/system-config"

/**
 * 解包统一响应信封中的 data 字段。
 *
 * Args:
 *   data: 响应体中的 data 字段
 *
 * Returns:
 *   非空的业务数据
 *
 * Raises:
 *   Error: data 缺失时
 */
function unwrapResponseData<T>(data: T | null | undefined): T {
  if (data == null) {
    throw new Error("接口响应缺少 data 字段")
  }
  return data
}

/**
 * 获取当前有效系统配置（LLM + 运行参数）。
 *
 * Returns:
 *   完整配置快照；API Key 仅含 configured/source，不回显明文
 */
export async function getSystemConfig(): Promise<SystemConfigData> {
  const response = await api.get<ApiResponse<SystemConfigData>>("/system-config")
  return unwrapResponseData(response.data.data)
}

/**
 * 更新 LLM 与 Embedding 配置。
 *
 * Args:
 *   payload: 非秘密字段必填；密钥留空表示保留，clear_* 表示显式清空
 *
 * Returns:
 *   保存后的完整配置快照
 */
export async function updateLlmSystemConfig(
  payload: LlmSystemConfigUpdate,
): Promise<SystemConfigData> {
  const response = await api.put<ApiResponse<SystemConfigData>>(
    "/system-config/llm",
    payload,
  )
  return unwrapResponseData(response.data.data)
}

/**
 * 更新 HITL 与监控运行参数。
 *
 * Args:
 *   payload: 四项运行参数
 *
 * Returns:
 *   保存后的完整配置快照
 */
export async function updateOperationsSystemConfig(
  payload: OperationsSystemConfigUpdate,
): Promise<SystemConfigData> {
  const response = await api.put<ApiResponse<SystemConfigData>>(
    "/system-config/operations",
    payload,
  )
  return unwrapResponseData(response.data.data)
}
