"""Languages (MCP English, REST/charts localized) and error mapping."""

import json
import time

import httpx
import pytest

from housevitals import i18n
from housevitals.config import DeviceConfig, ServerConfig, ServiceConfig, UnknownApplianceError
from housevitals.context import Services
from housevitals.errors import NotFoundError, UnavailableError
from housevitals.history import History, _promql_regex_alternatives, _promql_string
from housevitals.hub import CacheEntry, Hub
from housevitals.registry import load_profile
from housevitals.server import build_server
from housevitals.service import build_app
from test_charts import NOW, _fill
from test_history import FakePrometheus


def test_language_normalization():
    assert i18n.normalize("de-DE") == "de"
    assert i18n.normalize("Deutsch") == "de"
    assert i18n.normalize("en_GB") == "en"
    assert i18n.normalize("fr", "de") == "de"  # unsupported -> default
    assert i18n.normalize("") == "en"
    assert i18n.from_accept_language("fr-CH, fr;q=0.9, de;q=0.8, en;q=0.7") == "de"
    assert i18n.from_accept_language("en-US,de;q=0.95") == "en"  # no q = 1.0
    assert i18n.from_accept_language("fr, de;q=0.5, en;q=0.4") == "de"
    assert i18n.from_accept_language(None, "de") == "de"
    t = i18n.Translator("de")
    assert t("chart.heatpump", name="wp1") == "Wärmepumpe wp1"
    assert t.state("dhw") == "Warmwasser" and t.state("defrost") == "defrost"
    assert set(i18n.MESSAGES["de"]) == set(i18n.MESSAGES["en"])  # catalogs complete


def test_register_labels_from_profile():
    reg = load_profile("neo").registers["flow_temperature"]
    assert reg.display_label("de") == "Vorlauftemperatur"
    assert reg.display_label("en") == reg.display_label("xx") == "Flow temperature"
    assert load_profile("neo").find(search="vorlauf")  # German labels are searchable


def test_promql_escaping():
    assert _promql_string('a"b') == 'a\\"b'
    assert _promql_regex_alternatives(["wp.1", "inv"]) == "wp\\\\.1|inv"


async def test_failed_register_keeps_last_good_value(neo_device):
    config = ServerConfig(devices=[DeviceConfig(name="hp", host="127.0.0.1", port=neo_device, profile="neo")])
    app = Hub(config).get("hp")
    reg = app.profile.registers["low_pressure"]  # not polled: read on demand
    app.cache[reg.key] = CacheEntry({"value": 7.5, "unit": "bar"}, time.time() - 60)

    async def failing_read(regs):
        return {r.key: {"value": None, "error": "Illegal data address"} for r in regs}

    app.client.read = failing_read
    data = (await app.read([reg]))[reg.key]
    assert data["value"] == 7.5 and data["stale"] is True and data["age_s"] >= 60


async def test_mcp_text_is_english_and_chart_url_encoded(neo_device):
    config = ServerConfig(lang="de", devices=[DeviceConfig(
        name="hp", host="127.0.0.1", port=neo_device, profile="neo", aliases=["Wärmepumpe 1"])])
    mcp = build_server(config)
    result = json.loads((await mcp.call_tool(
        "read_values", {"appliance": "Wärmepumpe 1", "keys": ["flow_temperature"]})).content[0].text)
    assert result["values"]["flow_temperature"]["label"] == "Flow temperature"
    unknown = json.loads((await mcp.call_tool("list_categories", {"appliance": "nope"})).content[0].text)
    assert "Unknown appliance" in unknown["error"]


def _prom_services(prom: FakePrometheus, lang: str = "en") -> Services:
    devices = [DeviceConfig(name="hp", host="127.0.0.1", profile="neo", aliases=["Wärmepumpe 1"]),
               DeviceConfig(name="inverter", host="127.0.0.1", profile="sungrow_sh")]
    config = ServerConfig(devices=devices, lang=lang,
                          service=ServiceConfig(prometheus_url="http://prom"))
    hub = Hub(config)
    history = History(hub, "http://prom", transport=httpx.MockTransport(prom.handler))
    history.now = lambda: NOW
    return Services.create(config, hub, history)


async def test_chart_language_and_url():
    prom = FakePrometheus()
    _fill(prom)
    services = _prom_services(prom, lang="de")
    charts = services.charts
    de = await charts.get("heatpump", "Wärmepumpe 1")
    en = await charts.get("heatpump", "hp", lang="en-GB")
    assert (de.summary["lang"], en.summary["lang"]) == ("de", "en")
    assert de.png != en.png and charts.renders == 2
    assert {r.key[3] for r in charts.default_requests()} == {"de"}  # background: configured lang

    result = (await build_server(services).call_tool(
        "get_chart", {"chart": "heatpump", "appliance": "Wärmepumpe 1", "lang": "Deutsch"})).content
    meta = json.loads(result[0].text)
    assert meta["url"].endswith("heatpump.png?appliance=W%C3%A4rmepumpe+1&lang=de")


async def test_rest_language_and_status_codes(neo_device):
    prom = FakePrometheus()
    _fill(prom)
    services = _prom_services(prom)
    services.hub.appliances["hp"].client.port = neo_device
    app = build_app(services.config, services)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        url = "/api/v1/appliances/hp/registers?search=flow_temperature"
        assert (await c.get(url)).json()[0]["label"] == "Flow temperature"
        assert (await c.get(url + "&lang=de")).json()[0]["label"] == "Vorlauftemperatur"
        german = await c.get(url, headers={"Accept-Language": "de-DE,de;q=0.9"})
        assert german.json()[0]["label"] == "Vorlauftemperatur"

        values = (await c.get("/api/v1/appliances/hp/values?keys=flow_temperature&lang=de")).json()
        assert values["values"]["flow_temperature"]["label"] == "Vorlauftemperatur"

        assert (await c.get("/api/v1/appliances/nope/overview")).status_code == 404
        assert (await c.get("/api/v1/appliances/hp/values")).status_code == 400
        assert (await c.get("/api/v1/appliances/hp/values?keys=nope")).status_code == 404
        assert (await c.get("/api/v1/charts/pie.png")).status_code == 404
        png = await c.get("/api/v1/charts/energy_flow.png?lang=de")
        assert png.status_code == 200 and png.headers["content-language"] == "de"

        await services.history.close()
        services.history.prometheus._transport = httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
        down = await c.get("/api/v1/appliances/hp/history?keys=flow_temperature")
        assert down.status_code == 503 and "not reachable" in down.json()["detail"]


def test_error_types():
    config = ServerConfig(devices=[DeviceConfig(name="hp", host="x", profile="neo")])
    with pytest.raises(UnknownApplianceError) as err:
        config.resolve("nope")
    assert isinstance(err.value, NotFoundError) and err.value.status == 404
    from housevitals.history import PrometheusUnavailableError
    from housevitals.modbus import ModbusConnectError
    assert PrometheusUnavailableError.status == ModbusConnectError.status == 503
    assert issubclass(ModbusConnectError, UnavailableError)


def test_grafana_dashboards_fully_translated():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import build_grafana_dashboard as dashboards

    result = dashboards.dashboards()  # raises on any untranslated display text
    assert set(result) == {"de", "en"}
    assert result["en"]["uid"] == "home-energy-en"
    exprs = lambda d: json.dumps([t["expr"] for p in d["panels"] for t in p.get("targets", [])])  # noqa: E731
    assert exprs(result["de"]) == exprs(result["en"])  # queries identical
