"""Tests for the service layer: hub cache/poller, OpenTelemetry metrics, REST API."""

import asyncio
import json

import httpx
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from housevitals.config import DeviceConfig, ServerConfig, ServiceConfig
from housevitals.hub import Hub
from housevitals.metrics import metric_key, setup_metrics
from housevitals.registry import load_profile, poll_group
from housevitals.context import Services
from housevitals.server import build_server
from housevitals.service import build_app
from conftest import _free_port


def _config(neo_port, sungrow_port=None, **service):
    devices = [DeviceConfig(name="hp", host="127.0.0.1", port=neo_port, profile="neo",
                            aliases=["wp1"], timeout=2, extra_keys=["high_pressure"])]
    if sungrow_port:
        devices.append(DeviceConfig(name="inverter", host="127.0.0.1", port=sungrow_port,
                                    profile="sungrow_sh", timeout=2))
    return ServerConfig(devices=devices, service=ServiceConfig(**service))


def test_poll_plan():
    neo = {r.key: poll_group(r, frozenset({"high_pressure"})) for r in load_profile("neo").registers.values()}
    assert neo["flow_temperature"] == "fast"
    assert neo["electricity_total"] == "slow"
    assert neo["high_pressure"] == "fast"  # extra key
    assert neo["low_pressure"] is None  # on demand only
    sg = {r.key: r for r in load_profile("sungrow_sh").registers.values()}
    assert poll_group(sg["serial_number"]) == "static"
    assert sg["total_pv_energy"].is_counter and not sg["daily_pv_energy"].is_counter
    assert not sg["battery_capacity"].is_counter


def test_metric_key_collisions():
    used: set[str] = set()
    assert metric_key("total_pv_energy", True, used) == "pv_energy"
    assert metric_key("boiler_gas_energy", True, used) == "boiler_gas_energy"
    assert metric_key("boiler_gas_energy_total", True, used) == "boiler_gas_energy_overall"
    assert metric_key("total_power", False, used) == "total_power"  # gauges keep their key


async def test_mcp_serves_polled_values_from_cache(neo_device):
    hub = Hub(_config(neo_device))
    app = hub.get("wp1")
    await app.poll("fast")
    requests_after_poll = app.client.request_count
    mcp = build_server(Services.create(hub.config, hub))
    result = json.loads((await mcp.call_tool(
        "read_values", {"appliance": "hp", "keys": ["flow_temperature", "high_pressure"]}
    )).content[0].text)
    assert result["values"]["flow_temperature"]["value"] == 34.5
    assert result["values"]["flow_temperature"]["age_s"] < 5
    assert app.client.request_count == requests_after_poll  # no extra Modbus traffic

    # Not polled -> read on demand once, then served from cache within the TTL.
    await mcp.call_tool("read_values", {"appliance": "hp", "keys": ["low_pressure"]})
    after_first = app.client.request_count
    assert after_first == requests_after_poll + 1
    await mcp.call_tool("read_values", {"appliance": "hp", "keys": ["low_pressure"]})
    assert app.client.request_count == after_first
    await hub.stop()


async def test_requests_to_one_device_never_overlap(neo_device):
    hub = Hub(_config(neo_device))
    app = hub.get("hp")
    active = max_active = 0
    original = app.client._read_words

    async def tracking(*args, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            return await original(*args, **kwargs)
        finally:
            active -= 1

    app.client._read_words = tracking
    regs = app.profile.find(category="refrigerant")
    await asyncio.gather(app.poll("fast"), app.poll("slow"), app.read(regs),
                         app.client.read_raw("input", 10, 2))
    assert max_active == 1
    await hub.stop()


async def test_unreachable_appliance_returns_stale_values(neo_device):
    hub = Hub(_config(neo_device, on_demand_ttl=0.01))
    app = hub.get("hp")
    reg = app.profile.registers["low_pressure"]
    await app.read([reg])
    await app.client.close()
    app.client.port = _free_port()  # nothing listens here any more
    app.client._client = None
    await asyncio.sleep(0.05)
    data = (await app.read([reg]))[reg.key]
    assert data["stale"] is True and "value" in data
    assert app.up is False
    await hub.stop()


async def test_metrics(neo_device, sungrow_device):
    hub = Hub(_config(neo_device, sungrow_device))
    for app in hub.appliances.values():
        for group in app.groups:
            await app.poll(group)
    reader = InMemoryMetricReader()
    provider, _ = setup_metrics(hub, readers=[reader])
    data = reader.get_metrics_data()
    metrics = {
        m.name: m
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    flow = metrics["housevitals.flow_temperature"]
    point = flow.data.data_points[0]
    assert point.value == 34.5 and point.attributes["appliance"] == "hp"
    assert flow.unit == "Cel"
    energy = metrics["housevitals.electricity"]  # "total" token dropped like Prometheus does
    assert type(energy.data).__name__ == "Sum" and energy.data.is_monotonic
    assert energy.unit == "kWh"
    assert metrics["housevitals.compressor"].data.data_points[0].value == 1  # enum -> code
    assert metrics["housevitals.pv_generating"].data.data_points[0].value == 1  # bool -> 1
    assert "housevitals.serial_number" not in metrics  # strings only in info
    info = {p.attributes["appliance"]: p.attributes for p in metrics["housevitals.appliance.info"].data.data_points}
    assert info["inverter"]["serial_number"] == "A242"
    ups = {p.attributes["appliance"]: p.value for p in metrics["housevitals.up"].data.data_points}
    assert ups == {"hp": 1, "inverter": 1}
    rm = data.resource_metrics[0]
    assert rm.resource.attributes["service.name"] == "housevitals"
    provider.shutdown()
    await hub.stop()


async def test_rest_api_and_mcp_http(neo_device, sungrow_device):
    config = _config(neo_device, sungrow_device)
    app = build_app(config, metric_readers=[InMemoryMetricReader()])
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.5)  # first poll cycle
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            health = (await client.get("/healthz")).json()
            assert {k: v["available"] for k, v in health["appliances"].items()} == {"hp": True, "inverter": True}

            appliances = (await client.get("/api/v1/appliances")).json()
            assert [a["name"] for a in appliances] == ["hp", "inverter"]
            assert appliances[0]["status"]["groups"]["fast"]["errors"] == 0

            values = (await client.get("/api/v1/appliances/wp1/values",
                                       params={"keys": ["flow_temperature", "cop"]})).json()
            assert values["values"]["flow_temperature"]["value"] == 34.5
            assert values["values"]["cop"]["age_s"] is not None

            overview = (await client.get("/api/v1/overview")).json()
            assert overview["inverter"]["values"]["battery_soc"]["value"] == 65.5

            assert (await client.get("/api/v1/appliances/attic/overview")).status_code == 404
            assert (await client.get("/api/v1/appliances/hp/values")).status_code == 400
            regs = (await client.get("/api/v1/appliances/hp/registers",
                                     params={"search": "pressure"})).json()
            assert {r["key"]: r["poll_group"] for r in regs} == {
                "low_pressure": None, "high_pressure": "fast"}

            spec = (await client.get("/openapi.json")).json()
            assert "/api/v1/appliances/{name}/values" in spec["paths"]

            # MCP over Streamable HTTP is served from the same app.
            init = await client.post("/mcp", headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"}}})
            assert init.status_code == 200
            assert "housevitals" in init.text
