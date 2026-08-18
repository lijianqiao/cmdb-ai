"""把状态变化日志还原成「最近一小时 × 每分钟一格」的可用率条。

实现流程：
1. **monitor_status_events 不是探测流水，是状态变化日志。** 见
   `crud.monitor_status_event.record_probe`：同状态就地更新当前行的 checked_at，
   变状态才追加新行。一个持续在线一小时的目标在表里**只有 1 行**，
   而那行的 checked_at 会随每次探测不断往前推。
2. 所以不能把行当成独立的探测点去分桶——那样整条图只有最后一格有颜色。
   必须还原成**阶跃函数**：第 i 行的状态覆盖 `(上一行.checked_at, 本行.checked_at]`，
   最早那行往前一直覆盖到 `known_since`。
3. 覆盖范围有两个边界，越界的格子是 unknown（灰）而不是绿：
   - 往后：最后一行的 checked_at 之后还没探测过。
   - 往前：`known_since`（目标创建时间）之前这个目标还不存在。
4. uptime_rate 按**时间加权**：处于 up 的时长 / 有覆盖的时长。按行数算没有意义
   ——行数是状态变化次数，不是探测次数。

已知限制：这个模型分不出「一直正常」和「巡检器挂了没测」。两者在表里都表现为
「checked_at 长时间不动」，而中间那段空白无从判断。要区分就得另存探测流水，
那是另一笔存储开销，当前不做。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

type BucketState = Literal["up", "down", "unknown"]

BUCKET_SECONDS = 60
BUCKET_COUNT = 60
WINDOW = timedelta(seconds=BUCKET_SECONDS * BUCKET_COUNT)


@dataclass(frozen=True, slots=True)
class UptimeWindow:
    """一条状态条：60 个定长格子 + 窗口元信息 + 可用率。"""

    started_at: datetime
    bucket_seconds: int
    buckets: list[BucketState]
    uptime_rate: float | None


def _as_utc(value: datetime) -> datetime:
    """SQLite 取回的时间是 naive 的，PostgreSQL 是 aware 的。

    列声明都是 DateTime(timezone=True)，但 SQLite 驱动不保留时区。单测跑
    SQLite、生产跑 PostgreSQL，不归一化就会「单测全绿、上线抛异常」。
    与 spawn/receipts.py、agent_sessions.py 的处理一致。
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def build_uptime_window(
    events: Sequence[tuple[str, datetime]],
    *,
    window_start: datetime,
    known_since: datetime,
) -> UptimeWindow:
    """把状态变化行还原成 60 个分钟格。

    Args:
        events: (status, checked_at) 序列，顺序任意。每条代表一次**状态变化**，
            checked_at 是该状态最后一次被观测到的时刻。
        window_start: 窗口起点（含），终点为起点 + 1 小时（不含）。
        known_since: 该目标从何时起存在（通常是 created_at）。此前的格子是
            unknown——目标那时还没建，画成任何状态都是编的。

    Returns:
        定长 60 格的状态条与时间加权的可用率。
    """
    ordered = sorted(
        ((status, _as_utc(checked_at)) for status, checked_at in events),
        key=lambda item: item[1],
    )
    window_end = window_start + WINDOW
    floor = max(_as_utc(known_since), window_start)

    # 还原阶跃：第 i 段 = (上一行的 checked_at, 本行的 checked_at]，
    # 最早那行往前一直延伸到 floor。
    segments: list[tuple[datetime, datetime, str]] = []
    previous_end = floor
    for status, checked_at in ordered:
        if checked_at > previous_end:
            segments.append((previous_end, min(checked_at, window_end), status))
        previous_end = max(previous_end, checked_at)

    buckets: list[BucketState] = ["unknown"] * BUCKET_COUNT
    covered = timedelta(0)
    up_time = timedelta(0)

    for start, end, status in segments:
        if end <= window_start or start >= window_end:
            continue
        clipped_start = max(start, window_start)
        clipped_end = min(end, window_end)
        if clipped_end <= clipped_start:
            continue

        covered += clipped_end - clipped_start
        if status == "up":
            up_time += clipped_end - clipped_start

        first = int((clipped_start - window_start).total_seconds()) // BUCKET_SECONDS
        # 减 1 微秒：正好落在格子边界上的结束时间属于前一格，不该染到下一格
        last = (
            int((clipped_end - timedelta(microseconds=1) - window_start).total_seconds())
            // BUCKET_SECONDS
        )
        for index in range(max(first, 0), min(last, BUCKET_COUNT - 1) + 1):
            # 已经红了的格子不能被后来的 up 段洗白：一格里既有失败又有恢复时判红，
            # 监控界面宁可多报，不能把抖动藏掉。
            if status != "up":
                buckets[index] = "down"
            elif buckets[index] == "unknown":
                buckets[index] = "up"

    return UptimeWindow(
        started_at=window_start,
        bucket_seconds=BUCKET_SECONDS,
        buckets=buckets,
        uptime_rate=(up_time / covered) if covered else None,
    )
