/** CMDB 资产搜索选择器

 * 通过 /cmdb/assets 按主机名/IP 搜索，展示「#ID 主机名（IP）」。
 * 需要 cmdb:read；无权限时给出说明，不回退成纯数字输入。
 */

import { useEffect, useMemo, useState } from "react"
import { isAxiosError } from "axios"

import {
  Combobox,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
  ComboboxList,
} from "@/components/ui/combobox"
import api from "@/lib/api"
import type { ApiResponse, PaginatedData } from "@/types/api"
import type { CmdbAsset } from "@/types/cmdb"

export interface CmdbAssetOption {
  id: number
  hostname: string
  ip_address: string
}

export function formatCmdbAssetOption(asset: CmdbAssetOption): string {
  return `#${asset.id} ${asset.hostname}（${asset.ip_address}）`
}

interface CmdbAssetPickerProps {
  id?: string
  value: number | null
  onChange: (assetId: number | null) => void
  allowClear?: boolean
  invalid?: boolean
  disabled?: boolean
  placeholder?: string
}

function toOption(asset: CmdbAsset): CmdbAssetOption {
  return {
    id: asset.id,
    hostname: asset.hostname,
    ip_address: asset.ip_address,
  }
}

export function CmdbAssetPicker({
  id,
  value,
  onChange,
  allowClear = true,
  invalid = false,
  disabled = false,
  placeholder = "搜索主机名、IP 或资产 ID",
}: CmdbAssetPickerProps) {
  const [query, setQuery] = useState("")
  const [items, setItems] = useState<CmdbAssetOption[]>([])
  const [selected, setSelected] = useState<CmdbAssetOption | null>(null)
  const [forbidden, setForbidden] = useState(false)

  useEffect(() => {
    if (value == null) {
      setSelected(null)
      return
    }
    if (selected?.id === value) return

    let cancelled = false
    const loadSelected = async () => {
      try {
        const response = await api.get<ApiResponse<CmdbAsset>>(`/cmdb/assets/${value}`)
        const asset = response.data.data
        if (!cancelled && asset) {
          setSelected(toOption(asset))
        }
      } catch (error) {
        if (isAxiosError(error) && error.response?.status === 403) {
          setForbidden(true)
        }
      }
    }
    void loadSelected()
    return () => {
      cancelled = true
    }
  }, [value, selected?.id])

  useEffect(() => {
    let cancelled = false
    const timer = window.setTimeout(() => {
      const search = query.trim()
      void (async () => {
        try {
          const response = await api.get<ApiResponse<PaginatedData<CmdbAsset>>>(
            "/cmdb/assets",
            {
              params: {
                page: 1,
                page_size: 20,
                ...(search ? { search } : {}),
              },
            },
          )
          if (cancelled) return
          setForbidden(false)
          const next = (response.data.data?.items ?? []).map(toOption)
          setItems(next)
        } catch (error) {
          if (cancelled) return
          if (isAxiosError(error) && error.response?.status === 403) {
            setForbidden(true)
            setItems([])
          }
        }
      })()
    }, 300)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [query])

  const options = useMemo(() => {
    if (!selected) return items
    const rest = items.filter((item) => item.id !== selected.id)
    return [selected, ...rest]
  }, [items, selected])

  if (forbidden) {
    return (
      <p className="text-sm text-muted-foreground">
        当前账号没有 CMDB 查看权限，无法搜索资产。请联系管理员开通
        cmdb:read。
      </p>
    )
  }

  return (
    <Combobox
      items={options}
      value={selected}
      onValueChange={(next) => {
        const asset = next as CmdbAssetOption | null
        setSelected(asset)
        onChange(asset?.id ?? null)
      }}
      onInputValueChange={setQuery}
      itemToStringValue={formatCmdbAssetOption}
      filter={() => true}
      disabled={disabled}
    >
      <ComboboxInput
        id={id}
        className="w-full"
        placeholder={placeholder}
        showClear={allowClear}
        aria-invalid={invalid}
        disabled={disabled}
      />
      <ComboboxContent>
        <ComboboxEmpty>没有匹配的资产</ComboboxEmpty>
        <ComboboxList>
          {(asset: CmdbAssetOption) => (
            <ComboboxItem key={asset.id} value={asset}>
              {formatCmdbAssetOption(asset)}
            </ComboboxItem>
          )}
        </ComboboxList>
      </ComboboxContent>
    </Combobox>
  )
}
