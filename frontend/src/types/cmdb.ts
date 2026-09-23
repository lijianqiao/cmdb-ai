/** CMDB 资产相关类型 */

export type CredentialType = "none" | "static" | "dynamic"

/** 厂商标识。有哪些值由后端命令目录接口给出（/device-commands/catalog），前端不再抄一份 */
export type VendorName = string

/** CMDB 资产（列表/详情响应） */
export interface CmdbAsset {
  id: number
  asset_type: string
  vendor: VendorName
  hostname: string
  ip_address: string
  location: string
  owner_user_id: number | null
  business_system: string
  subnet_cidr: string
  notes: string
  credential_type: CredentialType
  credential_username: string
  credential_password_set: boolean
  /** 管理口 SSH 端口，默认 22 */
  ssh_port: number
  created_at: string
  updated_at: string
}

/** 创建资产请求 */
export interface CmdbAssetCreate {
  asset_type: string
  vendor: VendorName
  hostname: string
  ip_address: string
  location?: string
  owner_user_id?: number | null
  business_system?: string
  subnet_cidr?: string
  notes?: string
  ssh_port?: number
  credential_type?: CredentialType
  credential_username?: string
  credential_password?: string | null
}

/** 更新资产请求（部分字段） */
export interface CmdbAssetUpdate {
  asset_type?: string
  vendor?: VendorName
  hostname?: string
  ip_address?: string
  location?: string
  owner_user_id?: number | null
  business_system?: string
  subnet_cidr?: string
  notes?: string
  ssh_port?: number
  credential_type?: CredentialType
  credential_username?: string
  credential_password?: string | null
}

/** 资产查询参数 */
export interface CmdbAssetQueryParams {
  page?: number
  page_size?: number
  search?: string
  asset_type?: string | null
  business_system?: string | null
}
