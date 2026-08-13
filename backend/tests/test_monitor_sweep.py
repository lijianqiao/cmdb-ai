"""Tests for the TCP probe and the single-pass monitor sweep."""

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.crud.system_config import system_config_crud
from app.services import monitor_sweep as monitor_sweep_module
from app.services.monitor_sweep import probe_tcp, run_monitor_sweep_loop, run_monitor_sweep_once

pytestmark = pytest.mark.asyncio


async def _start_echo_server() -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _get_closed_port() -> int:
    """Bind then immediately release a port so it's (very likely) free but not listening."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_probe_tcp_reports_up_for_listening_port() -> None:
    server, port = await _start_echo_server()
    try:
        status, latency_ms, detail = await probe_tcp("127.0.0.1", port, timeout_seconds=2.0)
    finally:
        server.close()
        await server.wait_closed()

    assert status == "up"
    assert latency_ms is not None
    assert latency_ms >= 0
    assert detail == ""


async def test_probe_tcp_reports_down_for_closed_port() -> None:
    port = _get_closed_port()

    status, latency_ms, detail = await probe_tcp("127.0.0.1", port, timeout_seconds=2.0)

    assert status == "down"
    assert latency_ms is None
    assert detail != ""


async def test_probe_tcp_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr("app.services.monitor_sweep.asyncio.open_connection", hang)

    status, latency_ms, detail = await probe_tcp("127.0.0.1", 9, timeout_seconds=0.05)

    assert status == "down"
    assert latency_ms is None
    assert detail == "连接超时"


async def test_run_monitor_sweep_once_records_one_event_per_active_target(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True}
    )
    await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.6", "port": 22, "is_active": False}
    )
    await db_session.commit()

    async def fake_probe(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
        return "up", 3, ""

    monkeypatch.setattr("app.services.monitor_sweep.probe_tcp", fake_probe)

    probed_count = await run_monitor_sweep_once(db_session)

    assert probed_count == 1
    events = await monitor_status_event_crud.list_recent_for_target(db_session, active.id)
    assert len(events) == 1
    assert events[0].status == "up"


async def test_run_monitor_sweep_once_continues_after_one_target_probe_raises(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True}
    )
    healthy = await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.6", "port": 22, "is_active": True}
    )
    await db_session.commit()

    async def flaky_probe(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
        if ip == "10.0.0.5":
            raise OSError("network unreachable")
        return "up", 1, ""

    monkeypatch.setattr("app.services.monitor_sweep.probe_tcp", flaky_probe)

    probed_count = await run_monitor_sweep_once(db_session)

    assert probed_count == 2
    failing_events = await monitor_status_event_crud.list_recent_for_target(db_session, failing.id)
    healthy_events = await monitor_status_event_crud.list_recent_for_target(db_session, healthy.id)
    assert failing_events[0].status == "down"
    assert healthy_events[0].status == "up"


async def test_run_monitor_sweep_once_reads_probe_timeout_from_database(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单轮巡检应从数据库读取 TCP 探测超时并传给 probe_tcp。"""
    await monitor_target_crud.create(
        db_session, {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True}
    )
    await db_session.commit()

    await system_config_crud.upsert_values(
        db_session,
        {"MONITOR_PROBE_TIMEOUT_SECONDS": "7.5"},
        updated_by_user_id=None,
    )

    recorded_timeout: float | None = None

    async def fake_probe(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
        nonlocal recorded_timeout
        recorded_timeout = timeout_seconds
        return "up", 3, ""

    monkeypatch.setattr("app.services.monitor_sweep.probe_tcp", fake_probe)

    await run_monitor_sweep_once(db_session)

    assert recorded_timeout == 7.5


async def test_run_monitor_sweep_loop_reads_sweep_interval_from_database(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台巡检循环应在每轮结束后按数据库间隔休眠。"""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(monitor_sweep_module, "AsyncSessionLocal", session_factory)

    async with session_factory() as db:
        await system_config_crud.upsert_values(
            db,
            {"MONITOR_SWEEP_INTERVAL_SECONDS": "12"},
            updated_by_user_id=None,
        )
        await db.commit()

    recorded_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded_delays.append(delay)
        raise asyncio.CancelledError

    async def fake_sweep_once(db: AsyncSession, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(monitor_sweep_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(monitor_sweep_module, "run_monitor_sweep_once", fake_sweep_once)

    with pytest.raises(asyncio.CancelledError):
        await run_monitor_sweep_loop()

    assert recorded_delays == [12.0]


async def test_run_monitor_sweep_loop_explicit_interval_overrides_database(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 interval_seconds 应覆盖数据库中的巡检间隔。"""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(monitor_sweep_module, "AsyncSessionLocal", session_factory)

    async with session_factory() as db:
        await system_config_crud.upsert_values(
            db,
            {"MONITOR_SWEEP_INTERVAL_SECONDS": "12"},
            updated_by_user_id=None,
        )
        await db.commit()

    recorded_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded_delays.append(delay)
        raise asyncio.CancelledError

    async def fake_sweep_once(db: AsyncSession, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(monitor_sweep_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(monitor_sweep_module, "run_monitor_sweep_once", fake_sweep_once)

    with pytest.raises(asyncio.CancelledError):
        await run_monitor_sweep_loop(interval_seconds=5.0)

    assert recorded_delays == [5.0]


async def test_second_sweep_same_status_updates_existing_event(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = await monitor_target_crud.create(
        db_session,
        {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True},
    )
    await db_session.commit()

    async def fake_probe(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
        return "up", 5, ""

    monkeypatch.setattr("app.services.monitor_sweep.probe_tcp", fake_probe)
    await run_monitor_sweep_once(db_session)
    first = (await monitor_status_event_crud.list_recent_for_target(db_session, target.id))[0]
    first_id = first.id
    first_checked = first.checked_at

    await run_monitor_sweep_once(db_session)
    events = await monitor_status_event_crud.list_recent_for_target(db_session, target.id)
    assert len(events) == 1
    assert events[0].id == first_id
    assert events[0].checked_at >= first_checked


async def test_status_change_inserts_new_event(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = await monitor_target_crud.create(
        db_session,
        {"cmdb_asset_id": None, "ip_address": "10.0.0.5", "port": 22, "is_active": True},
    )
    await db_session.commit()
    statuses = iter([("up", 3, ""), ("down", None, "连接超时")])

    async def fake_probe(ip: str, port: int, *, timeout_seconds: float) -> tuple[str, int | None, str]:
        return next(statuses)

    monkeypatch.setattr("app.services.monitor_sweep.probe_tcp", fake_probe)
    await run_monitor_sweep_once(db_session)
    await run_monitor_sweep_once(db_session)
    events = await monitor_status_event_crud.list_recent_for_target(db_session, target.id)
    assert [item.status for item in events] == ["down", "up"]
