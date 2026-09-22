/** CMDB 资产类型：只登记网络设备，须与后端 app/schemas/cmdb.py::AssetTypeName 手动保持一致。 */
export const ASSET_TYPE_VALUES = [
  "switch",
  "router",
  "firewall",
  "wireless_controller",
  "other",
] as const

export const ASSET_TYPE_ITEMS: { label: string; value: string }[] = [
  { label: "交换机", value: "switch" },
  { label: "路由器", value: "router" },
  { label: "防火墙", value: "firewall" },
  { label: "无线控制器", value: "wireless_controller" },
  { label: "其他网络设备", value: "other" },
]

export function isAssetTypeName(value: string): boolean {
  return (ASSET_TYPE_VALUES as readonly string[]).includes(value)
}

/** 列表展示用的中文标签；清理前的旧类型（如 server）没有标签，原样显示值本身。 */
export function assetTypeLabel(value: string): string {
  return ASSET_TYPE_ITEMS.find((item) => item.value === value)?.label ?? value
}
