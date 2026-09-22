/** 前端命令目录副本：跟后端 app/agent/device_commands.py 手动保持一致 */

import { describe, expect, it } from "vitest"

import {
  DEVICE_COMMAND_NAMES,
  isStateChangingCommand,
  STATE_CHANGING_COMMAND_NAMES,
} from "./device-command-policy"

describe("device-command-policy 命令目录副本", () => {
  it("与后端目录一致：整机关机 shutdown 已随主机厂商下线", () => {
    expect(DEVICE_COMMAND_NAMES).toEqual([
      "show_version",
      "show_running_config",
      "show_interfaces",
      "ping",
      "reboot",
      "port_enable",
      "port_disable",
    ])
    expect([...STATE_CHANGING_COMMAND_NAMES]).toEqual([
      "reboot",
      "port_enable",
      "port_disable",
    ])
    expect(isStateChangingCommand("shutdown")).toBe(false)
  })
})
