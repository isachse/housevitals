"""Pre-rendered charts (PNG) from the recorded history, for MCP answers and the REST API.

A fixed catalog of charts is drawn with matplotlib from Prometheus data (via
History). Images are cached in memory per (chart, appliance, range, language).
After every fast poll a scheduler re-renders the default charts (configured
language) whose image is older than the chart's max_age, one at a time in a worker
thread. Requests are answered from the cache when fresh, otherwise rendered on
demand; if rendering fails, the last image is returned marked as stale.

Chart texts are localized (an LLM cannot translate text inside an image); the key
figures returned next to the image use canonical English keys.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import chart_style as cs
from .errors import HomeModbusError, NotFoundError, UnavailableError
from .history import History, HistoryError, period_boundaries
from .hub import Appliance, Hub
from .i18n import Translator, normalize

_LOGGER = logging.getLogger(__name__)

RENDER_TIMEOUT = 20.0  # Prometheus queries time out after 8 s and run concurrently
MAX_CACHED_IMAGES = 64  # least recently used images beyond this are dropped


class ChartError(HomeModbusError):
    """Bad chart arguments or nothing to draw."""


class UnknownChartError(ChartError, NotFoundError):
    """No chart of this name (or no appliance of the kind it needs)."""


@dataclass
class ChartImage:
    png: bytes
    summary: dict[str, Any]  # includes generated_at (ISO) and the data range start/end
    generated_at: float
    stale: bool = False
    stale_reason: str | None = None

    @property
    def age_s(self) -> float:
        return time.time() - self.generated_at


@dataclass(frozen=True)
class ChartSpec:
    name: str
    description: str
    kind: str  # appliance kind the chart is about: "inverter" or "heat_pump"
    per_appliance: bool  # one chart per appliance (else: all appliances of the kind)
    default_range: str
    min_range: str
    max_range: str
    max_age: float  # seconds until a cached image is re-rendered
    needs_forecast: bool = False  # only with a configured PV forecast


CATALOG: dict[str, ChartSpec] = {s.name: s for s in (
    ChartSpec("energy_flow", "PV, house, battery and grid power; state of charge below",
              "inverter", True, "24h", "1h", "7d", 300),
    ChartSpec("energy_daily", "Daily PV, house consumption, grid import and feed-in (kWh)",
              "inverter", True, "30d", "2d", "62d", 6 * 3600),
    ChartSpec("heatpump", "Flow/return, hot water and outdoor temperature; compressor demand band below",
              "heat_pump", True, "24h", "1h", "7d", 300),
    ChartSpec("heatpump_spf", "Performance factor (heat/electricity) per month and heat pump",
              "heat_pump", False, "365d", "31d", "1826d", 6 * 3600),
    ChartSpec("compressor_cycles", "Compressor runtime hours and starts per day and heat pump",
              "heat_pump", False, "7d", "2d", "31d", 1800),
    ChartSpec("pv_forecast", "Today and tomorrow: measured and forecast PV power, expected house "
              "load, surplus windows; battery state of charge (measured, then simulated) below",
              "inverter", True, "2d", "2d", "2d", 900, needs_forecast=True),
)}

_RANGE = re.compile(r"^(\d+)\s*([hdw])$")


def range_seconds(value: str) -> int:
    m = _RANGE.match(value.strip().lower())
    if not m:
        raise ChartError(f"Invalid range '{value}'; use e.g. 24h, 7d, 4w.")
    return int(m.group(1)) * {"h": 3600, "d": 86400, "w": 7 * 86400}[m.group(2)]


CacheKey = tuple[str, str, str, str]  # chart, appliance ("" = all of the kind), range, lang


@dataclass
class _Request:
    """Everything a chart builder needs."""

    spec: ChartSpec
    key: CacheKey
    apps: list[Appliance]  # exactly one for per-appliance charts
    t: Translator

    @property
    def app(self) -> Appliance:
        return self.apps[0]


Builder = Callable[["ChartService", _Request, datetime, datetime],
                   Awaitable[tuple[Callable[[], bytes], dict[str, Any]]]]


class ChartService:
    def __init__(self, hub: Hub, history: History, forecast=None):
        self.hub = hub
        self.history = history
        self.forecast = forecast  # ForecastService when a forecast is configured
        self.default_lang = hub.config.lang
        self._cache: OrderedDict[CacheKey, ChartImage] = OrderedDict()
        # Cached images with a "stale" badge: key -> (generated_at of the original, png)
        self._stale_png: dict[CacheKey, tuple[float, bytes]] = {}
        self._locks: dict[CacheKey, asyncio.Lock] = {}
        self.renders = 0

    @property
    def tz(self) -> ZoneInfo:
        return self.history.tz

    # ----------------------------------------------------------------- catalog
    def appliances_of(self, kind: str) -> list[Appliance]:
        return [a for a in self.hub.appliances.values() if a.profile.kind == kind]

    def _available(self, spec: ChartSpec) -> bool:
        return bool(self.appliances_of(spec.kind)) and (not spec.needs_forecast or self.forecast is not None)

    def catalog(self) -> list[dict[str, Any]]:
        out = []
        for spec in CATALOG.values():
            apps = self.appliances_of(spec.kind)
            if self._available(spec):
                out.append({
                    "chart": spec.name,
                    "description": spec.description,
                    "appliances": [a.name for a in apps] if spec.per_appliance
                    else f"all {spec.kind.replace('_', ' ')}s",
                    "default_range": spec.default_range,
                    "range_limits": [spec.min_range, spec.max_range],
                    "refresh_s": spec.max_age,
                })
        return out

    def _request(self, chart: str, appliance: str, range_: str, lang: str | None) -> _Request:
        spec = CATALOG.get(chart)
        if spec is None:
            raise UnknownChartError(f"Unknown chart '{chart}'. Charts: {', '.join(CATALOG)}")
        apps = self.appliances_of(spec.kind)
        if not apps:
            raise UnknownChartError(f"No {spec.kind.replace('_', ' ')} configured for '{chart}'.")
        if spec.needs_forecast and self.forecast is None:
            raise UnknownChartError(f"'{chart}' needs a forecast section in the configuration.")
        if appliance:
            app = self.hub.get(appliance)  # UnknownApplianceError -> 404
            if app.profile.kind != spec.kind:
                raise ChartError(f"'{chart}' needs a {spec.kind.replace('_', ' ')}, not {app.name}.")
            apps = [app]
        range_ = (range_ or spec.default_range).strip().lower()
        seconds = range_seconds(range_)
        if not range_seconds(spec.min_range) <= seconds <= range_seconds(spec.max_range):
            raise ChartError(f"range for '{chart}' must be between {spec.min_range} and {spec.max_range}")
        if spec.per_appliance:
            apps = apps[:1]  # default: the first appliance of the kind
        # Charts over all appliances of a kind are cached under "" unless one was chosen.
        key_app = apps[0].name if spec.per_appliance or appliance else ""
        t = Translator(normalize(lang, self.default_lang))
        return _Request(spec, (chart, key_app, range_, t.lang), apps, t)

    def default_requests(self) -> list[_Request]:
        """Charts kept fresh in the background: every catalog chart, default range and
        language, per appliance where the chart is per appliance."""
        requests = []
        for spec in CATALOG.values():
            if not self._available(spec):
                continue
            apps = self.appliances_of(spec.kind)
            names = [a.name for a in apps] if spec.per_appliance else [""]
            requests += [self._request(spec.name, name, "", None) for name in names]
        return requests

    # ----------------------------------------------------------------- cache
    def _cached(self, key: CacheKey, max_age: float) -> ChartImage | None:
        image = self._cache.get(key)
        if image is not None and image.age_s < max_age:
            self._cache.move_to_end(key)
            return image
        return None

    async def get(self, chart: str, appliance: str = "", range_: str = "",
                  lang: str | None = None) -> ChartImage:
        """The chart from the cache when fresh, else rendered now. If it cannot be
        rendered (Prometheus down, no data, error) the last image is returned with
        stale=True and a visible badge; without one, the error is raised."""
        req = self._request(chart, appliance, range_, lang)
        if cached := self._cached(req.key, req.spec.max_age):
            return cached
        previous = self._cache.get(req.key)
        prometheus = self.history.prometheus
        if previous is not None and prometheus.open():  # known outage: don't even try
            return await self._stale(req, previous, prometheus.unavailable_error())
        try:
            return await self._render(req)
        except ChartError:
            raise
        except Exception as err:  # Prometheus down, no data, timeout, drawing failed
            if previous is not None:
                _LOGGER.info("Chart %s: re-render failed (%s), serving cached image", chart, _text(err))
                return await self._stale(req, previous, err)
            if isinstance(err, HomeModbusError):
                raise
            raise ChartError(f"Rendering '{chart}' failed: {_text(err)}") from err

    async def _stale(self, req: _Request, image: ChartImage, reason: Exception) -> ChartImage:
        """The cached image marked as outdated (badge drawn into the picture once)."""
        marked = self._stale_png.get(req.key)
        if marked is None or marked[0] != image.generated_at:
            stamp = datetime.fromtimestamp(image.generated_at, self.tz).strftime(
                cs.date_format(req.t, "stamp"))
            png = await asyncio.to_thread(cs.locked_draw, lambda: cs.stale_badge(
                image.png, req.t("stale", time=stamp)))
            marked = self._stale_png[req.key] = (image.generated_at, png)
        details = reason.to_dict() if isinstance(reason, HomeModbusError) else {"error": _text(reason)}
        details.pop("hint", None)
        return ChartImage(marked[1], {**image.summary, "stale": True, "stale_reason": details},
                          image.generated_at, stale=True, stale_reason=details["error"])

    async def _render(self, req: _Request) -> ChartImage:
        lock = self._locks.setdefault(req.key, asyncio.Lock())
        async with lock:
            if cached := self._cached(req.key, req.spec.max_age):  # rendered while we waited
                return cached
            end = self.history.now()
            start = end - timedelta(seconds=range_seconds(req.key[2]))
            try:
                draw, summary = await asyncio.wait_for(
                    BUILDERS[req.spec.name](self, req, start, end), RENDER_TIMEOUT)
            except TimeoutError as err:
                raise ChartError(f"Rendering '{req.spec.name}' timed out after {RENDER_TIMEOUT:.0f} s") from err
            generated = time.time()
            image = ChartImage(await asyncio.to_thread(cs.locked_draw, draw), {
                "chart": req.spec.name,
                "generated_at": datetime.fromtimestamp(generated, self.tz).isoformat(timespec="seconds"),
                "appliance": req.key[1] or [a.name for a in req.apps],
                "range": req.key[2],
                "lang": req.t.lang,
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                **summary,
            }, generated)
            self._cache[req.key] = image
            self._stale_png.pop(req.key, None)
            self._cache.move_to_end(req.key)
            while len(self._cache) > MAX_CACHED_IMAGES:
                dropped, _ = self._cache.popitem(last=False)
                self._locks.pop(dropped, None)
                self._stale_png.pop(dropped, None)
            self.renders += 1
            return image

    async def refresh_defaults(self) -> int:
        """Re-render outdated default charts; returns the number rendered.

        While Prometheus is down nothing is attempted: probe() checks (with back-off)
        whether it is back, so an outage costs one cheap request per back-off step
        instead of dozens of timed-out queries per poll.
        """
        prometheus = self.history.prometheus
        if prometheus.available is False and not await prometheus.probe():
            return 0
        count = 0
        for req in self.default_requests():
            if self._cached(req.key, req.spec.max_age):
                continue
            try:
                await self._render(req)
                count += 1
            except UnavailableError:
                break  # logged once by the Prometheus client; retry after back-off
            except Exception as err:  # e.g. no data yet; keep refreshing the others
                _LOGGER.info("Chart %s/%s not rendered: %s", req.spec.name, req.key[1] or "all", _text(err))
        return count

    async def run(self) -> None:
        """Wait for fast polls and keep the default charts fresh."""
        while True:
            await self.hub.polled.wait()
            self.hub.polled.clear()
            started = time.monotonic()
            try:
                count = await self.refresh_defaults()
            except Exception:  # never let the scheduler die
                _LOGGER.exception("Chart refresh failed")
                continue
            if count:
                _LOGGER.info("Rendered %d chart(s) in %.1f s", count, time.monotonic() - started)

    def status(self) -> list[dict[str, Any]]:
        return [{"chart": k[0], "appliance": k[1] or "all", "range": k[2], "lang": k[3],
                 "generated_at": v.summary["generated_at"], "age_s": round(v.age_s, 1),
                 "outdated": v.age_s >= CATALOG[k[0]].max_age, "bytes": len(v.png)}
                for k, v in self._cache.items()]

    def color(self, app: Appliance) -> str:
        """Fixed color per appliance within its kind (configuration order), as in Grafana."""
        same_kind = self.appliances_of(app.profile.kind)
        return cs.APPLIANCE_COLORS[same_kind.index(app) % len(cs.APPLIANCE_COLORS)]


# --------------------------------------------------------------------------- builders
# Each builder fetches its data (async) and returns a draw() callable for the worker
# thread plus the key figures that accompany the image.

def _require_points(data: dict, keys: list[str]) -> None:
    if all(not data.get(k, {}).get("points") for k in keys):
        raise HistoryError("no data in this time range")


async def _energy_flow(svc: ChartService, req: _Request, start: datetime, end: datetime):
    t, app = req.t, req.app
    keys = ["pv_power", "load_power", "battery_power", "grid_power", "battery_soc"]
    data = (await svc.history.history(app, keys, start.isoformat(), end.isoformat(), 300))["series"]
    _require_points(data, keys)
    lines = [("pv_power", t("pv"), cs.YELLOW), ("load_power", t("house"), cs.BLUE),
             ("battery_power", t("battery"), cs.AQUA), ("grid_power", t("grid"), cs.ORANGE)]
    summary = {"values": {k: cs.stats(data[k]) for k in keys},
               "note": "battery + = discharging, grid + = import"}

    def draw() -> bytes:
        fig, (ax, ax2) = cs.figure(2, heights=(3, 1))
        for key, label, color in lines:
            xs, ys = cs.xy(data.get(key), svc.tz, scale=0.001)
            ax.plot(xs, ys, color=color, lw=2, label=label)
        ax.axhline(0, color=cs.INK_2, lw=0.8)
        ax.set_ylabel("kW")
        xs, ys = cs.xy(data.get("battery_soc"), svc.tz)
        ax2.plot(xs, ys, color=cs.AQUA, lw=2)
        ax2.set_ylim(0, 100)
        ax2.set_yticks([0, 50, 100])
        ax2.set_ylabel(f"{t('soc_short')} %")
        cs.legend(ax)
        cs.time_axis(ax2, start, end, svc.tz, t)
        cs.header(fig, t("chart.energy_flow"),
                  f"{app.name} · {cs.span_text(start, end, t)} · {t('power_note')}", t, end)
        return cs.png(fig)

    return draw, summary


async def _energy_daily(svc: ChartService, req: _Request, start: datetime, end: datetime):
    t, app = req.t, req.app
    result = (await svc.history.energy([app], "day", start.isoformat(), end.isoformat()))
    periods = result["appliances"][app.name]["periods"]
    total = result["appliances"][app.name]["total"]
    if not periods:
        raise HistoryError("no data in this time range")
    series = [("total_pv_energy", t("pv"), cs.YELLOW), ("house_consumption", t("house"), cs.BLUE),
              ("total_import_energy", t("import"), cs.ORANGE),
              ("total_export_energy", t("export"), cs.VIOLET)]

    def value(entry: dict, key: str) -> float | None:
        source = entry.get("derived", {}) if key == "house_consumption" else entry["kWh"]
        return source.get(key)

    summary = {
        "unit": "kWh",
        "days": [{"day": p["start"][:10], **{k: value(p, k) for k, _, _ in series},
                  "self_sufficiency": p.get("derived", {}).get("self_sufficiency"),
                  **({"partial": True} if p.get("partial") else {})} for p in periods],
        "total": {**{k: value(total, k) for k, _, _ in series},
                  "self_sufficiency": total.get("derived", {}).get("self_sufficiency")},
    }

    def draw() -> bytes:
        fig, (ax,) = cs.figure(1)
        days = [datetime.fromisoformat(p["start"]) for p in periods]
        offsets, width = cs.bar_offsets(len(series))
        partial = [bool(p.get("partial")) for p in periods]
        for (key, label, color), off in zip(series, offsets):
            bars = ax.bar([d + timedelta(days=off) for d in days],
                          [value(p, key) or 0 for p in periods],
                          width=width * 0.9, color=color, label=label, zorder=2)
            cs.hatch_partial(bars, partial)
        ax.set_ylabel("kWh")
        cs.legend(ax, cs.patches([(label, color) for _, label, color in series]))
        cs.day_axis(ax, days, svc.tz, t)
        note = f"{app.name} · {t('days', n=len(periods))}"
        if any(partial):
            note += f" · {t('partial')}"
        cs.header(fig, t("chart.energy_daily"), note, t, end)
        return cs.png(fig)

    return draw, summary


async def _heatpump(svc: ChartService, req: _Request, start: datetime, end: datetime):
    t, app = req.t, req.app
    temps = [("flow_temperature", t("flow"), cs.ORANGE, "-"),
             ("return_temperature", t("return"), cs.ORANGE, "--"),
             ("dhw_temperature", t("dhw"), cs.BLUE, "-"),
             ("outdoor_temperature", t("outdoor"), cs.AQUA, "-")]
    keys = [k for k, *_ in temps] + ["compressor_demand"]
    missing = [k for k in keys if k not in svc.history.recorded_keys(app)]
    if missing:
        raise ChartError(f"{app.name} does not record {', '.join(missing)}")
    data = (await svc.history.history(app, keys, start.isoformat(), end.isoformat(), 300))["series"]
    _require_points(data, keys)
    demand = data["compressor_demand"].get("points", [])
    counts: dict[str, int] = {}
    for _, state in demand:
        counts[state] = counts.get(state, 0) + 1
    summary = {"values": {k: cs.stats(data[k]) for k, *_ in temps},
               "compressor_demand_share": {s: round(n / len(demand), 3) for s, n in counts.items()}}

    def draw() -> bytes:
        fig, (ax, ax2) = cs.figure(2, heights=(4, 1))
        for key, label, color, style in temps:
            xs, ys = cs.xy(data.get(key), svc.tz)
            ax.plot(xs, ys, color=color, lw=2, ls=style, label=label)
        ax.set_ylabel("°C")
        states = cs.state_band(ax2, demand, end)
        cs.legend(ax, ax.get_legend_handles_labels()[0] + cs.patches(
            [(t.state(s), cs.DEMAND_COLORS.get(s, cs.GRAY)) for s in states]))
        cs.time_axis(ax2, start, end, svc.tz, t)
        cs.header(fig, t("chart.heatpump", name=app.name),
                  f"{cs.span_text(start, end, t)} · {t('demand')}", t, end)
        return cs.png(fig)

    return draw, summary


async def _heatpump_spf(svc: ChartService, req: _Request, start: datetime, end: datetime):
    t, apps = req.t, req.apps
    result = await svc.history.energy(apps, "month", start.isoformat(), end.isoformat())
    per_app = {a.name: result["appliances"][a.name] for a in apps}
    months = sorted({p["start"][:7] for r in per_app.values() for p in r["periods"]})
    if not months:
        raise HistoryError("no data in this time range")
    summary = {
        "months": {a: {p["start"][:7]: {"spf": p.get("derived", {}).get("spf"),
                                        "heat_kWh": p["kWh"].get("heat_delivered_total"),
                                        "electricity_kWh": p["kWh"].get("electricity_total"),
                                        **({"partial": True} if p.get("partial") else {})}
                       for p in r["periods"]} for a, r in per_app.items()},
        "total_spf": {a: r["total"].get("derived", {}).get("spf") for a, r in per_app.items()},
    }

    def draw() -> bytes:
        fig, (ax,) = cs.figure(1)
        offsets, width = cs.bar_offsets(len(apps))
        for a, off in zip(apps, offsets):
            by_month = {p["start"][:7]: p for p in per_app[a.name]["periods"]}
            xs, ys, partial = [], [], []
            for j, month in enumerate(months):
                p = by_month.get(month)
                spf = p.get("derived", {}).get("spf") if p else None
                if spf:
                    xs.append(j + off)
                    ys.append(spf)
                    partial.append(bool(p.get("partial")))
            bars = ax.bar(xs, ys, width=width * 0.9, color=svc.color(a), zorder=2)
            cs.hatch_partial(bars, partial)
            for x, y in zip(xs, ys):
                ax.annotate(f"{y:.1f}", (x, y), ha="center", va="bottom", fontsize=8,
                            color=cs.INK_2, xytext=(0, 2), textcoords="offset points")
        ax.set_xticks(range(len(months)), [cs.month_label(m, t) for m in months])
        ax.set_ylim(bottom=0)
        cs.legend(ax, cs.patches([(a.name, svc.color(a)) for a in apps]))
        cs.header(fig, t("chart.heatpump_spf"), t("partial"), t, end)
        return cs.png(fig)

    return draw, summary


async def _compressor_cycles(svc: ChartService, req: _Request, start: datetime, end: datetime):
    t, apps = req.t, req.apps
    bounds = period_boundaries(start, end, "day", svc.tz)
    days = list(zip(bounds, bounds[1:]))

    async def one_day(app: Appliance, s: datetime, e: datetime) -> dict | None:
        try:
            r = await svc.history.runtime(app, "compressor", s.isoformat(), e.isoformat())
        except HistoryError:
            return None  # no data that day (an unreachable Prometheus is not a HistoryError)
        return {"hours_on": r["states"].get("on", {}).get("hours", 0.0), "starts": r["starts"]}

    results = {a.name: await asyncio.gather(*(one_day(a, s, e) for s, e in days)) for a in apps}
    if all(r is None for rs in results.values() for r in rs):
        raise HistoryError("no data in this time range")
    summary = {"days": {a: {s.date().isoformat(): r for (s, _), r in zip(days, rs) if r}
                        for a, rs in results.items()}}

    def draw() -> bytes:
        fig, (ax, ax2) = cs.figure(2, heights=(1, 1))
        starts = [s for s, _ in days]
        offsets, width = cs.bar_offsets(len(apps))
        for a, off in zip(apps, offsets):
            xs = [d + timedelta(days=off) for d in starts]
            rs = results[a.name]
            ax.bar(xs, [r["hours_on"] if r else 0 for r in rs], width=width * 0.9,
                   color=svc.color(a), zorder=2)
            ax2.bar(xs, [r["starts"] if r else 0 for r in rs], width=width * 0.9,
                    color=svc.color(a), zorder=2)
        ax.set_ylabel(t("hours"))
        ax2.set_ylabel(t("starts"))
        ax2.yaxis.get_major_locator().set_params(integer=True)
        cs.legend(ax, cs.patches([(a.name, svc.color(a)) for a in apps]))
        cs.day_axis(ax2, starts, svc.tz, t)
        cs.header(fig, t("chart.compressor_cycles"), cs.span_text(start, end, t), t, end)
        return cs.png(fig)

    return draw, summary


async def _pv_forecast(svc: ChartService, req: _Request, start: datetime, end: datetime):
    """Today and tomorrow: measured PV so far, forecast PV, expected load, surplus windows,
    battery state of charge (measured, then simulated). The requested range is fixed."""
    t, app, forecast = req.t, req.app, svc.forecast
    pv = await forecast.pv_forecast("", "15m")
    surplus = await forecast.surplus(resolution="15m")
    now = datetime.fromtimestamp(forecast.now(), svc.tz)
    day0 = datetime.combine(now.date(), datetime.min.time(), svc.tz)
    day2 = day0 + timedelta(days=2)
    measured: dict[str, list] = {}
    try:  # measured values of today; the forecast alone still makes a chart
        history = await svc.history.history(app, ["pv_power", "battery_soc"], day0.isoformat(),
                                            now.isoformat(), 300)
        measured = {k: v.get("points", []) for k, v in history["series"].items()}
    except HomeModbusError as err:
        _LOGGER.info("pv_forecast chart without measured values: %s", _text(err))

    def parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts).astimezone(svc.tz)

    pv_points = [(parse(i["start"]) + timedelta(minutes=7.5), i["pv_w"] / 1000) for i in pv["intervals"]]
    course = [(parse(i["start"]) + timedelta(minutes=7.5), i) for i in surplus["intervals"]]
    measured_pv = [(parse(ts), v / 1000) for ts, v in measured.get("pv_power", []) if v is not None]
    so_far = sum(v for _, v in measured.get("pv_power", []) if v is not None) * 300 / 3.6e6  # W·5 min -> kWh
    days = list(pv["days"].values())
    windows = surplus["windows"]
    summary = {"days": pv["days"], "measured_today_kwh": round(so_far, 1),
               "battery": surplus["battery"], "windows": windows,
               "performance_ratio": {a["name"]: a["performance_ratio"] for a in pv["arrays"]},
               "issued_at": pv["issued_at"], **({"forecast_stale": True} if pv["stale"] else {})}

    def draw() -> bytes:
        import matplotlib.dates as mdates

        fig, (ax, ax2) = cs.figure(2, heights=(3, 1))
        for w in windows:
            ax.axvspan(parse(w["start"]), parse(w["end"]), color=cs.AQUA, alpha=0.15, lw=0, zorder=1)
            ax.annotate(f"+{w['export_kwh']:g} kWh", (parse(w["start"]), 1.0), xycoords=("data", "axes fraction"),
                        xytext=(3, -12), textcoords="offset points", fontsize=8, color=cs.INK_2)
        if measured_pv:
            ax.plot(*zip(*measured_pv), color=cs.YELLOW, lw=2, label=t("measured"), zorder=3)
        ax.plot(*zip(*pv_points), color=cs.YELLOW, lw=2, ls="--", label=t("pv_forecast"), zorder=2)
        if course:
            ax.plot([c[0] for c in course], [c[1]["load_w"] / 1000 for c in course], color=cs.BLUE, lw=1.5,
                    ls="--", label=t("load_expected"), zorder=2)
        ax.axvline(now, color=cs.INK_2, lw=1, ls=":", zorder=4)
        ax.annotate(t("now"), (now, 1.0), xycoords=("data", "axes fraction"), xytext=(3, -24),
                    textcoords="offset points", fontsize=8, color=cs.INK_2)
        ax.set_ylabel("kW")
        ax.set_ylim(bottom=0)
        handles = ax.get_legend_handles_labels()[0] + cs.patches([(t("surplus"), cs.AQUA)])
        cs.legend(ax, handles)

        soc_measured = [(parse(ts), v) for ts, v in measured.get("battery_soc", []) if v is not None]
        if soc_measured:
            ax2.plot(*zip(*soc_measured), color=cs.AQUA, lw=2)
        soc_forecast = [(c[0], c[1]["soc"]) for c in course if c[1]["soc"] is not None]
        if soc_forecast:
            ax2.plot(*zip(*soc_forecast), color=cs.AQUA, lw=2, ls="--")
        ax2.axvline(now, color=cs.INK_2, lw=1, ls=":")
        ax2.set_ylim(0, 100)
        ax2.set_yticks([0, 50, 100])
        ax2.set_ylabel(f"{t('soc_short')} %")
        ax2.set_xlim(day0, day2)
        ax2.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=svc.tz))
        ax2.xaxis.set_major_formatter(mdates.DateFormatter(cs.date_format(t, "span"), tz=svc.tz))

        parts = [t("forecast_today", kwh=f"{days[0]['pv_kwh']:g}", so_far=f"{so_far:.1f}"),
                 t("forecast_tomorrow", kwh=f"{days[1]['pv_kwh']:g}") if len(days) > 1 else None,
                 t("surplus_from", time=_hhmm(windows[0]["start"], t)) if windows else t("no_surplus")]
        cs.header(fig, t("chart.pv_forecast"), " · ".join(p for p in parts if p), t, now)
        return cs.png(fig)

    return draw, summary


def _hhmm(iso: str, t: Translator) -> str:
    """'2026-09-30T13:15+02:00' -> '13:15' today, 'Sep 30 13:15' / '30.09. 13:15' otherwise."""
    return datetime.fromisoformat(iso).strftime(cs.date_format(t, "span"))


def _text(err: BaseException) -> str:
    return str(err) or type(err).__name__


BUILDERS: dict[str, Builder] = {
    "energy_flow": _energy_flow,
    "energy_daily": _energy_daily,
    "heatpump": _heatpump,
    "heatpump_spf": _heatpump_spf,
    "compressor_cycles": _compressor_cycles,
    "pv_forecast": _pv_forecast,
}
assert BUILDERS.keys() == CATALOG.keys()
