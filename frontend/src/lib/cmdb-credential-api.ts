/** CMDB 静态凭据 API 封装（纯函数，非 React 组件） */

import api from "@/lib/api"
import type { ApiResponse } from "@/types/api"

/** 拉取已保存的静态凭据明文（不写回编辑表单） */
export async function fetchCmdbAssetCredential(assetId: number): Promise<string> {
  const response = await api.get<ApiResponse<{ password: string }>>(
    `/cmdb/assets/${assetId}/credential`
  )
  return response.data.data.password
}
