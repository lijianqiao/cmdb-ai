/** CMDB 资产管理页
 *
 * DataTable + 搜索/资产类型筛选 + 新增/编辑/删除/回收站，结构照抄 UsersPage.tsx。
 */

import { useEffect, useMemo, useState } from "react"
import { Link, useSearchParams } from "react-router"
import type { ColumnDef } from "@tanstack/react-table"
import dayjs from "dayjs"
import { toast } from "sonner"

import {
  PlusSignIcon,
  PencilEdit02Icon,
  Delete02Icon,
  InboxIcon,
  MoreHorizontalIcon,
  ViewIcon,
} from "@/lib/icons"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { PageHeader } from "@/components/layout/PageHeader"
import { DataTable } from "@/components/common/DataTable"
import { Pagination } from "@/components/common/Pagination"
import { ConfirmDialog } from "@/components/common/ConfirmDialog"
import { CmdbAssetFormDialog, CmdbCredentialRevealDialog, fetchCmdbAssetCredential } from "@/components/cmdb/CmdbAssetFormDialog"
import api from "@/lib/api"
import { usePaginatedQuery } from "@/hooks/use-paginated-query"
import { usePermission } from "@/hooks/use-permission"
import { PERMISSIONS, ROUTES } from "@/lib/constants"
import type { CmdbAsset, CmdbAssetCreate, CmdbAssetUpdate } from "@/types/cmdb"

