/** HitlApprovalCard 展示与动态凭据校验单测（纯函数，不跑完整组件渲染栈） */

import { describe, expect, it } from "vitest"

import {
  isApproveButtonDisabled,
  needsDynamicCredentialPassword,
  shouldShowResultExcerpt,
} from "./hitlApprovalCardUtils"

describe("HitlApprovalCard 执行结果展示", () => {
  it("EXECUTED 且 result_excerpt 有值时应展示", () => {
    expect(shouldShowResultExcerpt("EXECUTED", "show version")).toBe(true)
    expect(shouldShowResultExcerpt("executed", "  output  ")).toBe(true)
  })

  it("非 EXECUTED 或 result_excerpt 为空时不展示", () => {
    expect(shouldShowResultExcerpt("PENDING", "output")).toBe(false)
    expect(shouldShowResultExcerpt("EXECUTED", null)).toBe(false)
    expect(shouldShowResultExcerpt("EXECUTED", "   ")).toBe(false)
  })
})

describe("HitlApprovalCard 动态凭据密码", () => {
  it("device_query + dynamic 凭据时需要密码输入", () => {
    expect(needsDynamicCredentialPassword("device_query", "dynamic")).toBe(true)
  })

  it("其它动作类型或凭据类型不需要密码输入", () => {
    expect(needsDynamicCredentialPassword("device_query", "static")).toBe(false)
    expect(needsDynamicCredentialPassword("notify", "dynamic")).toBe(false)
    expect(needsDynamicCredentialPassword("device_query", null)).toBe(false)
  })

  it("device_query + dynamic 时密码为空应禁用批准按钮", () => {
    expect(isApproveButtonDisabled(false, false, true, "")).toBe(true)
    expect(isApproveButtonDisabled(false, false, true, "   ")).toBe(true)
  })

  it("填写密码后应允许批准（未在加载/提交中）", () => {
    expect(isApproveButtonDisabled(false, false, true, "secret")).toBe(false)
  })

  it("不需要动态密码时不因密码为空而禁用", () => {
    expect(isApproveButtonDisabled(false, false, false, "")).toBe(false)
  })

  it("加载或提交中始终禁用批准", () => {
    expect(isApproveButtonDisabled(true, false, false, "x")).toBe(true)
    expect(isApproveButtonDisabled(false, true, false, "x")).toBe(true)
  })
})
