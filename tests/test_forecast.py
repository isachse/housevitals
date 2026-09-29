"""PV forecast and surplus windows (Open-Meteo mocked, measurements stubbed)."""

import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from housevitals.config import ConfigError, DeviceConfig, ForecastConfig, PVArray, ServerConfig, ServiceConfig
from housevitals.context import Services
from housevitals.errors import UnavailableError
from housevitals.forecast import (
    STEP_S,
    ForecastRequestError,
    ForecastService,
    ForecastUnavailableError,
    OpenMeteo,
)
from housevitals.hub import Hub
from housevitals.server import build_server
from housevitals.solar import dc_power, plane_irradiance, sun_position

TZ = ZoneInfo("Europe/Berlin")
LAT, LON = 52.52, 13.40  # Berlin (example site)
NOW = datetime(2026, 9, 29, 20, 0, tzinfo=TZ).timestamp()  # evening: tomorrow is fully ahead


def test_sun_position_and_irradiance():
    # Solar noon at the equinox: zenith ≈ latitude, sun due south.
    noon = datetime(2026, 3, 20, 11, 14, tzinfo=timezone.utc).timestamp()  # ≈ solar noon at 13.40° E
    zenith, azimuth = sun_position(noon, LAT, LON)
    assert abs(zenith - LAT) < 1.5 and abs(azimuth) < 5
    morning_az = sun_position(datetime(2026, 3, 20, 7, 0, tzinfo=timezone.utc).timestamp(), LAT, LON)[1]
    assert morning_az < -45  # east in the morning
    # A south-facing 30° plane gets more than the horizontal at noon; a north-facing one less.
    south = plane_irradiance(600, 800, 100, zenith, azimuth, 30, 0)
    north = plane_irradiance(600, 800, 100, zenith, azimuth, 30, 180)
    assert south > 600 > north
    assert plane_irradiance(600, 800, 100, 95, 0, 30, 0) == 0.0  # sun below the horizon
    # warm cells lose power
    assert dc_power(6.3, 800, 30) < dc_power(6.3, 800, 0) < 6.3 * 800 * 1.1


def _clear_sky_weather(start: float, days: int, factor: float = 1.0) -> dict:
    """Open-Meteo minutely_15 answer with clear-sky irradiance (scaled by factor)."""
    times, ghi, dni, dhi, temp, cloud = [], [], [], [], [], []
    t = start
    while t < start + days * 86400:
        t += STEP_S
        zen, _ = sun_position(t - STEP_S / 2, LAT, LON)
        c = math.cos(math.radians(zen))
        g = 1098 * c * math.exp(-0.057 / c) * factor if c > 0.05 else 0.0
        d = 0.15 * g
        n = (g - d) / c if c > 0.05 else 0.0
        times.append(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M"))
        ghi.append(round(g, 1)); dni.append(round(n, 1)); dhi.append(round(d, 1))
        temp.append(15.0); cloud.append(0)
    return {"minutely_15": {"time": times, "shortwave_radiation": ghi, "direct_normal_irradiance": dni,
                            "diffuse_radiation": dhi, "temperature_2m": temp, "cloud_cover": cloud}}


class Measured:
    """Stub measurements: array power = ratio × model, constant house load."""

    def __init__(self, service_ref, ratios: dict[str, float], load_w: float = 500.0):
        self.service_ref, self.ratios, self.load_w = service_ref, ratios, load_w

    async def array_power(self, array, start, end):
        if array.name not in self.ratios:
            return {}
        svc = self.service_ref()
        return {p.end: self.ratios[array.name] * svc.modelled_power(array, p)
                for p in svc.state.weather if start <= p.end <= min(end, NOW)}

    async def load_power(self, start, end):
        t, out = math.ceil(start / STEP_S) * STEP_S, {}
        while t <= end:
            out[t] = self.load_w
            t += STEP_S
        return out


def _config(**over) -> ForecastConfig:
    arrays = [PVArray("south", 6.3, 30, 0, voltage="mppt1_voltage", current="mppt1_current"),
              PVArray("flat", 5.4, 15, 0, voltage="mppt3_voltage", current="mppt3_current")]
    return ForecastConfig(latitude=LAT, longitude=LON, appliance="inverter", arrays=arrays,
                          calibration_days=3, **over)


def _service(weather_answer, ratios=None, soc=50.0, capacity=10.0, **over) -> ForecastService:
    server = ServerConfig(devices=[DeviceConfig(name="inverter", host="127.0.0.1", profile="sungrow_sh")])
    config = _config(**over)
    transport = httpx.MockTransport(weather_answer)
    service = None
    measured = Measured(lambda: service, ratios if ratios is not None else {"south": 0.8})
    service = ForecastService(Hub(server), config, measured, "Europe/Berlin", OpenMeteo(config, transport))
    service.now = lambda: NOW

    async def battery():
        return soc, capacity

    service._battery = battery
    return service


def _answer(factor: float = 1.0):
    start = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc).timestamp()
    body = _clear_sky_weather(start, 5, factor)
    return lambda request: httpx.Response(200, json=body)


async def test_calibration_per_array():
    service = _service(_answer(), ratios={"south": 0.8})
    await service.refresh()
    status = {a["name"]: a for a in service.status()["arrays"]}
    assert status["south"]["calibrated"] and status["south"]["performance_ratio"] == pytest.approx(0.8, abs=0.01)
    assert not status["flat"]["calibrated"] and status["flat"]["performance_ratio"] == 0.85  # no data yet
    assert service.state.load_profile and service.expected_load(NOW) == 500


