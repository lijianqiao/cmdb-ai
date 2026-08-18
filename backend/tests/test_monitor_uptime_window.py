"""把原始探测事件折叠成「一小时 × 每分钟一格」的可用率条。

实现流程：
1. 监控目标页要画一条状态条（形如公有云状态页）。数据源是 monitor_status_events
   的原始探测记录，探测间隔可低至 5 秒，所以一格里可能有多次探测。
2. 这个模块只做纯计算：给它一批事件和窗口起点，返回 60 个格子的状态与可用率。
   不连库、不碰时间"现在"（窗口起点由调用方传入），因此完全可测——
   时间相关的逻辑最忌讳内部读 now()，那会让测试变成掷骰子。
3. 三个判断在这里落实：
   - 一格里**只要有一次失败就是红**。监控界面宁可多报，也不能把抖动藏掉。
   - **没有探测的格子是 unknown（灰），不是绿**。把"没测"画成"正常"是撒谎。
   - **uptime_rate 按原始探测数算**，不是按格子算；窗口内没有任何探测时返回
     None 而不是 1.0——否则一个刚建好、根本没跑过的目标会显示「100% 可用」。
"""

from datetime import UTC, datetime, timedelta

from app.services.monitor_uptime import BUCKET_COUNT, build_uptime_window

WINDOW_START = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def _at(minute: int, second: int = 0) -> datetime:
    return WINDOW_START + timedelta(minutes=minute, seconds=second)


def test_window_always_has_sixty_buckets() -> None:
    """格子数固定，前端才能无脑渲染定长的条。"""
    window = build_uptime_window([], window_start=WINDOW_START)

    assert len(window.buckets) == BUCKET_COUNT == 60


def test_buckets_without_probes_are_unknown_not_up() -> None:
    """没探测过就画成绿色等于撒谎——目标可能刚建好，也可能探测挂了。"""
    window = build_uptime_window([], window_start=WINDOW_START)

    assert set(window.buckets) == {"unknown"}
    assert window.uptime_rate is None


def test_all_successful_probes_make_every_covered_bucket_up() -> None:
    """有探测且全成功的格子是绿的，没覆盖到的仍是灰的。"""
    events = [("up", _at(minute)) for minute in range(10)]

    window = build_uptime_window(events, window_start=WINDOW_START)

    assert window.buckets[:10] == ["up"] * 10
    assert window.buckets[10:] == ["unknown"] * 50
    assert window.uptime_rate == 1.0


def test_one_failure_makes_the_whole_bucket_down() -> None:
    """一格里两次探测、一次失败——必须是红。宁可多报也不能藏抖动。"""
    events = [("up", _at(3, 0)), ("down", _at(3, 30))]

    window = build_uptime_window(events, window_start=WINDOW_START)

    assert window.buckets[3] == "down"


def test_uptime_rate_counts_probes_not_buckets() -> None:
    """按原始探测算：4 次里错 1 次是 0.75。

    若按格子算，这 4 次挤在 1 格里就成了「1 格全红 = 0%」，
    严重夸大故障——格子的粒度不该影响可用率数字。
    """
    events = [
        ("up", _at(0, 0)),
        ("up", _at(0, 15)),
        ("up", _at(0, 30)),
        ("down", _at(0, 45)),
    ]

    window = build_uptime_window(events, window_start=WINDOW_START)

    assert window.buckets[0] == "down"
    assert window.uptime_rate == 0.75


def test_events_outside_the_window_are_ignored() -> None:
    """窗口外的事件不能算进来，否则「最近一小时」这句话就是假的。"""
    events = [
        ("down", WINDOW_START - timedelta(minutes=1)),  # 太早
        ("up", _at(0)),
        ("down", WINDOW_START + timedelta(hours=1, minutes=1)),  # 太晚
    ]

    window = build_uptime_window(events, window_start=WINDOW_START)

    assert window.buckets[0] == "up"
    assert window.uptime_rate == 1.0


def test_bucket_index_is_floor_of_elapsed_minutes() -> None:
    """59 分 59 秒仍属于最后一格，不能溢出到第 61 格。"""
    events = [("down", _at(59, 59))]

    window = build_uptime_window(events, window_start=WINDOW_START)

    assert window.buckets[59] == "down"
    assert window.buckets[58] == "unknown"


def test_window_metadata_lets_the_frontend_label_each_bucket() -> None:
    """前端要能算出每格对应的时间，才能做 tooltip。"""
    window = build_uptime_window([], window_start=WINDOW_START)

    assert window.started_at == WINDOW_START
    assert window.bucket_seconds == 60


def test_unsorted_events_are_handled() -> None:
    """事件来源按 target 分组、组内倒序，别的调用方也可能乱序传入。"""
    events = [("down", _at(5)), ("up", _at(1))]

    window = build_uptime_window(events, window_start=WINDOW_START)

    assert window.buckets[1] == "up"
    assert window.buckets[5] == "down"


def test_naive_timestamps_are_treated_as_utc() -> None:
    """SQLite 取回的 checked_at 是 naive 的，PostgreSQL 是 aware 的。

    列声明是 DateTime(timezone=True)，但 SQLite 驱动不保留时区。
    单测跑 SQLite、生产跑 PostgreSQL，这个函数必须两种都吃得下——
    否则单测全绿、上线就抛「can't subtract offset-naive and offset-aware」。
    项目里 spawn/receipts.py 与 agent_sessions.py 用的是同一套归一化。
    """
    naive = datetime(2026, 8, 18, 10, 5)  # noqa: DTZ001 - 刻意构造 naive，见上

    window = build_uptime_window([("down", naive)], window_start=WINDOW_START)

    assert window.buckets[5] == "down"
