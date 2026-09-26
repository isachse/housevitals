"""Behaviour while Prometheus is down: fail fast, clear answers, stale charts, no data loss."""

import asyncio
import json
import time

import httpx
import pytest
from opentelemetry.sdk.metrics.export import MetricExportResult

from housevitals import prometheus
from housevitals.charts import ChartService
from housevitals.errors import UnavailableError
from housevitals.metrics import BufferingExporter
from housevitals.prometheus import PrometheusClient, PrometheusQueryError, PrometheusUnavailableError
from housevitals.server import build_server
from housevitals.service import build_app
from test_charts import _fill
from test_history import FakePrometheus
from test_i18n_and_errors import _prom_services


class Switchable:
    """Transport that forwards to a FakePrometheus while `up`, else refuses; counts requests."""

    def __init__(self, fake: FakePrometheus):
        self.fake, self.up, self.requests = fake, True, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if not self.up:
            raise httpx.ConnectError("Connection refused")
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="ready")
        return self.fake.handler(request)


def _services(lang: str = "en"):
    fake = FakePrometheus()
    _fill(fake)
    switch = Switchable(fake)
    services = _prom_services(fake, lang)
    services.history.prometheus._transport = httpx.MockTransport(switch)
    return services, switch


def _allow_retry(client: PrometheusClient) -> None:
    client._state.retry_at = 0.0  # back-off elapsed


async def test_circuit_breaker_fails_fast_and_recovers():
    services, switch = _services()
    history, app = services.history, services.hub.get("hp")
    switch.up = False
    with pytest.raises(PrometheusUnavailableError) as err:
        await history.history(app, ["flow_temperature"])
    assert err.value.status == 503 and err.value.retry_after >= 1
    assert err.value.details["history_available"] is False and "Live values" in err.value.details["hint"]
    status = history.prometheus.status()
    assert status["available"] is False and "refused" in status["last_error"] and status["since"]

    before = switch.requests
    for _ in range(5):  # while the breaker is open no request is sent at all
        with pytest.raises(PrometheusUnavailableError):
            await history.energy([app])
    assert switch.requests == before

    switch.up = True
    _allow_retry(history.prometheus)
    result = await history.history(app, ["flow_temperature"])
    assert result["series"]["flow_temperature"]["last"] is not None
    assert history.prometheus.status()["available"] is True


