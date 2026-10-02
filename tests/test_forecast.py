"""PV forecast and surplus windows (Open-Meteo mocked, measurements stubbed)."""

import asyncio
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

    def __init__(self, service_ref, ratios: dict[str, float], load_w: float = 500.0,
                 soc: float = 50.0, capacity: float = 10.0):
        self.service_ref, self.ratios, self.load_w = service_ref, ratios, load_w
        self.soc, self.capacity = soc, capacity

    async def battery(self):
        return self.soc, self.capacity

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


def _hub() -> Hub:
    return Hub(ServerConfig(devices=[DeviceConfig(name="inverter", host="127.0.0.1", profile="sungrow_sh")]))


def _service(weather_answer, ratios=None, soc=50.0, capacity=10.0, **over) -> ForecastService:
    config = _config(**over)
    transport = httpx.MockTransport(weather_answer)
    service = None
    measured = Measured(lambda: service, ratios if ratios is not None else {"south": 0.8},
                        soc=soc, capacity=capacity)
    service = ForecastService(config, measured, "Europe/Berlin", OpenMeteo(config, transport))
    service.now = lambda: NOW
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
    assert datetime.fromisoformat(peak["start"]).timestamp() == peak["ts"]  # epoch for Grafana
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

    attempts = []

    def down(request):
        attempts.append(request)
        raise httpx.ConnectError("down")

    fresh = _service(down)
    with pytest.raises(ForecastUnavailableError) as err:
        await fresh.pv_forecast()
    assert err.value.status == 503 and err.value.retry_after
    with pytest.raises(ForecastRequestError):  # invalid input is reported first
        await fresh.pv_forecast("next week")
    with pytest.raises(ForecastUnavailableError) as err:  # no new attempt within RETRY_S
        await fresh.surplus()
    assert len(attempts) == 1 and "down" in str(err.value) and 0 < err.value.retry_after <= 300


async def test_requests_do_not_wait_for_open_meteo_while_the_loop_runs():
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()  # Open-Meteo hangs
        return _answer()(request)

    service = _service(lambda r: httpx.Response(200))  # replaced below
    service.source = OpenMeteo(service.config, httpx.MockTransport(slow))
    await service.start()
    await asyncio.sleep(0.05)
    started = asyncio.get_running_loop().time()
    with pytest.raises(ForecastUnavailableError, match="being fetched") as err:
        await service.pv_forecast()
    assert asyncio.get_running_loop().time() - started < 0.1 and err.value.retry_after
    gate.set()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if service.state.weather:
            break
    assert (await service.pv_forecast("tomorrow"))["days"]
    await service.stop()


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
    services = Services(server_config, _hub(), forecast=service)
    mcp = build_server(services)
    names = {t.name for t in await mcp.list_tools()}
    assert {"get_pv_forecast", "get_surplus_windows", "get_weather_forecast"} <= names
    pv = json.loads((await mcp.call_tool("get_pv_forecast", {"day": "tomorrow"})).content[0].text)
    assert pv["days"]["2026-09-30"]["pv_kwh"] > 0
    bad = json.loads((await mcp.call_tool("get_pv_forecast", {"resolution": "5m"})).content[0].text)
    assert "resolution" in bad["error"]
    surplus = json.loads((await mcp.call_tool("get_surplus_windows", {})).content[0].text)
    assert surplus["windows"] and "intervals" not in surplus
    weather = json.loads((await mcp.call_tool("get_weather_forecast", {"day": "today"})).content[0].text)
    assert list(weather["days"]) == ["2026-09-29"] and weather["intervals"][0]["temperature_c"] == 15.0

    from housevitals.service import build_app
    app = build_app(server_config, services, metric_readers=[])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        assert (await c.get("/api/v1/forecast")).json()["source"] == "Open-Meteo"
        assert (await c.get("/api/v1/forecast/pv?day=tomorrow&resolution=15m")).status_code == 200
        assert (await c.get("/api/v1/forecast/pv?resolution=5m")).status_code == 422  # validated by FastAPI
        surplus = (await c.get("/api/v1/forecast/surplus?resolution=1h")).json()
        assert surplus["windows"] and surplus["intervals"]
        weather = (await c.get("/api/v1/forecast/weather?resolution=15m")).json()
        assert len(weather["intervals"]) == 2 * 96 and weather["intervals"][0]["ts"]


