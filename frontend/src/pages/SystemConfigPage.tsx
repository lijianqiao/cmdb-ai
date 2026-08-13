/** 系统配置管理页

 * 加载 LLM 与运行参数配置，分别由两个卡片编辑保存。
 * 权限由路由与侧边栏统一控制，本页不再重复校验。
 */

import { useCallback, useEffect, useState } from "react"

import { LlmConfigCard } from "@/components/system-config/LlmConfigCard"
import { OperationsConfigCard } from "@/components/system-config/OperationsConfigCard"
import { PageHeader } from "@/components/layout/PageHeader"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { getSystemConfig } from "@/lib/system-config-api"
import type { SystemConfigData } from "@/types/system-config"

export function SystemConfigPage() {
  const [config, setConfig] = useState<SystemConfigData | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  const fetchConfig = useCallback(async () => {
    setIsLoading(true)
    setLoadError(null)
    try {
      const data = await getSystemConfig()
      setConfig(data)
    } catch {
      setConfig(null)
      setLoadError("系统配置加载失败，请稍后重试。")
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    void fetchConfig()
  }, [fetchConfig])

  const handleSaved = (next: SystemConfigData) => {
    setConfig(next)
  }

  return (
    <div>
      <PageHeader
        title="系统配置"
        description="管理模型服务、HITL 与监控巡检的运行参数"
      />

      {isLoading ? (
        <div className="flex flex-col gap-6">
          {Array.from({ length: 2 }).map((_, index) => (
            <Card key={index}>
              <CardHeader>
                <Skeleton className="h-6 w-32" />
                <Skeleton className="mt-2 h-4 w-64" />
              </CardHeader>
              <CardContent className="grid gap-4 sm:grid-cols-2">
                {Array.from({ length: 4 }).map((__, fieldIndex) => (
                  <Skeleton key={fieldIndex} className="h-16 w-full" />
                ))}
              </CardContent>
            </Card>
          ))}
        </div>
      ) : loadError ? (
        <Alert variant="destructive">
          <AlertTitle>加载失败</AlertTitle>
          <AlertDescription className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <span>{loadError}</span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void fetchConfig()}
            >
              重新加载
            </Button>
          </AlertDescription>
        </Alert>
      ) : config ? (
        <div className="flex flex-col gap-6">
          <LlmConfigCard value={config.llm} onSaved={handleSaved} />
          <OperationsConfigCard
            value={config.operations}
            onSaved={handleSaved}
          />
        </div>
      ) : null}
    </div>
  )
}