async def test_hanging_prometheus_times_out_quickly(monkeypatch):
    monkeypatch.setattr(prometheus, "TIMEOUT", httpx.Timeout(0.5, connect=0.5))

    async def never_answers(reader, writer):
        await asyncio.sleep(3600)

    server = await asyncio.start_server(never_answers, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = PrometheusClient(f"http://127.0.0.1:{port}")
    try:
        started = time.monotonic()
        with pytest.raises(PrometheusUnavailableError, match="timeout"):
            await client.get("query", {"query": "up"})
        assert time.monotonic() - started < 2
        started = time.monotonic()
        with pytest.raises(PrometheusUnavailableError):  # breaker open: immediate
            await client.get("query", {"query": "up"})
        assert time.monotonic() - started < 0.05
    finally:
        await client.close()
        server.close()


async def test_rejected_query_is_not_an_outage():
    def bad_query(request):
        return httpx.Response(400, json={"status": "error", "error": "parse error"})

    client = PrometheusClient("http://prom", httpx.MockTransport(bad_query))
    with pytest.raises(PrometheusQueryError, match="parse error"):
        await client.get("query", {"query": "("})
    assert client.available is True and not client.open()


async def test_outage_is_not_reported_as_missing_data():
    services, switch = _services()
    switch.up = False
    with pytest.raises(UnavailableError):  # used to say "no data in this time range"
        await services.charts.get("compressor_cycles")


async def test_charts_during_outage_and_after():
    services, switch = _services(lang="de")
    charts: ChartService = services.charts
    first = await charts.get("energy_flow")
    assert first.summary["generated_at"] and not first.stale

    switch.up = False
    first.generated_at -= 3600  # outdated
    stale = await charts.get("energy_flow")
    assert stale.stale and stale.summary["stale"] is True
    assert stale.summary["generated_at"] == first.summary["generated_at"]
    assert stale.png != first.png  # badge drawn into the picture

    # Scheduler: while down only a probe per back-off step, no render attempts.
    renders, before = charts.renders, switch.requests
    assert await charts.refresh_defaults() == 0
    assert switch.requests == before  # back-off not elapsed: not even a probe
    _allow_retry(services.history.prometheus)
    assert await charts.refresh_defaults() == 0
    assert switch.requests == before + 1  # one probe, still down
    assert charts.renders == renders

    switch.up = True
    _allow_retry(services.history.prometheus)
    assert await charts.refresh_defaults() == len(charts.default_requests())
    fresh = await charts.get("energy_flow")
    assert not fresh.stale and fresh.generated_at > first.generated_at


async def test_mcp_answers_during_outage():
    services, switch = _services()
    mcp = build_server(services)
    await services.charts.get("heatpump", "hp")  # cached before the outage
    services.charts._cache[next(iter(services.charts._cache))].generated_at -= 3600
    switch.up = False

    async def call(tool, args):
        return (await mcp.call_tool(tool, args)).content

    history = json.loads((await call("get_history", {"appliance": "hp", "keys": ["flow_temperature"]}))[0].text)
    assert history["history_available"] is False and history["retry_after_s"] >= 1
    assert "Live values" in history["hint"] and history["unavailable_since"]

    text, image = await call("get_chart", {"chart": "heatpump", "appliance": "hp"})
    meta = json.loads(text.text)
    assert meta["stale"] is True and meta["generated_at"] and "Outdated" in meta["note"]
    assert image.type == "image"

    no_cache = json.loads((await call("get_chart", {"chart": "energy_daily"}))[0].text)
    assert no_cache["history_available"] is False and "error" in no_cache

    devices = json.loads((await call("list_devices", {}))[0].text)
    assert devices["history"]["available"] is False


async def test_rest_during_outage():
    services, switch = _services()
    await services.charts.get("energy_flow")
    services.charts._cache[next(iter(services.charts._cache))].generated_at -= 3600
    switch.up = False
    app = build_app(services.config, services, metric_readers=[])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        down = await c.get("/api/v1/energy")
        assert down.status_code == 503 and int(down.headers["retry-after"]) >= 1
        assert down.json()["history_available"] is False
        png = await c.get("/api/v1/charts/energy_flow.png")
        assert png.status_code == 200 and png.headers["x-chart-stale"]
        assert png.headers["last-modified"].endswith("GMT") and png.headers["x-chart-generated-at"]
        health = (await c.get("/healthz")).json()
        assert health["status"] == "ok" and health["history"]["available"] is False
        assert health["charts"]["outdated"] == 1


class FlakyExporter:
    _preferred_temporality = {}
    _preferred_aggregation = {}

    def __init__(self):
        self.up, self.received = False, []

    def export(self, data, timeout_millis=10_000, **kwargs):
        if not self.up:
            return MetricExportResult.FAILURE
        self.received.append(data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=10_000):
        return True

    def shutdown(self, timeout_millis=30_000, **kwargs):
        pass


def test_metric_export_buffers_during_outage(monkeypatch):
    inner = FlakyExporter()
    exporter = BufferingExporter(inner)
    for batch in ("a", "b", "c"):
        assert exporter.export(batch) is MetricExportResult.FAILURE
    assert exporter.status()["available"] is False and exporter.status()["buffered_exports"] == 3

    inner.up = True
    assert exporter.export("d") is MetricExportResult.SUCCESS
    assert inner.received == ["a", "b", "c", "d"]  # re-sent in order, nothing lost
    assert exporter.status() | {"since": None} == {"available": True, "buffered_exports": 0,
                                                   "dropped_exports": 0, "since": None}

    inner.up = False
    exporter.export("old")
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + BufferingExporter.MAX_BACKLOG_AGE + 60)
    exporter.export("new")  # "old" is beyond Prometheus' out-of-order window
    assert exporter.status()["dropped_exports"] == 1 and exporter.status()["buffered_exports"] == 1
