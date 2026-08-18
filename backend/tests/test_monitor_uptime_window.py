"""把状态变化日志还原成「一小时 × 每分钟一格」的可用率条。

实现流程：
1. **monitor_status_events 不是探测流水，是状态变化日志。** 见
   `crud.monitor_status_event.record_probe`：同状态就地更新当前行的 checked_at，
   变状态才追加新行。所以一个持续在线一小时的目标在表里**只有 1 行**。
2. 因此不能把行当成独立的探测点去分桶——那样只有最后一格有颜色，其余全灰，
   整条图等于废掉。必须把行还原成**阶跃函数**：第 i 行的状态覆盖
   `(上一行.checked_at, 本行.checked_at]`，最早那行往前一直覆盖到有记录之初。
3. 覆盖范围有两个边界，越界的格子是 unknown（灰）而不是绿：
   - 往后：最后一行的 checked_at 之后还没探测过。
   - 往前：`known_since`（目标创建时间）之前这个目标还不存在。
4. uptime_rate 按**时间加权**：窗口内处于 up 的时长 / 有覆盖的时长。
   按行数算是没意义的——行数是状态变化次数，不是探测次数。
"""

from datetime import UTC, datetime, timedelta

from app.services.monitor_uptime import BUCKET_COUNT, build_uptime_window

WINDOW_START = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
LONG_AGO = WINDOW_START - timedelta(days=1)


def _at(minute: int, second: int = 0) -> datetime:
    return WINDOW_START + timedelta(minutes=minute, seconds=second)


def test_window_always_has_sixty_buckets() -> None:
    """格子数固定，前端才能无脑渲染定长的条。"""
    window = build_uptime_window([], window_start=WINDOW_START, known_since=LONG_AGO)

    assert len(window.buckets) == BUCKET_COUNT == 60


def test_no_events_means_unknown_not_up() -> None:
    """一行都没有 = 从没探测过。画成绿色等于撒谎。"""
    window = build_uptime_window([], window_start=WINDOW_START, known_since=LONG_AGO)

    assert set(window.buckets) == {"unknown"}
    assert window.uptime_rate is None


def test_single_up_row_paints_the_whole_covered_window_green() -> None:
    """**这是最常见也是之前画错的场景。**

    持续在线的目标在表里只有 1 行，checked_at 就是最近一次探测时间。
    这一行代表「从有记录以来一直是 up，最后一次确认是在 checked_at」，
    所以从窗口起点到 checked_at 全都该是绿的——而不是只有最后一格绿。
    """
    events = [("up", _at(59, 30))]

    window = build_uptime_window(
        events, window_start=WINDOW_START, known_since=LONG_AGO
    )

    assert set(window.buckets) == {"up"}
    assert window.uptime_rate == 1.0


def test_buckets_after_the_last_observation_are_unknown() -> None:
    """最后一次探测之后的时间还没被观测，不能当成正常。"""
    events = [("up", _at(20, 0))]

    window = build_uptime_window(
        events, window_start=WINDOW_START, known_since=LONG_AGO
    )

    assert window.buckets[19] == "up"
    assert window.buckets[21] == "unknown"


def test_buckets_before_the_target_existed_are_unknown() -> None:
    """目标 30 分钟前才建，之前那半小时不该被画成任何状态。"""
    events = [("up", _at(59, 0))]

    window = build_uptime_window(
        events, window_start=WINDOW_START, known_since=_at(30, 0)
    )

    assert window.buckets[0] == "unknown"
    assert window.buckets[29] == "unknown"
    assert window.buckets[31] == "up"


def test_state_change_splits_the_strip() -> None:
    """down 一直持续到它被 up 取代那一刻，之后才转绿。"""
    events = [("down", _at(20, 0)), ("up", _at(59, 0))]

    window = build_uptime_window(
        events, window_start=WINDOW_START, known_since=LONG_AGO
    )

    assert window.buckets[0] == "down"
    assert window.buckets[19] == "down"
    assert window.buckets[30] == "up"


def test_change_inside_one_bucket_makes_it_down() -> None:
    """一格里既有失败又有恢复时判红。监控界面宁可多报，不能把抖动藏掉。"""
    events = [("down", _at(10, 20)), ("up", _at(59, 0))]

    window = build_uptime_window(
        events, window_start=WINDOW_START, known_since=LONG_AGO
    )

    assert window.buckets[10] == "down"


def test_uptime_rate_is_time_weighted_not_row_counted() -> None:
    """按时长算：前 30 分钟 down、后 30 分钟 up → 0.5。

    按行数算是没意义的——行数是状态变化次数，不是探测次数。
    这里只有 2 行，按行数算会得出 0.5 纯属巧合；把 down 段拉长就会露馅。
    """
    events = [("down", _at(30, 0)), ("up", _at(60, 0))]

    window = build_uptime_window(
        events, window_start=WINDOW_START, known_since=LONG_AGO
    )

    assert window.uptime_rate == 0.5


def test_uptime_rate_ignores_uncovered_time() -> None:
    """只探测过前 15 分钟就全绿的目标是 100%，不该被后面 45 分钟的空白拉低。"""
    events = [("up", _at(15, 0))]

    window = build_uptime_window(
        events, window_start=WINDOW_START, known_since=LONG_AGO
    )

    assert window.uptime_rate == 1.0


def test_events_are_sorted_before_reconstruction() -> None:
    """调用方按 checked_at 倒序取最近 N 行，这里必须自己排好再还原。"""
    events = [("up", _at(59, 0)), ("down", _at(20, 0))]

    window = build_uptime_window(
        events, window_start=WINDOW_START, known_since=LONG_AGO
    )

    assert window.buckets[0] == "down"
    assert window.buckets[40] == "up"


def test_window_metadata_lets_the_frontend_label_each_bucket() -> None:
    """前端要能算出每格对应的时间，才能做 tooltip。"""
    window = build_uptime_window([], window_start=WINDOW_START, known_since=LONG_AGO)

    assert window.started_at == WINDOW_START
    assert window.bucket_seconds == 60


def test_naive_timestamps_are_treated_as_utc() -> None:
    """SQLite 取回的 checked_at 是 naive 的，PostgreSQL 是 aware 的。

    列声明是 DateTime(timezone=True)，但 SQLite 驱动不保留时区。
    单测跑 SQLite、生产跑 PostgreSQL，这个函数必须两种都吃得下——
    否则单测全绿、上线就抛「can't subtract offset-naive and offset-aware」。
    项目里 spawn/receipts.py 与 agent_sessions.py 用的是同一套归一化。
    """
    naive = datetime(2026, 8, 18, 10, 30)  # noqa: DTZ001 - 刻意构造 naive，见上

    window = build_uptime_window(
        [("up", naive)], window_start=WINDOW_START, known_since=LONG_AGO
    )

    assert window.buckets[0] == "up"
    assert window.buckets[29] == "up"
