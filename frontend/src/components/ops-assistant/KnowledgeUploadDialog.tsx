/** 知识库文档上传对话框

 * 打开时拉取分类列表；仅有 upload 无 read 时 categories 会 403，需友好提示。
 * 有 knowledge:manage（含超管）时可在本对话框内联新建分类。
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
import { usePermission } from "@/hooks/use-permission"
import { PERMISSIONS } from "@/lib/constants"
import {
  createCategory,
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

const createCategorySchema = z.object({
  code: z
    .string()
    .min(1, "请输入分类代码")
    .max(50, "代码最多 50 个字符")
    .regex(/^[a-zA-Z0-9_-]+$/, "代码仅允许字母、数字、下划线与连字符"),
  name: z.string().min(1, "请输入分类名称").max(100, "名称最多 100 个字符"),
})

type CreateCategoryValues = z.infer<typeof createCategorySchema>

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
 * 知识库上传对话框：分类（可内联新建）+ 标题 + 文件。
 */
export function KnowledgeUploadDialog({
  open,
  onOpenChange,
}: KnowledgeUploadDialogProps) {
  const { hasPermission } = usePermission()
  const canManageCategory = hasPermission(PERMISSIONS.KNOWLEDGE_MANAGE)

  const [categories, setCategories] = useState<KnowledgeCategory[]>([])
  const [categoriesLoading, setCategoriesLoading] = useState(false)
  const [categoriesForbidden, setCategoriesForbidden] = useState(false)
  const [showCreateCategory, setShowCreateCategory] = useState(false)
  const [creatingCategory, setCreatingCategory] = useState(false)

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      category_code: "",
      title: "",
      file: undefined,
    },
  })

  const createForm = useForm<CreateCategoryValues>({
    resolver: zodResolver(createCategorySchema),
    defaultValues: { code: "", name: "" },
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
    createForm.reset({ code: "", name: "" })
    setCategories([])
    setCategoriesForbidden(false)
    setShowCreateCategory(false)
    setCategoriesLoading(true)

    void listCategories()
      .then((rows) => {
        setCategories(rows)
        setCategoriesForbidden(false)
        // 无分类且有管理权限时，默认展开新建区，减少一次点击
        if (rows.length === 0 && canManageCategory) {
          setShowCreateCategory(true)
        }
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
  }, [open, form, createForm, canManageCategory])

  const handleCreateCategory = async (
    data: CreateCategoryValues,
  ): Promise<void> => {
    if (creatingCategory) return
    setCreatingCategory(true)
    try {
      const created = await createCategory({
        code: data.code.trim(),
        name: data.name.trim(),
      })
      setCategories((prev) => {
        if (prev.some((row) => row.code === created.code)) return prev
        return [...prev, created]
      })
      form.setValue("category_code", created.code, { shouldValidate: true })
      createForm.reset({ code: "", name: "" })
      setShowCreateCategory(false)
      toast.success("分类已创建")
    } catch (error: unknown) {
      toast.error(readErrorMessage(error, "创建分类失败"))
    } finally {
      setCreatingCategory(false)
    }
  }

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
                  <div className="flex items-center justify-between gap-2">
                    <FieldLabel htmlFor="knowledge-category">分类</FieldLabel>
                    {canManageCategory && !categoriesForbidden && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        disabled={categoriesLoading}
                        onClick={() =>
                          setShowCreateCategory((prev) => !prev)
                        }
                      >
                        {showCreateCategory ? "收起新建" : "新建分类"}
                      </Button>
                    )}
                  </div>
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
                      {canManageCategory
                        ? "暂无分类，请在下方新建后再上传。"
                        : "暂无可用分类，请先由管理员创建。"}
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

            {canManageCategory &&
              !categoriesForbidden &&
              showCreateCategory && (
                <div className="flex flex-col gap-3 rounded-lg border border-dashed p-3">
                  <p className="text-sm font-medium">新建分类</p>
                  <Controller
                    control={createForm.control}
                    name="code"
                    render={({ field, fieldState }) => (
                      <Field data-invalid={fieldState.invalid}>
                        <FieldLabel htmlFor="knowledge-category-code">
                          代码
                        </FieldLabel>
                        <Input
                          id="knowledge-category-code"
                          placeholder="例如 sop"
                          aria-invalid={fieldState.invalid}
                          {...field}
                        />
                        <FieldDescription>
                          字母、数字、下划线或连字符
                        </FieldDescription>
                        <FieldError errors={[fieldState.error]} />
                      </Field>
                    )}
                  />
                  <Controller
                    control={createForm.control}
                    name="name"
                    render={({ field, fieldState }) => (
                      <Field data-invalid={fieldState.invalid}>
                        <FieldLabel htmlFor="knowledge-category-name">
                          名称
                        </FieldLabel>
                        <Input
                          id="knowledge-category-name"
                          placeholder="例如 故障处理 SOP"
                          aria-invalid={fieldState.invalid}
                          {...field}
                        />
                        <FieldError errors={[fieldState.error]} />
                      </Field>
                    )}
                  />
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={creatingCategory}
                    onClick={() =>
                      void createForm.handleSubmit(handleCreateCategory)()
                    }
                  >
                    {creatingCategory && (
                      <Spinner data-icon="inline-start" />
                    )}
                    保存分类
                  </Button>
                </div>
              )}

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