def _rainy_answer():
    """Clear sky, but temperature rising 0.1 K per quarter hour, and tomorrow 06:00–07:00 UTC
    rain (0.5 mm per quarter hour), 07:00–07:15 snow."""
    start = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc).timestamp()
    body = _clear_sky_weather(start, 5)
    m = body["minutely_15"]
    n = len(m["time"])
    m["temperature_2m"] = [round(10 + 0.1 * i, 1) for i in range(n)]
    m["precipitation"], m["rain"], m["snowfall"], m["weather_code"] = [0.0] * n, [0.0] * n, [0.0] * n, [0] * n
    for i, t in enumerate(m["time"]):
        if "2026-09-30T06:15" <= t <= "2026-09-30T07:00":
            m["precipitation"][i] = m["rain"][i] = 0.5
            m["weather_code"][i] = 63
        elif t == "2026-09-30T07:15":
            m["precipitation"][i], m["snowfall"][i], m["weather_code"][i] = 0.7, 1.0, 73
    hours = [datetime.fromtimestamp(start + h * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M") for h in range(5 * 24)]
    body["hourly"] = {"time": hours,
                      "precipitation_probability": [80 if h.startswith("2026-09-30T07") else 5 for h in hours]}
    return lambda request: httpx.Response(200, json=body)


async def test_weather_forecast():
    service = _service(_rainy_answer())
    await service.refresh()
    # now = 18:00 UTC; the points around it are 18:00 and 18:15 UTC
    i = next(i for i, p in enumerate(service.state.weather) if p.end == NOW)
    current = service.current_weather()
    assert current["temperature"] == pytest.approx(service.state.weather[i].temperature)  # exactly on a point
    assert current["precipitation"] == 0 and current["snowfall"] == 0
    service.now = lambda: NOW + 450  # halfway: interpolated
    assert service.current_weather()["temperature"] == pytest.approx(service.state.weather[i].temperature + 0.05)
    service.now = lambda: NOW

    hourly = await service.weather("tomorrow", "1h")
    day = hourly["days"]["2026-09-30"]
    assert day["precipitation_mm"] == pytest.approx(2.7) and day["snowfall_cm"] == 1.0
    assert day["precipitation_probability_max"] == 80
    assert day["temperature_max_c"] - day["temperature_min_c"] == pytest.approx(9.5)  # 96 points × 0.1 K
    rows = {r["start"][11:16]: r for r in hourly["intervals"]}  # local time (UTC+2)
    assert rows["08:00"]["rain_mm"] == 2.0 and rows["08:00"]["condition"] == "rain"
    assert rows["09:00"]["snowfall_cm"] == 1.0 and rows["09:00"]["condition"] == "snow"
    assert rows["09:00"]["snow_mm"] == 0.7 and rows["08:00"]["snow_mm"] == 0
    # hourly probability covers the preceding hour: 07:00 UTC -> 06:00–07:00 UTC = 08:00 local
    assert rows["08:00"]["precipitation_probability"] == 80 and rows["09:00"]["precipitation_probability"] == 5
    assert rows["12:00"]["condition"] == "clear" and rows["12:00"]["cloud_cover"] == 0
    # mean temperature of the hour: instants 0.1 K apart, the interval mean lies between them
    assert rows["11:00"]["temperature_c"] == pytest.approx(rows["10:00"]["temperature_c"] + 0.4, abs=0.05)
    quarter = await service.weather("", "15m")
    assert len(quarter["intervals"]) == 2 * 96
    with pytest.raises(ForecastRequestError):
        await service.weather("", "5m")


def test_weather_without_precipitation_columns():
    # older answers (and the PV-only mock) have no precipitation: values stay None
    service = _service(_answer())
    asyncio.run(service.refresh())
    point = service.state.weather[0]
    assert point.precipitation is None and point.precipitation_probability is None and point.rain is None
    assert "precipitation" not in service.current_weather()


async def test_pv_forecast_chart():
    from housevitals.charts import ChartService, UnknownChartError
    from housevitals.history import HistoryError

    service = _service(_answer(), ratios={"south": 0.8, "flat": 0.6})

    class NoHistory:  # Prometheus down: the chart still shows the forecast
        tz = TZ

        class prometheus:
            @staticmethod
            def open():
                return False

        def now(self):
            return datetime.fromtimestamp(NOW, TZ)

        async def history(self, *args, **kwargs):
            raise HistoryError("no data in this time range")

    without = ChartService(_hub(), NoHistory())
    assert "pv_forecast" not in [c["chart"] for c in without.catalog()]  # no forecast configured
    with pytest.raises(UnknownChartError, match="forecast"):
        await without.get("pv_forecast")
    charts = ChartService(_hub(), NoHistory(), service)
    assert "pv_forecast" in [c["chart"] for c in charts.catalog()]
    image = await charts.get("pv_forecast", lang="de")
    assert image.png.startswith(b"\x89PNG") and image.summary["windows"]
    assert image.summary["battery"]["full_at"].startswith("2026-09-30")
    assert image.summary["measured_today_kwh"] == 0  # no measured values available
