/** 运维助手输入框

 * InputGroup：左侧审批档位选择器 + Textarea + 发送按钮同一行、等高对齐；Enter 发送、Shift+Enter 换行。
 */

import { useState, type KeyboardEvent } from "react"

import { SentIcon } from "@/lib/icons"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import type { ApprovalMode } from "@/types/agent"
import { APPROVAL_MODE_LABELS } from "@/types/agent"

/** base-ui 的 Select 需要 items 才能在受控赋值时渲染选中项文案 */
const APPROVAL_MODE_ITEMS = (
  Object.entries(APPROVAL_MODE_LABELS) as [ApprovalMode, string][]
).map(([value, label]) => ({ value, label }))

export interface ChatInputProps {
  disabled?: boolean
  isSending?: boolean
  placeholder?: string
  approvalMode: ApprovalMode | null
  onApprovalModeSelect: (mode: ApprovalMode) => void
  onSend: (content: string) => void | Promise<void>
}

/**
 * 聊天输入与发送按钮（单行、等高）。
 *
 * Args:
 *   disabled: 禁用输入（无会话或发送中由页面传入）
 *   isSending: 显示发送中 Spinner
 *   placeholder: 占位文案
 *   approvalMode: 当前会话已保存的审批档位
 *   onApprovalModeSelect: 用户选择新档位（由页面处理 PATCH / 确认弹窗）
 *   onSend: 提交非空正文
 */
export function ChatInput({
  disabled = false,
  isSending = false,
  placeholder = "输入消息，Enter 发送，Shift+Enter 换行",
  approvalMode,
  onApprovalModeSelect,
  onSend,
}: ChatInputProps) {
  const [value, setValue] = useState("")

  const submit = async (): Promise<void> => {
    const trimmed = value.trim()
    if (!trimmed || disabled || isSending) return
    setValue("")
    await onSend(trimmed)
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key !== "Enter" || event.shiftKey) return
    event.preventDefault()
    void submit()
  }

  return (
    <InputGroup className="h-auto min-h-10 items-center border border-border bg-background">
      <InputGroupAddon
        align="inline-start"
        className="h-10 shrink-0 items-center self-center py-0 pl-1.5"
      >
        <Select
          items={APPROVAL_MODE_ITEMS}
          value={approvalMode ?? undefined}
          onValueChange={(next) => {
            if (next) onApprovalModeSelect(next as ApprovalMode)
          }}
          disabled={approvalMode == null}
        >
          <SelectTrigger
            size="sm"
            className="h-8 w-[7.5rem] border-0 bg-transparent px-2 shadow-none"
            aria-label="审批模式"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {APPROVAL_MODE_ITEMS.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      </InputGroupAddon>
      <InputGroupTextarea
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        rows={1}
        className="max-h-32 min-h-10 field-sizing-content py-2.5 leading-5 md:text-sm"
        aria-label="消息输入"
      />
      <InputGroupAddon
        align="inline-end"
        className="h-10 shrink-0 items-center self-center py-0 pr-1.5"
      >
        <InputGroupButton
          type="button"
          variant="default"
          size="xs"
          className="h-8 gap-1.5 rounded-md px-2.5 text-sm"
          disabled={disabled || isSending || !value.trim()}
          onClick={() => void submit()}
          aria-label="发送"
        >
          {isSending ? (
            <Spinner data-icon="inline-start" />
          ) : (
            <SentIcon data-icon="inline-start" />
          )}
          发送
        </InputGroupButton>
      </InputGroupAddon>
    </InputGroup>
  )
}
