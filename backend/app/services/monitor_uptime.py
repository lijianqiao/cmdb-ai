"""把原始探测事件折叠成「最近一小时 × 每分钟一格」的可用率条。

实现流程：
1. 监控目标页要画一条状态条（形如公有云状态页那种绿红小格）。数据源是
   monitor_status_events 里的原始探测记录——探测间隔可低至 5 秒，
   所以一个分钟格里可能有十几次探测，需要折叠。
2. 这个模块**只做纯计算**：给它一批 (状态, 时间) 和窗口起点，返回 60 个格子。
   不连库，也不在内部读「现在几点」——窗口起点由调用方传入。
   时间相关的逻辑最忌讳内部读 now()，那会让测试变成掷骰子。
3. 三个判断落实在这里：
   - 一格里**只要有一次失败就是红**。监控界面宁可多报，也不能把抖动藏掉。
   - **没有探测的格子是 unknown（灰），不是绿**。把「没测」画成「正常」是撒谎：
     目标可能刚建好，也可能探测器本身挂了。
   - **uptime_rate 按原始探测数算，不按格子算**。若按格子算，4 次探测挤在
     1 格里错 1 次就成了「1 格全红 = 0%」，格子粒度会凭空放大故障。
     窗口内没有任何探测时返回 None 而不是 1.0——否则一个从没跑过的目标
     会显示「100% 可用」。
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


def build_uptime_window(
    events: Sequence[tuple[str, datetime]],
    *,
    window_start: datetime,
) -> UptimeWindow:
    """把窗口内的探测事件折叠成 60 个分钟格。

    Args:
        events: (status, checked_at) 序列，顺序任意；窗口外的会被忽略
        window_start: 窗口起点（含），终点为起点 + 1 小时（不含）

    Returns:
        定长 60 格的状态条与可用率。格子数固定，前端才能无脑渲染。
    """
    buckets: list[BucketState] = ["unknown"] * BUCKET_COUNT
    probe_total = 0
    probe_up = 0

    for status, raw_checked_at in events:
        # SQLite 取回的时间是 naive 的，PostgreSQL 是 aware 的（列声明都是
        # DateTime(timezone=True)，但 SQLite 驱动不保留时区）。单测跑 SQLite、
        # 生产跑 PostgreSQL，不归一化就会「单测全绿、上线抛异常」。
        # 与 spawn/receipts.py、agent_sessions.py 的处理一致。
        checked_at = (
            raw_checked_at.replace(tzinfo=UTC)
            if raw_checked_at.tzinfo is None
            else raw_checked_at
        )
        offset = checked_at - window_start
        if offset < timedelta(0) or offset >= WINDOW:
            continue
        index = int(offset.total_seconds()) // BUCKET_SECONDS

        probe_total += 1
        if status == "up":
            probe_up += 1
            # 已经红了的格子不能被后来的成功探测洗白
            if buckets[index] == "unknown":
                buckets[index] = "up"
        else:
            buckets[index] = "down"

    return UptimeWindow(
        started_at=window_start,
        bucket_seconds=BUCKET_SECONDS,
        buckets=buckets,
        uptime_rate=(probe_up / probe_total) if probe_total else None,
    )
