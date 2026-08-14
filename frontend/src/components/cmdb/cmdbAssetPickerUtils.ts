/** CMDB 资产选择器纯函数与类型（非 React 组件） */

export interface CmdbAssetOption {
  id: number
  hostname: string
  ip_address: string
}

export function formatCmdbAssetOption(asset: CmdbAssetOption): string {
  return `#${asset.id} ${asset.hostname}（${asset.ip_address}）`
}
