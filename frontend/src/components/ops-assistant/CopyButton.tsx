/** 通用 Markdown / 文本复制按钮组件
 *
 * 点击写入系统剪贴板，成功后切换为打勾图标并弹出 Toast 提示。
 */

import { useState, type ComponentProps } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Copy01Icon, CopyCheckIcon } from "@/lib/icons"
import { cn } from "@/lib/utils"

export interface CopyButtonProps
  extends Omit<ComponentProps<typeof Button>, "onClick"> {
  text: string
  label?: string
  successMessage?: string
  errorMessage?: string
}

export function CopyButton({
  text,
  label = "复制 Markdown 内容",
  successMessage = "已复制 Markdown 内容",
  errorMessage = "复制失败，请手动选择复制",
  className,
  variant = "ghost",
  size = "sm",
  children,
  ...props
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!text) return

    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        // 兼容降级
        const textarea = document.createElement("textarea")
        textarea.value = text
        textarea.style.position = "fixed"
        textarea.style.opacity = "0"
        document.body.appendChild(textarea)
        textarea.select()
        document.execCommand("copy")
        document.body.removeChild(textarea)
      }
      setCopied(true)
      toast.success(successMessage)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      toast.error(errorMessage)
    }
  }

  return (
    <Button
      type="button"
      variant={variant}
      size={size}
      onClick={handleCopy}
      className={cn(
        children ? "gap-1 px-2 text-xs" : "size-7 p-0",
        "text-muted-foreground transition-colors hover:text-foreground",
        copied && "text-emerald-600 dark:text-emerald-400 hover:text-emerald-600",
        className,
      )}
      title={copied ? "已复制" : label}
      aria-label={copied ? "已复制" : label}
      {...props}
    >
      {copied ? (
        <CopyCheckIcon className="size-3.5" />
      ) : (
        <Copy01Icon className="size-3.5" />
      )}
      {children}
    </Button>
  )
}
