import type { VendorName } from "@/types/cmdb"

/** 厂商枚举值须与后端 app/agent/device_commands.py::VendorName 手动保持一致。 */
export const VENDOR_VALUES = [
  "cisco_iosxe",
  "cisco_small_business",
  "huawei_vrp",
  "hp_comware",
  "juniper_junos",
  "other",
] as const satisfies readonly VendorName[]

export const VENDOR_ITEMS: { label: string; value: VendorName }[] = [
  { label: "思科 IOS-XE", value: "cisco_iosxe" },
  {
    label: "思科 Small Business（SG350X 等）",
    value: "cisco_small_business",
  },
  { label: "华为 VRP", value: "huawei_vrp" },
  { label: "H3C / HP Comware", value: "hp_comware" },
  { label: "Juniper Junos", value: "juniper_junos" },
  // 暂不支持的网络设备厂商先以它登记：能进台账和依赖图，但不能下发任何命令。
  { label: "其他 / 未指定", value: "other" },
]

export function isVendorName(value: string | undefined): value is VendorName {
  return typeof value === "string" && VENDOR_VALUES.includes(value as VendorName)
}