export function CmdbAssetsPage() {
  const { hasPermission } = usePermission()
  const [searchParams, setSearchParams] = useSearchParams()
  const [search, setSearch] = useState("")

  const {
    items: assets,
    total,
    page,
    setPage,
    pageSize,
    isLoading,
    onPageSizeChange,
    refetch: fetchAssets,
  } = usePaginatedQuery<CmdbAsset>({
    url: "/cmdb/assets",
    params: search ? { search } : {},
    errorMessage: "获取资产列表失败",
  })

  const [formOpen, setFormOpen] = useState(false)
  const [editingAsset, setEditingAsset] = useState<CmdbAsset | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleteAsset, setDeleteAsset] = useState<CmdbAsset | null>(null)
  const [credentialDialogOpen, setCredentialDialogOpen] = useState(false)
  const [credentialAsset, setCredentialAsset] = useState<CmdbAsset | null>(null)
  const [revealedPassword, setRevealedPassword] = useState("")

  const handleCreate = () => {
    setEditingAsset(null)
    setFormOpen(true)
  }

  useEffect(() => {
    if (
      searchParams.get("create") === "1" &&
      hasPermission(PERMISSIONS.CMDB_MANAGE)
    ) {
      setEditingAsset(null)
      setFormOpen(true)
      const next = new URLSearchParams(searchParams)
      next.delete("create")
      setSearchParams(next, { replace: true })
    }
  }, [searchParams, setSearchParams, hasPermission])

  const handleEdit = (asset: CmdbAsset) => {
    setEditingAsset(asset)
    setFormOpen(true)
  }

  const handleDeleteClick = (asset: CmdbAsset) => {
    setDeleteAsset(asset)
    setDeleteOpen(true)
  }

  const handleCredentialDialogOpenChange = (open: boolean) => {
    setCredentialDialogOpen(open)
    if (!open) {
      setRevealedPassword("")
      setCredentialAsset(null)
    }
  }

  const handleViewCredential = async (asset: CmdbAsset) => {
    try {
      const password = await fetchCmdbAssetCredential(asset.id)
      setCredentialAsset(asset)
      setRevealedPassword(password)
      setCredentialDialogOpen(true)
    } catch {
      toast.error("查看密码失败")
    }
  }

  const handleSubmit = async (
    data: CmdbAssetCreate | CmdbAssetUpdate
  ): Promise<boolean> => {
    try {
      if (editingAsset) {
        await api.patch(`/cmdb/assets/${editingAsset.id}`, data)
        toast.success("更新成功")
      } else {
        await api.post("/cmdb/assets", data)
        toast.success("创建成功")
      }
      fetchAssets()
      return true
    } catch {
      toast.error(editingAsset ? "更新失败" : "创建失败")
      return false
    }
  }

  const handleDeleteConfirm = async (): Promise<boolean> => {
    if (!deleteAsset) return false
    try {
      await api.delete(`/cmdb/assets/${deleteAsset.id}`)
      toast.success("删除成功")
      fetchAssets()
      return true
    } catch {
      toast.error("删除失败")
      return false
    }
  }

  const columns = useMemo<ColumnDef<CmdbAsset>[]>(
    () => [
      { accessorKey: "hostname", header: "主机名" },
      { accessorKey: "ip_address", header: "IP 地址" },
      {
        accessorKey: "asset_type",
        header: "类型",
        cell: ({ row }) => {
          const labels: Record<string, string> = {
            server: "服务器",
            switch: "交换机",
            router: "路由器",
            firewall: "防火墙",
            load_balancer: "负载均衡",
            storage: "存储",
            other: "其他",
          }
          return labels[row.original.asset_type] ?? row.original.asset_type
        },
      },
      {
        accessorKey: "business_system",
        header: "业务系统",
        cell: ({ row }) => row.original.business_system || "-",
      },
      {
        id: "credential",
        header: "登录凭据",
        cell: ({ row }) => {
          const asset = row.original
          if (asset.credential_type === "none") {
            return <span className="text-muted-foreground">未配置</span>
          }
          return (
            <Badge variant="secondary">
              {asset.credential_type === "static" ? "静态密码" : "动态密码"}
              {asset.credential_type === "static" &&
                !asset.credential_password_set &&
                "（未设置）"}
            </Badge>
          )
        },
      },
      {
        accessorKey: "created_at",
        header: "创建时间",
        cell: ({ row }) =>
          dayjs(row.original.created_at).format("YYYY-MM-DD HH:mm"),
      },
      {
        id: "actions",
        header: "操作",
        cell: ({ row }) => (
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button variant="ghost" size="icon-sm" aria-label="更多操作" />
              }
            >
              <MoreHorizontalIcon />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                {hasPermission(PERMISSIONS.CMDB_MANAGE) && (
                  <DropdownMenuItem onClick={() => handleEdit(row.original)}>
                    <PencilEdit02Icon />
                    <span>编辑</span>
                  </DropdownMenuItem>
                )}
                {hasPermission(PERMISSIONS.CMDB_CREDENTIAL_READ) &&
                  row.original.credential_type === "static" &&
                  row.original.credential_password_set && (
                    <DropdownMenuItem
                      onClick={() => handleViewCredential(row.original)}
                    >
                      <ViewIcon />
                      <span>查看密码</span>
                    </DropdownMenuItem>
                  )}
                {hasPermission(PERMISSIONS.CMDB_MANAGE) && (
                  <DropdownMenuItem
                    onClick={() => handleDeleteClick(row.original)}
                    className="text-destructive"
                  >
                    <Delete02Icon />
                    <span>删除</span>
                  </DropdownMenuItem>
                )}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        ),
      },
    ],
    [hasPermission]
  )

  return (
    <div>
      <PageHeader
        title="CMDB 资产管理"
        description="维护设备台账与登录凭据"
        actions={
          <>
            {hasPermission(PERMISSIONS.CMDB_MANAGE) && (
              <Button
                variant="outline"
                render={<Link to={ROUTES.CMDB_TRASH} />}
              >
                <InboxIcon data-icon="inline-start" />
                回收站
              </Button>
            )}
            {hasPermission(PERMISSIONS.CMDB_MANAGE) && (
              <Button onClick={handleCreate}>
                <PlusSignIcon data-icon="inline-start" />
                新增资产
              </Button>
            )}
          </>
        }
      />

      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <Input
          placeholder="搜索主机名、IP 或业务系统..."
          value={search}
          onChange={(e) => {
            setSearch(e.target.value)
            setPage(1)
          }}
          className="sm:max-w-xs"
        />
      </div>

      <DataTable
        columns={columns}
        data={assets}
        isLoading={isLoading}
        emptyMessage="暂无资产数据"
      />

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={onPageSizeChange}
      />

      <CmdbAssetFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        asset={editingAsset}
        onSubmit={handleSubmit}
      />
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="确认删除资产"
        description={`确定要删除资产「${deleteAsset?.hostname}」吗？可在回收站恢复。`}
        onConfirm={handleDeleteConfirm}
      />
      <CmdbCredentialRevealDialog
        open={credentialDialogOpen}
        onOpenChange={handleCredentialDialogOpenChange}
        password={revealedPassword}
        assetHostname={credentialAsset?.hostname}
      />
    </div>
  )
}
