/** CMDB 资产相关类型 */

export type CredentialType = "none" | "static" | "dynamic"

/** 厂商标识，须与后端 app/agent/device_commands.py::VendorName 手动保持一致 */
export type VendorName =
  | "cisco_iosxe"
  | "cisco_small_business"
  | "huawei_vrp"
  | "hp_comware"
  | "juniper_junos"
  | "linux"
  | "generic"

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
