/** 知识文档正文预览抽屉
 *
 * .md 走 Markdown 渲染，.txt 保持等宽原样输出——把纯文本喂给 Markdown 渲染器
 * 会吞掉缩进、把行首的 # 和 - 当成标题和列表，看到的就不是文件真正的样子了。
 */

import { useEffect, useState } from "react"
import { toast } from "sonner"

import { ChatMarkdown } from "@/components/ops-assistant/ChatMarkdown"
import { Badge } from "@/components/ui/badge"
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from "@/components/ui/drawer"
import { Spinner } from "@/components/ui/spinner"
import { Book02Icon } from "@/lib/icons"
import {
  getDocumentContent,
  type KnowledgeDocument,
  type KnowledgeDocumentContent,
} from "@/lib/knowledge-api"

const CHAR_FORMAT = new Intl.NumberFormat("zh-CN")

export interface DocumentPreviewDrawerProps {
  document: KnowledgeDocument | null
  onClose: () => void
}

export function DocumentPreviewDrawer({
  document,
  onClose,
}: DocumentPreviewDrawerProps) {
  const [content, setContent] = useState<KnowledgeDocumentContent | null>(null)
  const [isLoading, setIsLoading] = useState(false)

  const documentId = document?.id ?? null

  useEffect(() => {
    if (documentId == null) {
      setContent(null)
      return
    }
    let cancelled = false
    setIsLoading(true)
    setContent(null)
    getDocumentContent(documentId)
      .then((data) => {
        // 抽屉在请求途中被关掉或换了一份文档时，丢弃这次结果，
        // 否则慢的那次会覆盖掉当前展示的内容
        if (!cancelled) setContent(data)
      })
      .catch(() => {
        if (!cancelled) toast.error("读取文档正文失败")
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [documentId])

  const isMarkdown = content?.file_type === "md"

  return (
    <Drawer
      swipeDirection="right"
      open={document !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DrawerContent className="w-[92vw] sm:w-[560px] md:w-[720px] max-w-full overflow-x-hidden">
        <DrawerHeader className="border-b pb-4">
          <div className="flex items-center gap-2">
            <div className="flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Book02Icon />
            </div>
            <DrawerTitle className="text-base font-semibold">
              {document?.title ?? "文档预览"}
            </DrawerTitle>
          </div>
          <DrawerDescription className="flex flex-wrap items-center gap-2">
            <span>{document?.original_filename}</span>
            {content ? (
              <span className="tabular-nums">
                · {CHAR_FORMAT.format(content.total_chars)} 字符
              </span>
            ) : null}
            {content?.truncated ? (
              <Badge variant="outline">
                仅预览前 {CHAR_FORMAT.format(content.content.length)} 字符
              </Badge>
            ) : null}
          </DrawerDescription>
        </DrawerHeader>

        <div className="flex-1 overflow-y-auto px-4 py-4">
          {isLoading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Spinner className="size-4" />
              正在读取正文…
            </div>
          ) : content == null ? (
            <p className="text-sm text-muted-foreground">暂无可展示的正文。</p>
          ) : isMarkdown ? (
            <div className="text-sm">
              <ChatMarkdown content={content.content} />
            </div>
          ) : (
            <pre className="text-xs leading-5 whitespace-pre-wrap break-words font-mono">
              {content.content}
            </pre>
          )}
        </div>
      </DrawerContent>
    </Drawer>
  )
}
