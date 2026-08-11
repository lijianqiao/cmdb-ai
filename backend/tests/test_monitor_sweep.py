"""Tests for the TCP probe and the single-pass monitor sweep."""

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.monitor_status_event import monitor_status_event_crud
from app.crud.monitor_target import monitor_target_crud
from app.services.monitor_sweep import probe_tcp, run_monitor_sweep_once

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
