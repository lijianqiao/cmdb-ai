/** 动态凭据口令输入框
 *
 * 动态凭据是一次性的设备登录口令：可能是 6 位 OTP，也可能更长、含字母和符号，
 * 甚至带合法的首尾空白。所以这里用普通掩码密码框，长度与后端一致（1 至 256），
 * 输入什么就交出什么，不截断、不 trim；是否全为空白只由调用方用来判断能否提交。
 * 可以切换显示/隐藏，但口令只存在组件状态里，不写入任何浏览器存储。
 */

import { useState } from "react"

import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group"
import { ViewIcon, ViewOffSlashIcon } from "@/lib/icons"

/** 与后端 HitlDecideRequest / HitlRetryRequest 的 max_length 保持一致 */
const DYNAMIC_PASSWORD_MAX_LENGTH = 256

interface DynamicPasswordInputProps {
  id: string
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  testId?: string
}

export function DynamicPasswordInput({
  id,
  value,
  onChange,
  disabled,
  testId,
}: DynamicPasswordInputProps) {
  const [visible, setVisible] = useState(false)

  return (
    <InputGroup>
      <InputGroupInput
        id={id}
        type={visible ? "text" : "password"}
        // 一次性口令：提示浏览器不要保存或自动填充
        autoComplete="one-time-code"
        maxLength={DYNAMIC_PASSWORD_MAX_LENGTH}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        data-testid={testId}
      />
      <InputGroupAddon align="inline-end">
        <InputGroupButton
          type="button"
          size="icon-xs"
          aria-label={visible ? "隐藏密码" : "显示密码"}
          aria-pressed={visible}
          onClick={() => setVisible((prev) => !prev)}
        >
          {visible ? <ViewOffSlashIcon /> : <ViewIcon />}
        </InputGroupButton>
      </InputGroupAddon>
    </InputGroup>
  )
}
