/** 厂商的中文显示名。
 *
 * 有哪些厂商由后端目录接口说了算（见 hooks/use-device-command-catalog）；这里只管显示。
 * 后端新接一个厂商时，没登记标签就直接显示厂商值本身，页面不会少一个选项。
 */
const VENDOR_LABELS: Record<string, string> = {
  cisco_iosxe: "思科 IOS-XE",
  cisco_small_business: "思科 Small Business（SG350X 等）",
  huawei_vrp: "华为 VRP",
  hp_comware: "H3C / HP Comware",
  juniper_junos: "Juniper Junos",
  // 暂不支持的网络设备厂商先以它登记：能进台账和依赖图，但不能下发任何命令。
  other: "其他 / 未指定",
}

/** 没登记标签的厂商原样显示值本身。 */
export function vendorLabel(value: string): string {
  return VENDOR_LABELS[value] ?? value
}

/** 把目录返回的厂商值列表变成下拉项。 */
export function vendorItems(
  vendors: readonly string[]
): { label: string; value: string }[] {
  return vendors.map((value) => ({ label: vendorLabel(value), value }))
}
