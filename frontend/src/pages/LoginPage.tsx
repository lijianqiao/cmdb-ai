/** 登录页

 * 表单（react-hook-form + zod）+ 品牌区 + 响应式布局。
 */

import { useState } from "react"
import { useNavigate } from "react-router"
import { Controller, useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"
import { toast } from "sonner"

import { Shield02Icon, ViewIcon, ViewOffSlashIcon } from "@/lib/icons"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Field,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group"
import { Spinner } from "@/components/ui/spinner"
import { useAuth } from "@/hooks/use-auth"
import { ROUTES } from "@/lib/constants"

const loginSchema = z.object({
  username: z.string().min(1, "请输入用户名"),
  password: z.string().min(1, "请输入密码"),
})

type LoginFormData = z.infer<typeof loginSchema>

export function LoginPage() {
  const navigate = useNavigate()
  const { login, isLoading } = useAuth()
  const [error, setError] = useState<string | null>(null)
  const [showPassword, setShowPassword] = useState(false)

  const form = useForm<LoginFormData>({
    resolver: zodResolver(loginSchema),
    defaultValues: {
      username: "",
      password: "",
    },
  })

  const onSubmit = async (data: LoginFormData) => {
    setError(null)
    try {
      await login(data)
      toast.success("登录成功")
      navigate(ROUTES.DASHBOARD)
    } catch {
      const msg = "登录失败，请检查用户名和密码"
      setError(msg)
      toast.error(msg)
    }
  }

  return (
    <div className="flex min-h-svh">
      {/* 左侧品牌区（桌面端） */}
      <div className="hidden flex-1 flex-col justify-center bg-primary p-12 text-primary-foreground md:flex">
        <div className="mx-auto max-w-md">
          <Shield02Icon className="mb-6 size-12" />
          <h1 className="text-3xl font-bold">运维管理系统</h1>
          <p className="mt-4 text-lg text-primary-foreground/80">
            用 AI 助手查资产、探设备、审批变更命令
          </p>
          <div className="mt-8 flex flex-col gap-3 text-sm text-primary-foreground/70">
            <p>• 运维助手：自然语言排查与变更审批</p>
            <p>• CMDB：统一管理设备资产与登录凭据</p>
            <p>• 监控探活：按巡检间隔跟踪在线状态与延迟</p>
            <p>• 设备命令策略：白名单约束高风险操作</p>
          </div>
        </div>
      </div>

      {/* 右侧表单区 */}
      <div className="flex flex-1 items-center justify-center p-6">
        <Card className="w-full max-w-md">
          <CardHeader>
            <div className="mb-2 flex items-center gap-2 md:hidden">
              <Shield02Icon className="size-6 text-primary" />
              <span className="text-lg font-semibold">运维管理系统</span>
            </div>
            <CardTitle className="text-2xl">登录</CardTitle>
            <CardDescription>请输入您的账号和密码</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={form.handleSubmit(onSubmit)}>
              <FieldGroup>
                <Controller
                  control={form.control}
                  name="username"
                  render={({ field, fieldState }) => (
                    <Field data-invalid={fieldState.invalid}>
                      <FieldLabel htmlFor="login-username">用户名</FieldLabel>
                      <Input
                        id="login-username"
                        autoComplete="username"
                        placeholder="请输入用户名或邮箱"
                        aria-invalid={fieldState.invalid}
                        {...field}
                      />
                      <FieldError errors={[fieldState.error]} />
                    </Field>
                  )}
                />
                <Controller
                  control={form.control}
                  name="password"
                  render={({ field, fieldState }) => (
                    <Field data-invalid={fieldState.invalid}>
                      <FieldLabel htmlFor="login-password">密码</FieldLabel>
                      <InputGroup>
                        <InputGroupInput
                          id="login-password"
                          type={showPassword ? "text" : "password"}
                          autoComplete="current-password"
                          placeholder="请输入密码"
                          aria-invalid={fieldState.invalid}
                          {...field}
                        />
                        <InputGroupAddon align="inline-end">
                          <InputGroupButton
                            type="button"
                            size="icon-xs"
                            aria-label={showPassword ? "隐藏密码" : "显示密码"}
                            aria-pressed={showPassword}
                            onClick={() => setShowPassword((prev) => !prev)}
                          >
                            {showPassword ? (
                              <ViewOffSlashIcon />
                            ) : (
                              <ViewIcon />
                            )}
                          </InputGroupButton>
                        </InputGroupAddon>
                      </InputGroup>
                      <FieldError errors={[fieldState.error]} />
                    </Field>
                  )}
                />
                {error && (
                  <Alert variant="destructive">
                    <AlertTitle>登录失败</AlertTitle>
                    <AlertDescription>{error}</AlertDescription>
                  </Alert>
                )}
                <Button type="submit" className="w-full" disabled={isLoading}>
                  {isLoading && <Spinner data-icon="inline-start" />}
                  {isLoading ? "登录中" : "登录"}
                </Button>
              </FieldGroup>
            </form>
          </CardContent>
          <CardFooter className="justify-center">
            <p className="text-sm text-muted-foreground">仅管理员可创建账户</p>
          </CardFooter>
        </Card>
      </div>
    </div>
  )
}
