"""Chart catalog, cache, background refresh and MCP delivery (fake Prometheus)."""

import asyncio
import json
from datetime import datetime

import httpx
import pytest

from housevitals.charts import CATALOG, ChartError, ChartService
from housevitals.history import History
from housevitals.context import Services
from housevitals.server import build_server
from test_history import TZ, FakePrometheus, _hub

PNG = b"\x89PNG\r\n\x1a\n"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=TZ)


def _fill(prom: FakePrometheus):
    t0 = NOW.timestamp() - 3 * 86400
    for i in range(3 * 24 * 12):  # every 5 min for 3 days
        t = t0 + i * 300
        hour = (i * 5 / 60) % 24
        prom.add("housevitals_pv_power_watts", "inverter", [(t, max(0, 5000 - abs(hour - 13) * 900))])
        prom.add("housevitals_load_power_watts", "inverter", [(t, 800)])
        prom.add("housevitals_battery_power_watts", "inverter", [(t, -500 if 9 < hour < 16 else 300)])
        prom.add("housevitals_grid_power_watts", "inverter", [(t, 100)])
        prom.add("housevitals_battery_soc_percent", "inverter", [(t, 20 + hour * 3)])
        prom.add("housevitals_pv_energy_kWh_total", "inverter", [(t, 1000 + i * 0.2)])
        prom.add("housevitals_import_energy_kWh_total", "inverter", [(t, 500 + i * 0.01)])
        prom.add("housevitals_export_energy_kWh_total", "inverter", [(t, 300 + i * 0.05)])
        prom.add("housevitals_direct_consumption_kWh_total", "inverter", [(t, 200 + i * 0.05)])
        prom.add("housevitals_battery_discharge_kWh_total", "inverter", [(t, 100 + i * 0.02)])
        prom.add("housevitals_flow_temperature_celsius", "hp", [(t, 35 + hour / 4)])
        prom.add("housevitals_return_temperature_celsius", "hp", [(t, 30 + hour / 4)])
        prom.add("housevitals_dhw_temperature_celsius", "hp", [(t, 48)])
        prom.add("housevitals_outdoor_temperature_celsius", "hp", [(t, 8)])
        prom.add("housevitals_compressor_demand", "hp", [(t, 20 if hour < 6 else 30 if hour < 7 else 0)])
        prom.add("housevitals_electricity_kWh_total", "hp", [(t, 100 + i // 12)])
        prom.add("housevitals_heat_delivered_kWh_total", "hp", [(t, 400 + 4 * (i // 12))])
    # Runtime needs dense samples (gaps > 2 min count as "no data"): every minute.
    prom.add("housevitals_compressor", "hp", [
        (t0 + m * 60, 10 if (m / 60) % 24 < 7 else 0) for m in range(3 * 24 * 60)])


@pytest.fixture
def charts():
    prom = FakePrometheus()
    _fill(prom)
    hub = _hub()
    history = History(hub, "http://prom", "Europe/Berlin", transport=httpx.MockTransport(prom.handler))
    history.now = lambda: NOW
    service = ChartService(hub, history)
    service.prom = prom
    return service


async def test_every_catalog_chart_renders(charts):
    for req in charts.default_requests():
        chart = req.spec.name
        image = await charts.get(chart, req.key[1])
        assert image.png.startswith(PNG) and 5_000 < len(image.png) < 300_000, chart
        assert image.summary["chart"] == chart
    assert charts.renders == len(charts.default_requests())  # pv_forecast needs a forecast  # one heat pump, one inverter
    flow = (await charts.get("energy_flow")).summary
    assert flow["values"]["load_power"]["avg"] == 800
    hp = (await charts.get("heatpump", "hp")).summary
    assert set(hp["compressor_demand_share"]) == {"heating", "dhw", "none"}
    cycles = (await charts.get("compressor_cycles")).summary["days"]["hp"]
    assert cycles["2026-09-25"]["starts"] == 1 and cycles["2026-09-25"]["hours_on"] == pytest.approx(7, abs=0.1)


async def test_cache_and_stale_fallback(charts):
    first = await charts.get("energy_flow", "", "24h")
    again = await charts.get("energy_flow", "", "24h")
    assert again is first and charts.renders == 1
    await charts.get("energy_flow", "", "6h")  # different range: separate image
    assert charts.renders == 2

    first.generated_at -= CATALOG["energy_flow"].max_age + 1  # expire
    await charts.history.close()  # the pooled connection is re-created with the new transport
    charts.history.prometheus._transport = httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    stale = await charts.get("energy_flow", "", "24h")
    assert stale.stale and stale.png != first.png and stale.png.startswith(PNG)  # badge drawn in
    assert stale.summary["generated_at"] == first.summary["generated_at"]
    assert stale.summary["stale_reason"]["history_available"] is False
    renders = charts.renders
    again = await charts.get("energy_flow", "", "24h")  # outage known: no new attempt
    assert again.png == stale.png and charts.renders == renders


async def test_argument_errors(charts):
    with pytest.raises(ChartError, match="Unknown chart"):
        await charts.get("pie")
    with pytest.raises(ChartError, match="needs a heat pump"):
        await charts.get("heatpump", "inverter")
    with pytest.raises(ChartError, match="between"):
        await charts.get("energy_flow", "", "90d")
    with pytest.raises(ChartError, match="Invalid range"):
        await charts.get("energy_flow", "", "yesterday")


async def test_scheduler_refreshes_after_fast_poll(charts):
    task = asyncio.create_task(charts.run())
    try:
        charts.hub.polled.set()
        for _ in range(100):
            if charts.renders == len(charts.default_requests()):
                break
            await asyncio.sleep(0.05)
        assert charts.renders == len(charts.default_requests())
        charts.hub.polled.set()  # nothing stale: no re-render
        await asyncio.sleep(0.2)
        assert charts.renders == len(charts.default_requests())
    finally:
        task.cancel()


async def test_get_chart_tool_returns_image_and_figures(charts):
    mcp = build_server(Services(charts.hub.config, charts.hub, charts.history, charts))
    result = await mcp.call_tool("get_chart", {"chart": "heatpump", "appliance": "hp"})
    text, image = result.content
    meta = json.loads(text.text)
    assert meta["chart"] == "heatpump" and meta["url"].endswith("/api/v1/charts/heatpump.png?appliance=hp&lang=en")
    assert image.type == "image" and image.mime_type == "image/png"
    error = json.loads((await mcp.call_tool("get_chart", {"chart": "pie"})).content[0].text)
    assert "Unknown chart" in error["error"]
