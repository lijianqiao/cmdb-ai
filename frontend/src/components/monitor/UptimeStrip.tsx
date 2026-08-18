/**
 * 可用率状态条：最近一小时、每分钟一格。
 *
 * 数据由监控目标列表接口一并返回（`uptime_window`），所以这个组件是纯展示的——
 * 不发请求、不算时间窗，拿到什么画什么。一次列表请求就能渲染整页的条，
 * 不会退化成逐行追加请求。
 *
 * 三个刻意的选择：
 * 1. **没有探测的格子是灰的，不是绿的**。画成绿色等于告诉运维「那段时间正常」，
 *    而真相是「那段时间没测」——目标可能刚建好，也可能探测器本身挂了。
 * 2. **没有数据时右上角显示「—」而不是 100%**，同理。
 * 3. **格子数固定 60**，条宽不随数据多少变化，多行之间才能目视对齐。
 */

import type { MonitorBucketState, MonitorUptimeWindow } from "@/types/monitor"

const BUCKET_STYLES: Record<MonitorBucketState, string> = {
  up: "bg-emerald-500",
  down: "bg-red-500",
  // 灰色且更淡：一眼能看出「这里没有数据」，而不是某种状态
  unknown: "bg-muted-foreground/25",
}

const BUCKET_LABELS: Record<MonitorBucketState, string> = {
  up: "正常",
  down: "失败",
  unknown: "无探测数据",
}

/** 把一格的序号换算成它对应的那一分钟，用于 tooltip */
function bucketTime(data: MonitorUptimeWindow, index: number): string {
  const startedAt = new Date(data.started_at).getTime()
  const at = new Date(startedAt + index * data.bucket_seconds * 1000)
  return at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
}

export function UptimeStrip({ data }: { data: MonitorUptimeWindow }) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">最近 1 小时</p>
        <p className="text-xs font-medium tabular-nums">
          {data.uptime_rate == null
            ? "—"
            : `${(data.uptime_rate * 100).toFixed(2)}% 可用率`}
        </p>
      </div>
      <div className="flex items-stretch gap-[2px]" role="img" aria-label="可用率状态条">
        {data.buckets.map((state, index) => (
          <div
            key={index}
            data-testid="uptime-bucket"
            title={`${bucketTime(data, index)} · ${BUCKET_LABELS[state]}`}
            className={`h-6 min-w-0 flex-1 rounded-[2px] ${BUCKET_STYLES[state]}`}
          />
        ))}
      </div>
    </div>
  )
}
