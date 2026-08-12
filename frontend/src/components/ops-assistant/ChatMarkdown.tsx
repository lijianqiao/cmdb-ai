/** 聊天消息 Markdown 渲染

 * 将助手回复以阅读模式展示（标题、列表、代码块、表格等），
 * 使用 react-markdown + remark-gfm；样式走语义 token。
 */

import type { Components } from "react-markdown"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"

import { cn } from "@/lib/utils"

export interface ChatMarkdownProps {
  content: string
  className?: string
}

/** 丢掉 hast node，避免落到 DOM 上变成 node="[object Object]" */
function withoutNode<T extends { node?: unknown }>(
  props: T,
): Omit<T, "node"> {
  const { node: _node, ...rest } = props
  return rest
}

const markdownComponents: Components = {
  h1: ({ children, ...props }) => (
    <h1
      className="mt-3 mb-2 border-b border-border pb-1 text-base font-semibold tracking-tight first:mt-0"
      {...withoutNode(props)}
    >
      {children}
    </h1>
  ),
  h2: ({ children, ...props }) => (
    <h2
      className="mt-3 mb-1.5 text-base font-semibold tracking-tight first:mt-0"
      {...withoutNode(props)}
    >
      {children}
    </h2>
  ),
  h3: ({ children, ...props }) => (
    <h3
      className="mt-2.5 mb-1 text-sm font-semibold first:mt-0"
      {...withoutNode(props)}
    >
      {children}
    </h3>
  ),
  p: ({ children, ...props }) => (
    <p className="my-1.5 first:mt-0 last:mb-0" {...withoutNode(props)}>
      {children}
    </p>
  ),
  ul: ({ children, ...props }) => (
    <ul
      className="my-1.5 list-disc space-y-1 pl-5 marker:text-muted-foreground"
      {...withoutNode(props)}
    >
      {children}
    </ul>
  ),
  ol: ({ children, ...props }) => (
    <ol
      className="my-1.5 list-decimal space-y-1 pl-5 marker:text-muted-foreground"
      {...withoutNode(props)}
    >
      {children}
    </ol>
  ),
  li: ({ children, ...props }) => (
    <li className="leading-relaxed" {...withoutNode(props)}>
      {children}
    </li>
  ),
  strong: ({ children, ...props }) => (
    <strong className="font-semibold" {...withoutNode(props)}>
      {children}
    </strong>
  ),
  em: ({ children, ...props }) => (
    <em className="italic" {...withoutNode(props)}>
      {children}
    </em>
  ),
  a: ({ href, children, ...props }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-primary underline underline-offset-2"
      {...withoutNode(props)}
    >
      {children}
    </a>
  ),
  blockquote: ({ children, ...props }) => (
    <blockquote
      className="my-2 border-l-2 border-border pl-3 text-muted-foreground"
      {...withoutNode(props)}
    >
      {children}
    </blockquote>
  ),
  hr: (props) => <hr className="my-3 border-border" {...withoutNode(props)} />,
  code: ({ className: codeClassName, children, ...props }) => {
    const isBlock = Boolean(codeClassName?.includes("language-"))
    if (isBlock) {
      return (
        <code
          className={cn("font-mono text-xs leading-relaxed", codeClassName)}
          {...withoutNode(props)}
        >
          {children}
        </code>
      )
    }
    return (
      <code
        className="rounded-md bg-muted px-1 py-0.5 font-mono text-[0.85em]"
        {...withoutNode(props)}
      >
        {children}
      </code>
    )
  },
  pre: ({ children, ...props }) => (
    <pre
      className="my-2 overflow-x-auto rounded-lg border bg-muted/80 p-3 font-mono text-xs leading-relaxed"
      {...withoutNode(props)}
    >
      {children}
    </pre>
  ),
  table: ({ children, ...props }) => (
    <div className="my-2 overflow-x-auto rounded-lg border">
      <table
        className="w-full border-collapse text-left text-xs"
        {...withoutNode(props)}
      >
        {children}
      </table>
    </div>
  ),
  thead: ({ children, ...props }) => (
    <thead className="border-b bg-muted/60" {...withoutNode(props)}>
      {children}
    </thead>
  ),
  th: ({ children, ...props }) => (
    <th className="px-2.5 py-1.5 font-medium" {...withoutNode(props)}>
      {children}
    </th>
  ),
  td: ({ children, ...props }) => (
    <td className="border-t px-2.5 py-1.5" {...withoutNode(props)}>
      {children}
    </td>
  ),
}

/**
 * 渲染 Markdown 正文（聊天气泡内）。
 *
 * Args:
 *   content: Markdown 源文本
 *   className: 外层 class
 */
export function ChatMarkdown({ content, className }: ChatMarkdownProps) {
  if (!content) return null

  return (
    <div
      className={cn(
        "chat-markdown text-sm leading-relaxed break-words text-card-foreground",
        className,
      )}
      data-testid="chat-markdown"
    >
      <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
        {content}
      </Markdown>
    </div>
  )
}
