/** 运维助手输入框

 * InputGroup：Textarea 与发送按钮同一行；Enter 发送、Shift+Enter 换行。
 */

import { useState, type KeyboardEvent } from "react"

import { SentIcon } from "@/lib/icons"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group"
import { Spinner } from "@/components/ui/spinner"

export interface ChatInputProps {
  disabled?: boolean
  isSending?: boolean
  placeholder?: string
  onSend: (content: string) => void | Promise<void>
}

/**
 * 聊天输入与发送按钮（单行布局）。
 *
 * Args:
 *   disabled: 禁用输入（无会话或发送中由页面传入）
 *   isSending: 显示发送中 Spinner
 *   placeholder: 占位文案
 *   onSend: 提交非空正文
 */
export function ChatInput({
  disabled = false,
  isSending = false,
  placeholder = "输入消息，Enter 发送，Shift+Enter 换行",
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
    <InputGroup className="h-auto min-h-9 items-end border border-border bg-background">
      <InputGroupTextarea
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        rows={1}
        className="max-h-32 min-h-9 py-2"
        aria-label="消息输入"
      />
      <InputGroupAddon align="inline-end" className="shrink-0 self-end py-1.5">
        <InputGroupButton
          type="button"
          variant="default"
          size="sm"
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
