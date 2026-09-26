"""stdio -> HTTP bridge against the real service app (in-process)."""

import asyncio
import json

import httpx
import pytest
import uvicorn

from housevitals import proxy
from housevitals.config import DeviceConfig, ServerConfig
from housevitals.proxy import UNREACHABLE, Bridge
from housevitals.service import build_app
from conftest import _free_port

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


@pytest.fixture
async def service(neo_device):
    """The real service app on a real local port (SSE responses need real streaming)."""
    config = ServerConfig(devices=[DeviceConfig(name="hp", host="127.0.0.1", port=neo_device, profile="neo")])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(config, metric_readers=[]), host="127.0.0.1",
                                           port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    await asyncio.wait_for(task, 10)


async def _call(bridge: Bridge, request_id, method, params=None):
    [answer] = await bridge.handle({"jsonrpc": "2.0", "id": request_id, "method": method,
                                    "params": params or {}})
    return answer


async def test_bridge_forwards_tools(service):
    bridge = Bridge(service)
    [init] = await bridge.handle(INIT)
    assert init["result"]["serverInfo"]["name"] == "housevitals" and bridge.session_id
    assert await bridge.handle(INITIALIZED) == []
    tools = await _call(bridge, 2, "tools/list")
    assert "get_overview" in {t["name"] for t in tools["result"]["tools"]}
    result = await _call(bridge, 3, "tools/call", {"name": "read_values",
                                                   "arguments": {"keys": ["flow_temperature"]}})
    values = json.loads(result["result"]["content"][0]["text"])
    assert values["values"]["flow_temperature"]["value"] == 34.5
    await bridge.close()


async def test_bridge_recovers_from_expired_session(service):
    bridge = Bridge(service)
    await bridge.handle(INIT)
    await bridge.handle(INITIALIZED)
    bridge.session_id = "expired-after-service-restart"
    tools = await _call(bridge, 7, "tools/list")
    assert tools["id"] == 7 and tools["result"]["tools"]  # re-initialized transparently
    assert bridge.session_id != "expired-after-service-restart"
    await bridge.close()


async def test_bridge_reports_unreachable_service():
    def refuse(request):
        raise httpx.ConnectError("Connection refused")

    bridge = Bridge("http://127.0.0.1:1/mcp", transport=httpx.MockTransport(refuse))
    [answer] = await bridge.handle(INIT)
    assert answer["id"] == 1 and answer["error"]["code"] == UNREACHABLE
    assert "not reachable" in answer["error"]["message"]
    assert await bridge.handle(INITIALIZED) == []  # notifications get no answer
    await bridge.close()


async def test_service_reachable_and_health_url(service):
    assert proxy.health_url("http://127.0.0.1:8080/mcp") == "http://127.0.0.1:8080/healthz"
    assert await asyncio.to_thread(proxy.service_reachable, service) is True
    assert proxy.service_reachable("http://127.0.0.1:1/mcp", timeout=0.5) is False
