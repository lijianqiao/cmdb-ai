/** 知识库文档上传对话框

 * 打开时拉取分类列表；仅有 upload 无 read 时 categories 会 403，需友好提示。
 * 表单字段与后端一致：category_code / title / file（.md/.txt）。
 */

import { useEffect, useMemo, useState } from "react"
import { isAxiosError } from "axios"
import { Controller, useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { toast } from "sonner"
import { z } from "zod"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import {
  listCategories,
  uploadDocument,
  type KnowledgeCategory,
} from "@/lib/knowledge-api"

const schema = z.object({
  category_code: z.string().min(1, "请选择分类"),
  title: z.string().min(1, "请输入标题").max(200, "标题最多 200 个字符"),
  file: z
    .custom<File>((value) => value instanceof File, { message: "请选择文件" })
    .refine(
      (file) => /\.(md|txt)$/i.test(file.name),
      "仅支持 .md 或 .txt 文件",
    ),
})

type FormValues = z.infer<typeof schema>

interface KnowledgeUploadDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * 读取接口错误中的中文 message/detail。
 *
 * Args:
 *   error: 捕获的异常
 *   fallback: 默认文案
 *
 * Returns:
 *   可展示的错误字符串
 */
function readErrorMessage(error: unknown, fallback: string): string {
  if (!isAxiosError(error)) return fallback
  const data = error.response?.data
  if (data && typeof data === "object") {
    const message = (data as { message?: unknown }).message
    if (typeof message === "string" && message.trim()) return message
    const detail = (data as { detail?: unknown }).detail
    if (typeof detail === "string" && detail.trim()) return detail
  }
  return fallback
}

/**
 * 知识库上传对话框：分类 + 标题 + 文件。
 */
export function KnowledgeUploadDialog({
  open,
  onOpenChange,
}: KnowledgeUploadDialogProps) {
  const [categories, setCategories] = useState<KnowledgeCategory[]>([])
  const [categoriesLoading, setCategoriesLoading] = useState(false)
  const [categoriesForbidden, setCategoriesForbidden] = useState(false)

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      category_code: "",
      title: "",
      file: undefined,
    },
  })

  const categoryItems = useMemo(
    () =>
      categories.map((category) => ({
        value: category.code,
        label: category.name,
      })),
    [categories],
  )

  useEffect(() => {
    if (!open) return

    form.reset({
      category_code: "",
      title: "",
      file: undefined,
    })
    setCategories([])
    setCategoriesForbidden(false)
    setCategoriesLoading(true)

    void listCategories()
      .then((rows) => {
        setCategories(rows)
        setCategoriesForbidden(false)
      })
      .catch((error: unknown) => {
        const status = isAxiosError(error) ? error.response?.status : undefined
        if (status === 403) {
          setCategoriesForbidden(true)
          toast.error("需要知识库查看权限才能选择分类")
        } else {
          toast.error(readErrorMessage(error, "加载分类失败"))
        }
        setCategories([])
      })
      .finally(() => {
        setCategoriesLoading(false)
      })
  }, [open, form])

  const handleSubmit = async (data: FormValues): Promise<void> => {
    try {
      await uploadDocument(data.category_code, data.title, data.file)
      toast.success("上传成功")
      onOpenChange(false)
      form.reset({
        category_code: "",
        title: "",
        file: undefined,
      })
    } catch (error: unknown) {
      toast.error(readErrorMessage(error, "上传失败"))
    }
  }

  const canSubmit =
    !categoriesLoading &&
    !categoriesForbidden &&
    categories.length > 0 &&
    !form.formState.isSubmitting

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>上传知识文档</DialogTitle>
          <DialogDescription>
            上传 Markdown 或纯文本文件到知识库（仅支持 .md / .txt）
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={form.handleSubmit(handleSubmit)}>
          <FieldGroup>
            <Controller
              control={form.control}
              name="category_code"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="knowledge-category">分类</FieldLabel>
                  <Select
                    items={categoryItems}
                    value={field.value || null}
                    onValueChange={(value) => field.onChange(value ?? "")}
                    disabled={
                      categoriesLoading ||
                      categoriesForbidden ||
                      categories.length === 0
                    }
                  >
                    <SelectTrigger
                      id="knowledge-category"
                      className="w-full"
                      aria-invalid={fieldState.invalid}
                    >
                      <SelectValue placeholder="请选择分类" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {categoryItems.map((item) => (
                          <SelectItem key={item.value} value={item.value}>
                            {item.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                  {categoriesForbidden ? (
                    <FieldDescription>
                      需要知识库查看权限才能选择分类
                    </FieldDescription>
                  ) : categoriesLoading ? (
                    <FieldDescription>正在加载分类…</FieldDescription>
                  ) : categories.length === 0 ? (
                    <FieldDescription>
                      暂无可用分类，请先由管理员创建。
                    </FieldDescription>
                  ) : (
                    <FieldDescription>
                      选择文档所属的知识库分类
                    </FieldDescription>
                  )}
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />

            <Controller
              control={form.control}
              name="title"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="knowledge-title">标题</FieldLabel>
                  <Input
                    id="knowledge-title"
                    placeholder="请输入文档标题"
                    aria-invalid={fieldState.invalid}
                    {...field}
                  />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />

            <Controller
              control={form.control}
              name="file"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid}>
                  <FieldLabel htmlFor="knowledge-file">文件</FieldLabel>
                  <Input
                    id="knowledge-file"
                    type="file"
                    accept=".md,.txt,text/markdown,text/plain"
                    aria-invalid={fieldState.invalid}
                    onChange={(event) => {
                      const next = event.target.files?.[0]
                      field.onChange(next)
                    }}
                    onBlur={field.onBlur}
                    name={field.name}
                    ref={field.ref}
                  />
                  <FieldDescription>仅支持 .md 与 .txt</FieldDescription>
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={form.formState.isSubmitting}
              >
                取消
              </Button>
              <Button type="submit" disabled={!canSubmit}>
                {form.formState.isSubmitting && (
                  <Spinner data-icon="inline-start" />
                )}
                上传
              </Button>
            </DialogFooter>
          </FieldGroup>
        </form>
      </DialogContent>
    </Dialog>
  )
}
