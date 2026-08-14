import type { VendorName } from "@/types/cmdb"

/** 厂商枚举值须与后端 app/agent/device_commands.py::VendorName 手动保持一致。 */
export const VENDOR_VALUES = [
  "cisco_iosxe",
  "cisco_small_business",
  "huawei_vrp",
  "hp_comware",
  "juniper_junos",
  "linux",
  "generic",
] as const satisfies readonly VendorName[]

export const VENDOR_ITEMS: { label: string; value: VendorName }[] = [
  { label: "通用", value: "generic" },
  { label: "思科 IOS-XE", value: "cisco_iosxe" },
  {
    label: "思科 Small Business（SG350X 等）",
    value: "cisco_small_business",
  },
  { label: "华为 VRP", value: "huawei_vrp" },
  { label: "H3C Comware", value: "hp_comware" },
  { label: "Juniper Junos", value: "juniper_junos" },
  { label: "Linux", value: "linux" },
]

export function isVendorName(value: string | undefined): value is VendorName {
  return typeof value === "string" && VENDOR_VALUES.includes(value as VendorName)
}
