"""PV forecast and surplus windows from Open-Meteo weather forecasts.

Every `refresh_s` the service fetches 15-minute irradiance and temperature for the
past `calibration_days` and the next two days from Open-Meteo, and

* models each PV array's DC power from the irradiance on its plane (tilt, azimuth,
  kWp; see solar.py),
* calibrates a performance ratio per array: measured energy / modelled energy over the
  past days (an energy ratio, so the timing errors of past forecasts, e.g. clouds an
  hour early, average out; it absorbs shading, soiling and inverter losses),
* builds a house load profile: mean load per quarter hour of the day,
* simulates the battery through today and tomorrow (PV - load, charge and discharge
  limits) and reports surplus windows: periods with expected export to the grid,
* reports the weather itself: temperature, cloud cover, rain, snow and the
  precipitation probability.

The latest forecast is kept in memory; if Open-Meteo is unreachable, the previous one
stays in use (marked stale). Measurements come from Prometheus through a small
interface, so the model is testable without it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from .config import ForecastConfig, PVArray
from .errors import HomeModbusError, UnavailableError
from .hub import Hub
from .solar import dc_power, plane_irradiance, sun_position

_LOGGER = logging.getLogger(__name__)

STEP_S = 900  # Open-Meteo minutely_15: values are means of the preceding 15 minutes

# WMO weather codes (Open-Meteo "weather_code") -> condition
CONDITIONS = [(0, "clear"), (3, "cloudy"), (48, "fog"), (57, "drizzle"), (67, "rain"),
              (77, "snow"), (82, "rain showers"), (86, "snow showers"), (99, "thunderstorm")]


def condition(code: int | None) -> str | None:
    if code is None:
        return None
    return next((name for limit, name in CONDITIONS if code <= limit), "unknown")
DEFAULT_PERFORMANCE_RATIO = 0.85
MIN_CALIBRATION_KWH = 2.0  # modelled energy needed before a calibration is trusted
DEFAULT_LOAD_W = 500.0
TIMEOUT = httpx.Timeout(20.0, connect=5.0)
RETRY_S = 300.0


class ForecastUnavailableError(UnavailableError):
    """No forecast yet (Open-Meteo not reached since the start)."""


class ForecastRequestError(HomeModbusError):
    """Invalid forecast request (day, resolution)."""


@dataclass(frozen=True)
class WeatherPoint:
    end: float  # unix time at the end of the 15-minute interval
    ghi: float
    dni: float
    dhi: float
    temperature: float | None  # °C at the end of the interval
    cloud_cover: float | None
    precipitation: float | None = None  # mm in the interval (rain, showers and melted snow)
    rain: float | None = None  # mm, rain and showers
    snowfall: float | None = None  # cm
    weather_code: int | None = None  # WMO code
    precipitation_probability: float | None = None  # % for the hour containing the interval


@dataclass
class Calibration:
    performance_ratio: float
    calibrated: bool
    measured_kwh: float
    modelled_kwh: float


class Measurements(Protocol):
    """What the forecast needs from the house (the only way it reaches the house)."""

    async def battery(self) -> tuple[float, float]:
        """(state of charge in %, usable capacity in kWh); (0, 0) without a battery."""
        ...

    async def array_power(self, array: PVArray, start: float, end: float) -> dict[float, float]:
        """15-minute mean power in W by interval end."""

    async def load_power(self, start: float, end: float) -> dict[float, float]:
        """15-minute mean house load in W by interval end."""


class OpenMeteo:
    VARIABLES = ("shortwave_radiation,direct_normal_irradiance,diffuse_radiation,temperature_2m,cloud_cover,"
                 "precipitation,rain,showers,snowfall,weather_code")
    HOURLY = "precipitation_probability"  # not available per 15 minutes

    def __init__(self, config: ForecastConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport

    async def fetch(self, past_days: int, forecast_days: int = 2) -> list[WeatherPoint]:
        params = {"latitude": self.config.latitude, "longitude": self.config.longitude,
                  "minutely_15": self.VARIABLES, "hourly": self.HOURLY, "past_days": past_days,
                  "forecast_days": forecast_days, "timezone": "UTC"}
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
            resp = await client.get(self.config.open_meteo_url, params=params)
        body = resp.json()
        if resp.status_code != 200 or body.get("error"):
            raise UnavailableError(f"Open-Meteo: {body.get('reason', resp.status_code)}")
        m = body["minutely_15"]
        hourly = body.get("hourly") or {}
        # probability of the preceding hour, keyed by the hour's end
        probability = dict(zip((_utc(t) for t in hourly.get("time", [])),
                               hourly.get("precipitation_probability", [])))
        column = lambda name, i: m[name][i] if name in m else None  # noqa: E731

        def liquid(i: int) -> float | None:  # rain + showers
            parts = [column("rain", i), column("showers", i)]
            return None if parts[0] is None else sum(v or 0.0 for v in parts)
        points = []
        for i, t in enumerate(m["time"]):
            ghi = m["shortwave_radiation"][i]
            if ghi is None:
                continue
            end = _utc(t)
            code = column("weather_code", i)
            points.append(WeatherPoint(end, ghi, m["direct_normal_irradiance"][i] or 0.0,
                                       m["diffuse_radiation"][i] or 0.0, m["temperature_2m"][i],
                                       m["cloud_cover"][i], column("precipitation", i), liquid(i),
                                       column("snowfall", i), None if code is None else int(code),
                                       probability.get(math.ceil(end / 3600) * 3600)))
        return points


def _utc(t: str) -> float:
    return datetime.fromisoformat(t).replace(tzinfo=ZoneInfo("UTC")).timestamp()


@dataclass
class _State:
    weather: list[WeatherPoint] = field(default_factory=list)
    fetched_at: float | None = None
    last_error: str | None = None
    last_attempt: float | None = None
    calibration: dict[str, Calibration] = field(default_factory=dict)
    load_profile: dict[str, float] = field(default_factory=dict)  # "HH:MM" local -> W


class ForecastService:
    def __init__(self, config: ForecastConfig, measurements: Measurements, timezone: str,
                 weather: OpenMeteo | None = None):
        self.config = config
        self.measurements = measurements
        self.tz = ZoneInfo(timezone)
        self.source = weather or OpenMeteo(config)
        self.state = _State()
        self._task: asyncio.Task | None = None
        self._refreshing = asyncio.Lock()  # one fetch at a time

    def now(self) -> float:
        return time.time()

    # ------------------------------------------------------------------ refresh
    async def refresh(self) -> None:
        """Fetch the weather, recalibrate and rebuild the load profile."""
        async with self._refreshing:
            await self._refresh()

    async def _refresh(self) -> None:
        self.state.last_attempt = self.now()
        try:
            weather = await self.source.fetch(self.config.calibration_days)
        except (httpx.HTTPError, UnavailableError, ValueError, KeyError, TypeError) as err:
            message = str(err) or type(err).__name__
            if self.state.last_error != message:
                _LOGGER.warning("PV forecast: Open-Meteo not available: %s", message)
            self.state.last_error = message
            raise UnavailableError(f"Open-Meteo not reachable: {message}") from err
        if self.state.last_error:
            _LOGGER.warning("PV forecast: Open-Meteo reachable again")
        self.state.weather, self.state.fetched_at, self.state.last_error = weather, self.now(), None
        await self._calibrate()
        await self._build_load_profile()

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
                delay = self.config.refresh_s
            except UnavailableError:
                delay = min(self.config.refresh_s, RETRY_S)
            except Exception:  # keep the loop alive
                _LOGGER.exception("PV forecast refresh failed")
                delay = RETRY_S
            await asyncio.sleep(delay)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="pv-forecast")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def ensure(self) -> None:
        """A forecast must exist. Requests never wait for Open-Meteo while the background
        loop runs; without the loop (stdio mode, tests) the first request fetches, at most
        once per RETRY_S after a failure."""
        if self.state.weather:
            return
        s = self.state
        since = None if s.last_attempt is None else self.now() - s.last_attempt
        if self._task is not None or self._refreshing.locked() or (s.last_error and since < RETRY_S):
            retry = RETRY_S - since if since is not None and s.last_error else 30.0
            raise ForecastUnavailableError(
                "No weather forecast yet" + (f" (Open-Meteo: {s.last_error})" if s.last_error else
                                             "; it is being fetched"),
                retry_after=max(1.0, retry))
        try:
            await self.refresh()
        except UnavailableError as err:
            raise ForecastUnavailableError(str(err), retry_after=RETRY_S) from err

    # ------------------------------------------------------------------ model
    def modelled_power(self, array: PVArray, point: WeatherPoint) -> float:
        """DC power in W at a performance ratio of 1 for the interval ending at point.end."""
        zenith, sun_azimuth = sun_position(point.end - STEP_S / 2, self.config.latitude, self.config.longitude)
        irradiance = plane_irradiance(point.ghi, point.dni, point.dhi, zenith, sun_azimuth,
                                      array.tilt, array.azimuth)
        return dc_power(array.kwp, irradiance, point.temperature)

    def performance_ratio(self, array: PVArray) -> float:
        cal = self.state.calibration.get(array.name)
        return cal.performance_ratio if cal else DEFAULT_PERFORMANCE_RATIO

    def pv_power(self, point: WeatherPoint) -> dict[str, float]:
        """Forecast power in W per array and in total ("total") for one interval."""
        out = {a.name: self.performance_ratio(a) * self.modelled_power(a, point) for a in self.config.arrays}
        total = sum(out.values())
        if self.config.max_ac_w is not None:
            total = min(total, self.config.max_ac_w)
        out["total"] = total
        return out

    async def _calibrate(self) -> None:
        now = self.now()
        past = [p for p in self.state.weather if p.end <= now]
        if not past:
            return
        start, end = past[0].end, past[-1].end
        for array in self.config.arrays:
            try:
                measured = await self.measurements.array_power(array, start, end)
            except UnavailableError as err:
                _LOGGER.info("PV forecast: calibration of %s skipped: %s", array.name, err)
                continue
            meas = model = 0.0
            for p in past:
                m = measured.get(p.end)
                modelled = self.modelled_power(array, p)
                if m is None or modelled <= 20:
                    continue
                meas += m
                model += modelled
            meas_kwh, model_kwh = meas * STEP_S / 3.6e6, model * STEP_S / 3.6e6
            if model_kwh >= MIN_CALIBRATION_KWH:
                ratio = max(0.1, min(1.2, meas_kwh / model_kwh))
                self.state.calibration[array.name] = Calibration(round(ratio, 3), True, round(meas_kwh, 2),
                                                                 round(model_kwh, 2))
            else:
                self.state.calibration[array.name] = Calibration(DEFAULT_PERFORMANCE_RATIO, False,
                                                                 round(meas_kwh, 2), round(model_kwh, 2))

    async def _build_load_profile(self) -> None:
        now = self.now()
        start = now - self.config.calibration_days * 86400
        try:
            load = await self.measurements.load_power(start, now)
        except UnavailableError as err:
            _LOGGER.info("PV forecast: load profile not updated: %s", err)
            return
        slots: dict[str, list[float]] = {}
        for end, watts in load.items():
            slots.setdefault(self._slot(end), []).append(watts)
        self.state.load_profile = {slot: sum(v) / len(v) for slot, v in slots.items()}

    def _slot(self, end: float) -> str:
        return datetime.fromtimestamp(end - STEP_S, self.tz).strftime("%H:%M")

    def expected_load(self, end: float) -> float:
        profile = self.state.load_profile
        if not profile:
            return DEFAULT_LOAD_W
        return profile.get(self._slot(end), sum(profile.values()) / len(profile))

    # ------------------------------------------------------------------ metrics (sync)
    def current_power(self) -> dict[str, float] | None:
        """Forecast power per array and total for the running 15-minute interval."""
        now = self.now()
        point = next((p for p in self.state.weather if p.end > now), None)
        return self.pv_power(point) if point is not None and point.end - now <= STEP_S else None

    def current_weather(self) -> dict[str, float] | None:
        """Forecast temperature now (interpolated), precipitation in mm/h and snowfall in cm/h
        of the running quarter hour."""
        now = self.now()
        weather = self.state.weather
        i = next((i for i, p in enumerate(weather) if p.end > now), None)
        if i is None or weather[i].end - now > STEP_S:
            return None
        point = weather[i]
        out: dict[str, float] = {}
        before = weather[i - 1] if i > 0 and point.end - weather[i - 1].end == STEP_S else None
        if point.temperature is not None:
            out["temperature"] = point.temperature
            if before is not None and before.temperature is not None:
                f = (now - before.end) / STEP_S
                out["temperature"] = before.temperature + f * (point.temperature - before.temperature)
        if point.precipitation is not None:
            out["precipitation"] = point.precipitation * 3600 / STEP_S
        if point.snowfall is not None:
            out["snowfall"] = point.snowfall * 3600 / STEP_S
        return out

    def day_energy(self) -> dict[str, float]:
        """Forecast PV energy in kWh for "today" and "tomorrow" (whole days); {} without a forecast."""
        out: dict[str, float] = {}
        if not self.state.weather:
            return out
        for label, d in zip(("today", "tomorrow"), self._days()):
            start, end = self._day_bounds(d)
            out[label] = sum(self.pv_power(p)["total"] for p in self.state.weather
                             if start < p.end <= end) * STEP_S / 3.6e6
        return out

    def performance_ratios(self) -> dict[str, float]:
        """Calibrated performance ratio per array (arrays not calibrated yet are left out)."""
        return {name: cal.performance_ratio for name, cal in self.state.calibration.items()
                if cal.calibrated}

    def age_s(self) -> float | None:
        """Age of the forecast in use in seconds; None before the first fetch."""
        return None if self.state.fetched_at is None else self.now() - self.state.fetched_at

    # ------------------------------------------------------------------ views
    def _day_bounds(self, day: date) -> tuple[float, float]:
        start = datetime.combine(day, datetime.min.time(), self.tz)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    def _days(self) -> list[date]:
        today = datetime.fromtimestamp(self.now(), self.tz).date()
        return [today, today + timedelta(days=1)]

    def _iso(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).isoformat(timespec="minutes")

    def status(self) -> dict[str, Any]:
        s = self.state
        return {
            "source": "Open-Meteo",
            "issued_at": self._iso(s.fetched_at) if s.fetched_at else None,
            "age_s": round(self.now() - s.fetched_at) if s.fetched_at else None,
            "stale": bool(s.last_error) or (s.fetched_at is not None
                                            and self.now() - s.fetched_at > 3 * self.config.refresh_s),
            **({"last_error": s.last_error} if s.last_error else {}),
            "arrays": [{
                "name": a.name, "kwp": a.kwp, "tilt": a.tilt, "azimuth": a.azimuth,
                "performance_ratio": self.performance_ratio(a),
                "calibrated": bool(s.calibration.get(a.name) and s.calibration[a.name].calibrated),
                **({"measured_kwh": s.calibration[a.name].measured_kwh,
                    "modelled_kwh": s.calibration[a.name].modelled_kwh} if a.name in s.calibration else {}),
            } for a in self.config.arrays],
        }

    async def pv_forecast(self, day: str = "", resolution: str = "1h") -> dict[str, Any]:
        """Forecast power per interval and energy per day (today, tomorrow)."""
        days = self._select_days(day)  # validate before anything else
        _step_seconds(resolution)
        await self.ensure()
        now = self.now()
        out_days, intervals = {}, []
        for d in days:
            start, end = self._day_bounds(d)
            points = [p for p in self.state.weather if start < p.end <= end]
            powers = [(p, self.pv_power(p)) for p in points]
            total = sum(pw["total"] for _, pw in powers) * STEP_S / 3.6e6
            remaining = sum(pw["total"] for p, pw in powers if p.end > now) * STEP_S / 3.6e6
            out_days[d.isoformat()] = {
                "pv_kwh": round(total, 1),
                **({"remaining_kwh": round(remaining, 1)} if start <= now < end else {}),
                "per_array_kwh": {a.name: round(sum(pw[a.name] for _, pw in powers) * STEP_S / 3.6e6, 1)
                                  for a in self.config.arrays},
            }
            intervals += self._resample(powers, resolution)
        return {**self.status(), "days": out_days, "resolution": resolution,
                "note": "power is the mean over each interval (start given; ts = unix seconds), in W; "
                        "performance ratios are calibrated against the measured array power",
                "intervals": intervals}

    async def weather(self, day: str = "", resolution: str = "1h") -> dict[str, Any]:
        """Temperature, clouds, rain and snow per interval; extremes and sums per day."""
        days = self._select_days(day)  # validate before anything else
        _step_seconds(resolution)
        await self.ensure()
        out_days, rows = {}, []
        by_end = {p.end: p for p in self.state.weather}
        for d in days:
            start, end = self._day_bounds(d)
            points = [p for p in self.state.weather if start < p.end <= end]
            for p in points:
                previous = by_end.get(p.end - STEP_S)
                temps = [t for t in (p.temperature, previous.temperature if previous else None) if t is not None]
                rows.append({"end": p.end, "temperature": sum(temps) / len(temps) if temps else None,
                             "point": p})
            temps = [p.temperature for p in points if p.temperature is not None]
            out_days[d.isoformat()] = {
                "temperature_min_c": min(temps, default=None), "temperature_max_c": max(temps, default=None),
                "precipitation_mm": _sum(p.precipitation for p in points),
                "snowfall_cm": _sum(p.snowfall for p in points),
                "precipitation_probability_max": _max(p.precipitation_probability for p in points),
            }
        return {"source": "Open-Meteo", **{k: v for k, v in self.status().items() if k != "arrays"},
                "days": out_days, "resolution": resolution,
                "note": "per interval (start given; ts = unix seconds): mean temperature, mean cloud cover, "
                        "precipitation in mm = rain (incl. showers) + snow (as water), snowfall in cm of fresh snow, "
                        "precipitation probability of the hour in %; condition from the WMO weather code",
                "intervals": self._weather_rows(rows, resolution)}

    def _weather_rows(self, rows: list[dict], resolution: str) -> list[dict]:
        step = _step_seconds(resolution)
        buckets: dict[float, list[dict]] = {}
        for r in rows:
            start = r["end"] - STEP_S
            buckets.setdefault(start - (start % step if step > STEP_S else 0), []).append(r)
        out = []
        for start, items in sorted(buckets.items()):
            points = [r["point"] for r in items]
            temps = [r["temperature"] for r in items if r["temperature"] is not None]
            clouds = [p.cloud_cover for p in points if p.cloud_cover is not None]
            code = _max(p.weather_code for p in points)  # higher codes are the more severe weather
            out.append({
                "start": self._iso(start), "ts": int(start),
                "temperature_c": round(sum(temps) / len(temps), 1) if temps else None,
                "cloud_cover": round(sum(clouds) / len(clouds)) if clouds else None,
                "precipitation_mm": _sum(p.precipitation for p in points),
                "rain_mm": _sum(p.rain for p in points),
                "snow_mm": _sum(_snow_water(p) for p in points),
                "snowfall_cm": _sum(p.snowfall for p in points),
                "precipitation_probability": _max(p.precipitation_probability for p in points),
                "weather_code": code, "condition": condition(code),
            })
        return out

    async def surplus(self, threshold_w: float | None = None, resolution: str = "") -> dict[str, Any]:
        """Simulate the battery from now to the end of tomorrow; windows of grid export."""
        if resolution:
            _step_seconds(resolution)  # validate before anything else
        await self.ensure()
        threshold = self.config.surplus_threshold_w if threshold_w in (None, 0) else float(threshold_w)
        soc, capacity = await self._battery()
        cfg = self.config
        now = self.now()
        end_of_tomorrow = self._day_bounds(self._days()[1])[1]
        points = [p for p in self.state.weather if now < p.end <= end_of_tomorrow]
        dt_h = STEP_S / 3600
        min_kwh = capacity * cfg.battery_min_soc / 100
        stored = capacity * soc / 100
        rows, full_at = [], None
        for p in points:
            pv = self.pv_power(p)["total"]
            load = self.expected_load(p.end)
            net = pv - load
            export = grid_import = 0.0
            if net >= 0:
                charge = min(net, cfg.battery_max_charge_w, max(0.0, (capacity - stored) / dt_h * 1000))
                stored += charge * dt_h / 1000
                export = net - charge
            else:
                discharge = min(-net, cfg.battery_max_discharge_w, max(0.0, (stored - min_kwh) / dt_h * 1000))
                stored -= discharge * dt_h / 1000
                grid_import = -net - discharge
            soc_now = 100 * stored / capacity if capacity else None
            if full_at is None and capacity and stored >= capacity - 1e-6:
                full_at = p.end - STEP_S
            rows.append({"end": p.end, "pv_w": pv, "load_w": load, "export_w": export,
                         "import_w": grid_import, "soc": soc_now})
        windows = self._windows(rows, threshold)
        days = {}
        for d in self._days():
            start, end = self._day_bounds(d)
            day_rows = [r for r in rows if start < r["end"] <= end]
            days[d.isoformat()] = {k: round(sum(r[f"{k[:-4]}_w"] for r in day_rows) * dt_h / 1000, 1)
                                   for k in ("pv_kwh", "load_kwh", "export_kwh", "import_kwh")}
        result = {
            **{k: v for k, v in self.status().items() if k != "arrays"},
            "threshold_w": threshold,
            "battery": {"soc_now": round(soc, 1) if capacity else None, "capacity_kwh": capacity or None,
                        "full_at": self._iso(full_at) if full_at else None},
            "windows": windows,
            "days": days,
            "note": "expected grid export while the battery is full or charging at its limit; "
                    "house load from the average profile per quarter hour of the past days; "
                    "today's day values count from now",
        }
        if resolution:
            result["intervals"] = self._resample_rows(rows, resolution)
        return result

    # ------------------------------------------------------------------ helpers
    def _select_days(self, day: str) -> list[date]:
        today, tomorrow = self._days()
        choice = (day or "").strip().lower()
        if choice in ("", "all", "both"):
            return [today, tomorrow]
        if choice == "today":
            return [today]
        if choice == "tomorrow":
            return [tomorrow]
        try:
            wanted = date.fromisoformat(choice)
        except ValueError as err:
            raise ForecastRequestError("day must be today, tomorrow or an ISO date") from err
        if wanted not in (today, tomorrow):
            raise ForecastRequestError(f"Forecasts cover {today} and {tomorrow} only")
        return [wanted]

    def _resample(self, powers: list[tuple[WeatherPoint, dict[str, float]]], resolution: str) -> list[dict]:
        step = _step_seconds(resolution)
        buckets: dict[float, list[dict[str, float]]] = {}
        for p, pw in powers:
            start = p.end - STEP_S
            buckets.setdefault(start - (start % step if step > STEP_S else 0), []).append(pw)
        out = []
        for start, items in sorted(buckets.items()):
            mean = {k: sum(i[k] for i in items) / len(items) for k in items[0]}
            out.append({"start": self._iso(start), "ts": int(start), "pv_w": round(mean["total"]),
                        "per_array_w": {a.name: round(mean[a.name]) for a in self.config.arrays}})
        return out

    def _resample_rows(self, rows: list[dict], resolution: str) -> list[dict]:
        step = _step_seconds(resolution)
        buckets: dict[float, list[dict]] = {}
        for r in rows:
            start = r["end"] - STEP_S
            buckets.setdefault(start - (start % step if step > STEP_S else 0), []).append(r)
        out = []
        for start, items in sorted(buckets.items()):
            n = len(items)
            out.append({"start": self._iso(start), "ts": int(start),
                        **{k: round(sum(i[k] for i in items) / n) for k in ("pv_w", "load_w", "export_w", "import_w")},
                        "soc": None if items[-1]["soc"] is None else round(items[-1]["soc"], 1)})
        return out

    def _windows(self, rows: list[dict], threshold: float) -> list[dict]:
        windows, current = [], []
        for r in rows + [None]:
            if r is not None and r["export_w"] >= threshold:
                current.append(r)
                continue
            if current:
                energy = sum(c["export_w"] for c in current) * STEP_S / 3.6e6
                windows.append({
                    "start": self._iso(current[0]["end"] - STEP_S),
                    "end": self._iso(current[-1]["end"]),
                    "duration_min": len(current) * STEP_S // 60,
                    "export_kwh": round(energy, 1),
                    "avg_export_w": round(sum(c["export_w"] for c in current) / len(current)),
                    "peak_export_w": round(max(c["export_w"] for c in current)),
                })
                current = []
        return windows

    async def _battery(self) -> tuple[float, float]:
        if not (self.config.battery_soc and self.config.battery_capacity):
            return 0.0, 0.0
        return await self.measurements.battery()


def _snow_water(p: WeatherPoint) -> float | None:
    """Snow as water in mm: the part of the precipitation that is not rain."""
    if p.precipitation is None or p.rain is None:
        return None
    if not p.snowfall:
        return 0.0  # the rest is rounding (values come in steps of 0.1 mm)
    return max(0.0, p.precipitation - p.rain)


def _sum(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values), 2) if values else None


def _max(values):
    return max((v for v in values if v is not None), default=None)


def _step_seconds(resolution: str) -> int:
    value = (resolution or "1h").strip().lower()
    if value in ("15m", "15min", "quarter"):
        return STEP_S
    if value in ("1h", "hour", "hourly"):
        return 3600
    raise ForecastRequestError("resolution must be 15m or 1h")


class HouseMeasurements:
    """Measurements of the house: live values from the hub's cache, history from Prometheus
    (15-minute means, all series combined). Uses only the public interfaces of both."""

    def __init__(self, hub: Hub, history, config: ForecastConfig):
        self.hub = hub
        self.history = history
        self.config = config

    async def battery(self) -> tuple[float, float]:
        cfg = self.config
        app = self.hub.get(cfg.appliance)
        regs = [app.profile.registers[k] for k in (cfg.battery_soc, cfg.battery_capacity)]
        values = await app.read(regs)  # from the cache while it is fresh
        soc, capacity = (values[r.key].get("value") for r in regs)
        if soc is None or not capacity:
            return 0.0, 0.0
        return float(soc), float(capacity)

    async def _series(self, expr: str, start: float, end: float) -> dict[float, float]:
        start = math.ceil(start / STEP_S) * STEP_S  # align to quarter-hour ends
        if start > end:
            return {}
        query = f"avg_over_time(({expr})[{STEP_S}s:15s])"
        result = await self.history.query_range(query, datetime.fromtimestamp(start),
                                                datetime.fromtimestamp(end), STEP_S)
        return {float(t): float(v) for r in result for t, v in r["values"]}

    def _point(self, key: str) -> str:
        return self.history.point(self.config.appliance, key)

    async def array_power(self, array: PVArray, start: float, end: float) -> dict[float, float]:
        expr = (self._point(array.power) if array.power
                else f"{self._point(array.voltage)} * {self._point(array.current)}")
        return await self._series(expr, start, end)

    async def load_power(self, start: float, end: float) -> dict[float, float]:
        return await self._series(self._point(self.config.load), start, end)
