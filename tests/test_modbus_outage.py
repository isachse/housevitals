"""Behaviour while an appliance does not answer: fail fast, last known values, recovery."""

import asyncio
import json
import time

import httpx
import pytest

from housevitals import hub as hub_module
from housevitals.config import DeviceConfig, ServerConfig, ServiceConfig
from housevitals.context import Services
from housevitals.hub import ApplianceUnavailableError, Hub
from housevitals.server import build_server
from housevitals.service import build_app
from conftest import _free_port, _start_neo


class Hanging:
    """Accepts TCP connections and never answers; counts connections."""

    def __init__(self):
        self.connections = 0

    async def start(self) -> int:
        async def handler(reader, writer):
            self.connections += 1
            await asyncio.sleep(3600)

        self.server = await asyncio.start_server(handler, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


def _config(**devices: int) -> ServerConfig:
    return ServerConfig(
        devices=[DeviceConfig(name=n, host="127.0.0.1", port=p, profile="neo", timeout=0.5)
                 for n, p in devices.items()],
        service=ServiceConfig(poll_fast=0.3, poll_slow=0.6))


async def test_hanging_device_is_detected_once_then_fails_fast(neo_device):
    hanging = Hanging()
    config = _config(ok=neo_device, hang=await hanging.start())
    services = Services.create(config)
    app = services.hub.get("hang")
    started = time.monotonic()
    await app.poll("fast")  # used to take minutes: one timeout per batch and register
    assert time.monotonic() - started < 1.5
    assert app.up is False and "No response" in app.last_error
    connections = hanging.connections

    mcp = build_server(services)
    started = time.monotonic()
    result = json.loads((await mcp.call_tool("read_values", {"appliance": "hang", "keys": ["low_pressure"]})).content[0].text)
    overview = json.loads((await mcp.call_tool("get_overview", {})).content[0].text)
    raw = json.loads((await mcp.call_tool("read_raw_registers", {"appliance": "hang", "address": 10})).content[0].text)
    assert time.monotonic() - started < 0.3  # breaker open: no waiting
    assert hanging.connections == connections  # and no new connection attempt
    assert result["available"] is False and result["retry_after_s"] >= 1 and "hint" in result
    assert overview["appliances"]["ok"]["values"]["flow_temperature"]["value"] == 34.5
    assert overview["appliances"]["hang"]["available"] is False
    assert raw["available"] is False
    await services.close()
    hanging.server.close()


async def test_last_known_values_while_down_and_recovery():
    port, server, task = await _start_neo()
    config = _config(hp=port)
    hub = Hub(config)
    app = hub.get("hp")
    await app.poll("fast")
    assert app.up is True
    await server.shutdown()
    task.cancel()

    await app.poll("fast")
    assert app.up is False
    reg = app.profile.registers["flow_temperature"]
    value = (await app.read([reg]))[reg.key]
    assert value["value"] == 34.5 and value["stale"] is True  # young, but the device is down
    status = app.availability()
    assert status["available"] is False and status["unavailable_since"] and status["last_success"]
    with pytest.raises(ApplianceUnavailableError):
        await app.read_raw("input", 10, 1)

    # The background poller brings it back once the device answers again.
    runner = asyncio.create_task(app.run())
    try:
        port, server, task = await _start_neo(port=port)
        app.retry_at = 0.0  # skip the remaining back-off
        for _ in range(50):
            if app.up and app.status["slow"].last_success:
                break
            await asyncio.sleep(0.1)
        assert app.up is True and app.availability()["available"] is True
        assert app.status["slow"].last_success  # every group refreshed after recovery
        assert "stale" not in (await app.read([reg]))[reg.key]
    finally:
        runner.cancel()
        await server.shutdown()
        task.cancel()
        await hub.stop()


def test_back_off_grows_to_five_minutes():
    app = Hub(_config(hp=_free_port())).get("hp")
    waits = []
    for _ in range(6):
        app._mark_down("refused")
        waits.append(round(app.retry_in()))
    interval = app.status["fast"].interval
    assert waits[:4] == [round(interval * f) for f in hub_module.BACKOFF_FACTORS]
    assert waits[4:] == [hub_module.MAX_RETRY_S] * 2


async def test_device_error_response_is_not_an_outage(neo_device):
    app = Hub(_config(hp=neo_device)).get("hp")
    raw_error = None
    try:
        await app.read_raw("holding", 5000, 1)  # unmapped address: Modbus exception response
    except Exception as err:  # noqa: BLE001
        raw_error = err
    assert raw_error is not None and getattr(raw_error, "status", None) == 502
    assert app.up is True


async def test_rest_while_appliance_down(neo_device):
    config = _config(ok=neo_device, gone=_free_port())
    services = Services.create(config)
    await services.hub.get("gone").poll("fast")
    app = build_app(config, services, metric_readers=[])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        no_cache = await c.get("/api/v1/appliances/gone/values?keys=flow_temperature")
        assert no_cache.status_code == 503 and int(no_cache.headers["retry-after"]) >= 1
        assert no_cache.json()["available"] is False
        overview = await c.get("/api/v1/overview")
        assert overview.status_code == 200
        body = overview.json()
        assert body["ok"]["values"] and body["gone"]["available"] is False and body["gone"]["error"]
        health = (await c.get("/healthz")).json()
        assert health["status"] == "ok" and health["appliances"]["gone"]["available"] is False
    await services.close()


async def test_metrics_stop_while_down():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from housevitals.metrics import setup_metrics

    port, server, task = await _start_neo()
    hub = Hub(_config(hp=port))
    app = hub.get("hp")
    await app.poll("fast")
    reader = InMemoryMetricReader()
    provider, _ = setup_metrics(hub, readers=[reader])

    def exported() -> set[str]:
        data = reader.get_metrics_data()
        return {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics
                if m.data.data_points}

    assert "housevitals.flow_temperature" in exported()
    await server.shutdown()
    task.cancel()
    await app.poll("fast")
    names = exported()
    assert "housevitals.flow_temperature" not in names and "housevitals.up" in names
    provider.shutdown()
    await hub.stop()


async def test_outage_start_survives_a_restart(tmp_path):
    """"Unavailable since" and "last success" are kept across restarts of the service."""
    state = tmp_path / "availability.json"
    port, server, task = await _start_neo()
    config = _config(hp=port)
    config.service.availability_state_file = str(state)
    hub = Hub(config)
    app = hub.get("hp")
    await app.poll("fast")
    success = app.last_success
    await server.shutdown()
    task.cancel()
    await app.poll("fast")  # down now
    down_since = app.since
    await hub.stop()

    restarted = Hub(config)  # still down after the restart: the outage keeps its start
    app = restarted.get("hp")
    assert app.last_success == success
    await app.poll("fast")
    assert app.up is False and app.since == down_since
    assert app.availability()["last_success"] is not None
    await restarted.stop()

    state.write_text(json.dumps({"appliances": {"hp": {"down_since": None, "last_success": 1000.0}}}))
    app = Hub(config).get("hp")  # was up before the restart: down since its last success
    await app.poll("fast")
    assert app.since == 1000.0

    state.write_text("not json")
    assert Hub(config).get("hp").last_success is None  # unreadable state: start fresh