async def test_pv_forecast_days_and_resolution():
    service = _service(_answer(), ratios={"south": 0.8, "flat": 0.6})
    hourly = await service.pv_forecast(resolution="1h")
    today, tomorrow = hourly["days"]
    assert today == "2026-09-29" and tomorrow == "2026-09-30"
    t = hourly["days"][tomorrow]
    assert t["pv_kwh"] == pytest.approx(t["per_array_kwh"]["south"] + t["per_array_kwh"]["flat"], abs=0.2)
    assert t["per_array_kwh"]["south"] > t["per_array_kwh"]["flat"]  # bigger, steeper, better ratio
    assert hourly["days"][today]["remaining_kwh"] == 0  # evening
    peak = max((i for i in hourly["intervals"] if i["start"].startswith(tomorrow)), key=lambda i: i["pv_w"])
    assert peak["start"][11:13] in ("12", "13")  # around solar noon (≈ 12:57 CEST)
    quarter = await service.pv_forecast("tomorrow", "15m")
    assert len(quarter["intervals"]) == 96 and list(quarter["days"]) == [tomorrow]
    with pytest.raises(ForecastRequestError):
        await service.pv_forecast("next week")
    with pytest.raises(ForecastRequestError):
        await service.pv_forecast("", "5m")


async def test_surplus_windows_with_battery():
    service = _service(_answer(), ratios={"south": 0.8, "flat": 0.6}, soc=50.0, capacity=10.0)
    result = await service.surplus(resolution="1h")
    assert result["battery"]["full_at"].startswith("2026-09-30")
    [window] = result["windows"]  # one sunny afternoon
    assert window["start"] >= result["battery"]["full_at"]  # export only once the battery is full
    assert window["export_kwh"] > 5 and window["peak_export_w"] >= window["avg_export_w"] >= 1000
    tomorrow = result["days"]["2026-09-30"]
    assert tomorrow["pv_kwh"] > tomorrow["export_kwh"]
    assert result["days"]["2026-09-29"]["import_kwh"] == 0  # battery covers the evening
    socs = [i["soc"] for i in result["intervals"]]
    assert min(socs) >= 5 and max(socs) == 100
    # a high threshold leaves only the strongest part (or nothing)
    strict = await service.surplus(threshold_w=50000)
    assert strict["windows"] == []


async def test_night_without_battery_imports():
    service = _service(_answer(), ratios={"south": 0.8}, soc=0, capacity=0)
    result = await service.surplus()
    assert result["battery"]["soc_now"] is None
    assert result["days"]["2026-09-29"]["import_kwh"] > 0  # evening load from the grid


async def test_open_meteo_outage():
    calls = {"ok": True}

    def answer(request):
        if not calls["ok"]:
            raise httpx.ConnectError("down")
        return _answer()(request)

    service = _service(answer)
    await service.refresh()
    calls["ok"] = False
    with pytest.raises(UnavailableError):
        await service.refresh()
    kept = await service.pv_forecast("tomorrow")  # previous forecast still served
    assert kept["stale"] is True and "down" in kept["last_error"] and kept["days"]

    fresh = _service(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    with pytest.raises(ForecastUnavailableError) as err:
        await fresh.pv_forecast()
    assert err.value.status == 503 and err.value.retry_after


def test_forecast_config():
    data = {"latitude": LAT, "longitude": LON, "appliance": "PV",
            "arrays": [{"name": "a", "kwp": 6.3, "tilt": 30, "power": "pv_power"}]}
    cfg = ForecastConfig.from_dict(data)
    server = ServerConfig(devices=[DeviceConfig(name="inverter", host="x", profile="sungrow_sh", aliases=["PV"])],
                          forecast=cfg)
    assert server.forecast.appliance == "inverter" and cfg.arrays[0].azimuth == 0
    with pytest.raises(ConfigError, match="voltage"):
        ForecastConfig.from_dict({**data, "arrays": [{"name": "a", "kwp": 1, "tilt": 30}]})
    with pytest.raises(ConfigError, match="latitude"):
        ForecastConfig.from_dict({**data, "latitude": 123})
    with pytest.raises(ConfigError, match="Unknown forecast option"):
        ForecastConfig.from_dict({**data, "wind": True})


async def test_mcp_and_rest():
    service = _service(_answer(), ratios={"south": 0.8, "flat": 0.6})
    server_config = ServerConfig(devices=[DeviceConfig(name="inverter", host="127.0.0.1", profile="sungrow_sh")],
                                 service=ServiceConfig())
    services = Services(server_config, service.hub, forecast=service)
    mcp = build_server(services)
    names = {t.name for t in await mcp.list_tools()}
    assert {"get_pv_forecast", "get_surplus_windows"} <= names
    pv = json.loads((await mcp.call_tool("get_pv_forecast", {"day": "tomorrow"})).content[0].text)
    assert pv["days"]["2026-09-30"]["pv_kwh"] > 0
    bad = json.loads((await mcp.call_tool("get_pv_forecast", {"resolution": "5m"})).content[0].text)
    assert "resolution" in bad["error"]
    surplus = json.loads((await mcp.call_tool("get_surplus_windows", {})).content[0].text)
    assert surplus["windows"] and "intervals" not in surplus

    from housevitals.service import build_app
    app = build_app(server_config, services, metric_readers=[])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        assert (await c.get("/api/v1/forecast")).json()["source"] == "Open-Meteo"
        assert (await c.get("/api/v1/forecast/pv?day=tomorrow&resolution=15m")).status_code == 200
        assert (await c.get("/api/v1/forecast/pv?resolution=5m")).status_code == 422  # validated by FastAPI
        surplus = (await c.get("/api/v1/forecast/surplus?resolution=1h")).json()
        assert surplus["windows"] and surplus["intervals"]
