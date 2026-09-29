"""Historical values from Prometheus, mapped back to appliances and register keys.

Three views, each designed to give compact answers to an LLM:
    history  - downsampled time series plus min/max/avg per data point
    energy   - energy balance per local calendar day/week/month/year
    runtime  - time per state, starts and run lengths of enum/on-off data points
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .errors import HomeModbusError, NotFoundError
from .hub import Appliance, Hub
from .metrics import instrument_plan
from .prometheus import PrometheusClient, PrometheusUnavailableError  # noqa: F401 (re-export)
from .registry import Register

MAX_POINTS = 500
MAX_PERIODS = 62
MAX_RUNTIME_DAYS = 31
# Gaps longer than this between samples count as "no data" (service/Prometheus down).
MAX_SAMPLE_GAP = 120.0
# How far back to look for a counter reading at a period boundary.
COUNTER_LOOKBACK = "7d"
# The "last" value of a series is read from this window at the end of the range, so a
# series that ended earlier (see below) cannot win over the current one.
LAST_VALUE_WINDOW_S = 120

# Energy from integrated power (profile power_integration, device energy_from_power):
# power is sampled every POWER_STEP_S; a period with less than POWER_MIN_COVERAGE of
# its time covered by samples is marked partial (gaps count as zero, not extrapolated).
POWER_STEP_S = 60
POWER_MIN_COVERAGE = 0.95

# One appliance data point can be stored as several Prometheus series: whenever a
# label besides `appliance` changes (host name, version, ...) a new series starts and
# the old one stops. Every query therefore combines all series of an appliance; this
# maps each *_over_time function to the aggregation that keeps its meaning.
COMBINE = {"min_over_time": "min", "max_over_time": "max", "avg_over_time": "avg",
           "last_over_time": "max", "count_over_time": "sum"}


def combined(fn: str, selector: str, window: str) -> str:
    """fn over the window, combined over all series of each appliance."""
    return f"{COMBINE[fn]} by (appliance) ({fn}({selector}[{window}]))"

# Derived energy figures per profile: name -> (numerator keys, denominator keys, kind)
# kind "ratio" = sum(num) / sum(den); "sum" = sum(num); "one_minus" = 1 - num/den.
DERIVED: dict[str, dict[str, tuple[tuple[str, ...], tuple[str, ...], str]]] = {
    "sungrow_sh": {
        "house_consumption": (
            ("total_direct_consumption", "total_battery_discharge", "total_import_energy"), (), "sum"),
        "self_sufficiency": (
            ("total_import_energy",),
            ("total_direct_consumption", "total_battery_discharge", "total_import_energy"),
            "one_minus"),
        "self_consumption_rate": (("total_export_energy",), ("total_pv_energy",), "one_minus"),
    },
    "neo": {
        "spf": (("heat_delivered_total",), ("electricity_total",), "ratio"),
        "spf_heating": (("heat_delivered_heating",), ("electricity_heating",), "ratio"),
        "spf_dhw": (("heat_delivered_dhw",), ("electricity_dhw",), "ratio"),
    },
    "iwr": {
        "spf": (("total_thermal_delivered",), ("total_energy_consumed",), "ratio"),
    },
}
DERIVED_HELP = {
    "house_consumption": "kWh used by the house (PV direct + battery discharge + grid import)",
    "self_sufficiency": "share of house consumption not covered by the grid (Autarkie), 0-1",
    "self_consumption_rate": "share of PV energy used on site instead of exported, 0-1",
    "spf": "seasonal performance factor (Arbeitszahl): heat delivered / electricity used",
    "spf_heating": "performance factor for space heating",
    "spf_dhw": "performance factor for domestic hot water",
}


class HistoryError(HomeModbusError):
    """Invalid history request (bad time, range too long, no data, ...)."""


class UnknownRegisterError(HistoryError, NotFoundError):
    """The register does not exist or is not recorded."""


def _promql_string(value: str) -> str:
    """Escape a value for a PromQL double-quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _promql_regex_alternatives(values: list[str]) -> str:
    """values as a PromQL regex alternation matching them literally."""
    special = set("\\.^$|?*+()[]{}")
    parts = ["".join("\\" + c if c in special else c for c in v) for v in values]
    return _promql_string("|".join(parts))


@dataclass(frozen=True)
class Series:
    appliance: Appliance
    reg: Register
    metric: str  # Prometheus metric name

    @property
    def selector(self) -> str:
        return f'{self.metric}{{appliance="{_promql_string(self.appliance.name)}"}}'


# --------------------------------------------------------------------------- time
_REL = re.compile(r"^(?:now-)?(\d+(?:\.\d+)?)\s*([mhdwy])$")
_REL_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "y": 365 * 86400}


def parse_time(value: str, tz: ZoneInfo, now: datetime, default: datetime) -> datetime:
    """'' -> default; 'now'; relative '24h', '7d', 'now-30m'; 'today', 'yesterday';
    ISO date or datetime (local time zone unless an offset is given)."""
    value = (value or "").strip().lower()
    if not value:
        return default
    if value == "now":
        return now
    if value in ("today", "yesterday"):
        day = now.date() - timedelta(days=value == "yesterday")
        return datetime.combine(day, datetime.min.time(), tz)
    if m := _REL.match(value):
        return now - timedelta(seconds=float(m.group(1)) * _REL_SECONDS[m.group(2)])
    try:
        dt = datetime.fromisoformat(value.upper() if "t" in value else value)
    except ValueError as err:
        raise HistoryError(
            f"Cannot parse time '{value}'. Use ISO (2026-09-01, 2026-09-01T06:00), "
            "relative (24h, 7d, 30m) or today/yesterday/now."
        ) from err
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def _period_start(day: date, period: str) -> date:
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    if period == "year":
        return day.replace(month=1, day=1)
    return day


def _next_period(day: date, period: str) -> date:
    if period == "week":
        return day + timedelta(days=7)
    if period == "month":
        return (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    if period == "year":
        return day.replace(year=day.year + 1, month=1, day=1)
    return day + timedelta(days=1)


def period_boundaries(start: datetime, end: datetime, period: str, tz: ZoneInfo) -> list[datetime]:
    """Local-time calendar boundaries covering [start, end]; the last one is capped at end."""
    day = _period_start(start.astimezone(tz).date(), period)
    bounds = []
    while True:
        dt = datetime.combine(day, datetime.min.time(), tz)
        if dt >= end:
            break
        bounds.append(dt)
        day = _next_period(day, period)
    bounds.append(end)
    return bounds


def _iso(ts: float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")


def _step_for(seconds: float, max_points: int) -> int:
    """A round step (>= 15 s, the export interval) giving at most max_points points."""
    raw = max(15.0, seconds / max_points)
    for step in (15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400):
        if step >= raw:
            return step
    return int(math.ceil(raw / 86400) * 86400)


def _round(value: float | None, ndigits: int = 3) -> float | None:
    if value is None or math.isnan(value):
        return None
    return round(value, ndigits)


# --------------------------------------------------------------------------- history
class History:
    def __init__(self, hub: Hub, prometheus_url: str, timezone: str = "Europe/Berlin",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.hub = hub
        self.prometheus = PrometheusClient(prometheus_url, transport)
        self.tz = ZoneInfo(timezone)
        self._series: dict[tuple[str, str], Series] = {}
        for inst in instrument_plan(hub):
            for s in inst.series:
                self._series[(s.appliance.name, s.reg.key)] = Series(
                    s.appliance, s.reg, inst.prometheus_name)

    # ----------------------------------------------------------------- prometheus
    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        return await self.prometheus.get(path, params)

    async def close(self) -> None:
        await self.prometheus.close()

    async def _instant(self, query: str, at: datetime) -> list[dict]:
        return (await self._get("query", {"query": query, "time": at.timestamp()}))["result"]

    async def _range(self, query: str, start: datetime, end: datetime, step: int) -> list[dict]:
        data = await self._get("query_range", {
            "query": query, "start": start.timestamp(), "end": end.timestamp(), "step": step})
        return data["result"]

    # ----------------------------------------------------------------- helpers
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def span(self, start: str, end: str, default_start: str) -> tuple[datetime, datetime]:
        now = self.now()
        t_end = min(parse_time(end, self.tz, now, now), now)
        t_start = parse_time(start, self.tz, now, parse_time(default_start, self.tz, now, now))
        if t_start >= t_end:
            raise HistoryError("start must be before end")
        return t_start, t_end

    def series(self, app: Appliance, key: str) -> Series:
        if key not in app.profile.registers:
            raise UnknownRegisterError(f"Unknown register '{key}' for {app.name}; see list_registers.")
        s = self._series.get((app.name, key))
        if s is None:
            raise UnknownRegisterError(
                f"'{key}' is not recorded for {app.name} (only polled numeric values are; "
                "see list_registers, poll_group). Add it to extra_keys to record it."
            )
        return s

    def recorded_keys(self, app: Appliance) -> list[str]:
        return [k for (a, k) in self._series if a == app.name]

    def _enum_text(self, reg: Register, value: float) -> str | float:
        if not reg.enum:
            return value
        raw = int(value)
        return reg.enum.get(str(raw), reg.enum.get("*", str(raw)))

    # ----------------------------------------------------------------- history
    async def history(self, app: Appliance, keys: list[str], start: str = "", end: str = "",
                      max_points: int = 100, lang: str = "en") -> dict[str, Any]:
        if not keys:
            raise HistoryError("Provide at least one register key.")
        max_points = max(1, min(int(max_points or 100), MAX_POINTS))
        series = [self.series(app, k) for k in keys]
        t_start, t_end = self.span(start, end, "24h")
        seconds = (t_end - t_start).total_seconds()
        step = _step_for(seconds, max_points)
        window = f"{int(seconds)}s"

        async def one(s: Series) -> tuple[str, dict[str, Any]]:
            reg = s.reg
            is_state = reg.enum is not None or reg.data_type == "bool"
            # Points: average per step for measurements, last value for states/counters.
            fn = "last_over_time" if is_state or reg.is_counter else "avg_over_time"
            points_q = self._range(combined(fn, s.selector, f"{step}s"), t_start, t_end, step)
            last_window = f"{int(min(seconds, max(LAST_VALUE_WINDOW_S, step)))}s"
            stat_qs = [self._instant(combined(f, s.selector, w), t_end)
                       for f, w in (("min_over_time", window), ("max_over_time", window),
                                    ("avg_over_time", window), ("last_over_time", last_window),
                                    ("count_over_time", window))]
            points_r, *stats_r = await asyncio.gather(points_q, *stat_qs)
            stat = [float(r[0]["value"][1]) if r else None for r in stats_r]
            out: dict[str, Any] = {"label": reg.display_label(lang)}
            if reg.unit:
                out["unit"] = reg.unit
            if not stat[4]:
                out["error"] = "no data in this time range"
                return reg.key, out
            values = points_r[0]["values"] if points_r else []
            if stat[3] is None and values:  # nothing in the last minutes: last point
                stat[3] = float(values[-1][1])
            if is_state:
                out["last"] = self._enum_text(reg, stat[3])
                out["points"] = [[_iso(t, self.tz), self._enum_text(reg, float(v))] for t, v in values]
                if reg.enum:
                    out["values"] = reg.enum
            else:
                out.update({"min": _round(stat[0]), "max": _round(stat[1]), "avg": _round(stat[2]),
                            "last": _round(stat[3])})
                if reg.is_counter:
                    out["increase"] = _round(stat[3] - stat[0])
                out["points"] = [[_iso(t, self.tz), _round(float(v))] for t, v in values]
            return reg.key, out

        results = dict(await asyncio.gather(*(one(s) for s in series)))
        return {
            "appliance": app.name,
            "start": t_start.isoformat(timespec="seconds"),
            "end": t_end.isoformat(timespec="seconds"),
            "step_s": step,
            "note": "points are [local time, value]; measurements are averaged per step, "
                    "states and counters show the last value per step",
            "series": results,
        }

    # ----------------------------------------------------------------- energy
    async def energy(self, apps: list[Appliance], period: str = "day", start: str = "",
                     end: str = "") -> dict[str, Any]:
        if period not in ("day", "week", "month", "year"):
            raise HistoryError("period must be day, week, month or year")
        default = {"day": "7d", "week": "8w", "month": "365d", "year": "1826d"}[period]
        t_start, t_end = self.span(start, end, default)
        bounds = period_boundaries(t_start, t_end, period, self.tz)
        if len(bounds) - 1 > MAX_PERIODS:
            raise HistoryError(
                f"{len(bounds) - 1} {period}s requested, at most {MAX_PERIODS}; "
                "use a coarser period or a shorter range.")

        counters = {
            app.name: [self._series[(app.name, r.key)] for regs in app.groups.values()
                       for r in regs if r.is_counter and (app.name, r.key) in self._series]
            for app in apps
        }
        all_series = [s for ss in counters.values() for s in ss]
        if not all_series:
            raise HistoryError("None of the selected appliances records energy counters.")
        names = "|".join(sorted({s.metric for s in all_series}))  # metric names are [a-zA-Z0-9_]
        apps_re = _promql_regex_alternatives(sorted(counters))
        sel = f'{{__name__=~"{names}",appliance=~"{apps_re}"}}'

        def index(result: list[dict]) -> dict[tuple[str, str], float]:
            return {(r["metric"]["appliance"], r["metric"]["__name__"]): float(r["value"][1])
                    for r in result}

        # Counter reading at every boundary (last sample before it), one query per
        # boundary for every counter. Counters only grow, so over several series of
        # the same counter the highest reading is the current one.
        at_bounds = await asyncio.gather(*(
            self._instant(f"max by (__name__, appliance) (last_over_time({sel}[{COUNTER_LOOKBACK}]))", b)
            for b in bounds))
        readings = [index(r) for r in at_bounds]
        # Periods starting before recording began: use the first sample inside the
        # period (min of a monotonic counter). min_over_time drops the metric name,
        # so this is queried per metric, and only where needed.
        firsts: list[dict[tuple[str, str], float]] = [{} for _ in bounds[1:]]
        wanted = {
            (i, s.metric)
            for i in range(len(bounds) - 1)
            for s in all_series
            if (s.appliance.name, s.metric) not in readings[i]
            and (s.appliance.name, s.metric) in readings[i + 1]
        }

        async def first(i: int, metric: str) -> None:
            dur = max(1, int((bounds[i + 1] - bounds[i]).total_seconds()))
            result = await self._instant(
                combined("min_over_time", f'{metric}{{appliance=~"{apps_re}"}}', f"{dur}s"), bounds[i + 1])
            for r in result:
                firsts[i][(r["metric"]["appliance"], metric)] = float(r["value"][1])

        await asyncio.gather(*(first(i, m) for i, m in wanted))

        integrated = {app.name: await self._integrate(app, bounds) for app in apps
                      if self._uses_power(app)}

        out: dict[str, Any] = {}
        for app in apps:
            periods, totals, empty = [], {}, 0
            for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
                natural_end = datetime.combine(
                    _next_period(_period_start(a.date(), period), period), datetime.min.time(), self.tz)
                values, partial = {}, b < natural_end  # period still running
                for s in counters[app.name]:
                    k = (app.name, s.metric)
                    end_v = readings[i + 1].get(k)
                    start_v = readings[i].get(k)
                    if start_v is None or (end_v is not None and start_v > end_v):
                        start_v = firsts[i].get(k)
                        if start_v is not None:
                            partial = True
                    if start_v is None or end_v is None or end_v < start_v:
                        values[s.reg.key] = None
                        continue
                    values[s.reg.key] = _round(end_v - start_v, 2)
                if app.name in integrated:
                    kwh, coverage = integrated[app.name][i]
                    values.update(kwh)
                    if coverage < POWER_MIN_COVERAGE:
                        partial = True
                for key, value in values.items():
                    if value is not None:
                        totals[key] = _round(totals.get(key, 0) + value, 2)
                entry = {"start": a.isoformat(timespec="seconds"), "end": b.isoformat(timespec="seconds"),
                         "kWh": values, **self._derived(app, values)}
                if all(v is None for v in values.values()):
                    empty += 1  # e.g. before recording started
                    continue
                if partial:
                    entry["partial"] = True
                periods.append(entry)
            out[app.name] = {
                "type": app.profile.kind.replace("_", " "),
                "energy_source": "integrated power" if app.name in integrated else "counters",
                "periods": periods,
                "total": {"kWh": totals, **self._derived(app, totals)},
            }
            if empty:
                out[app.name]["periods_without_data"] = empty
        return {
            "period": period,
            "timezone": str(self.tz),
            "note": "kWh per period from the appliances' lifetime counters or, where "
                    "energy_source is 'integrated power', from the recorded power (for "
                    "devices whose counters are not updated). 'partial': the period is still "
                    "running, recording started within it, or power samples are missing. "
                    "Heat pump counters count whole kWh, so short periods are coarse.",
            "derived": {k: v for k, v in DERIVED_HELP.items()
                        if any(k in DERIVED.get(a.profile.name, {}) for a in apps)},
            "appliances": out,
        }

    def _integrations(self, app: Appliance) -> list:
        """The profile's power integrations whose data points are recorded."""
        return [pi for pi in app.profile.power_integration.values()
                if all((app.name, key) in self._series for key in pi.keys)]

    def _uses_power(self, app: Appliance) -> bool:
        return app.config.energy_from_power and bool(self._integrations(app))

    async def _integrate(self, app: Appliance, bounds: list[datetime]) -> list[tuple[dict, float]]:
        """Per period: ({counter: kWh}, share of the period covered by power samples)."""
        integrations = self._integrations(app)
        powers = sorted({pi.power for pi in integrations})

        def point(key: str) -> str:
            return f"max by (appliance) ({self._series[(app.name, key)].selector})"

        def power(pi) -> str:
            expr = point(pi.power)
            if pi.state:  # only while the state has this raw value
                expr = f"{expr} * on(appliance) ({point(pi.state)} == bool {pi.value})"
            return expr

        async def period(a: datetime, b: datetime) -> tuple[dict, float]:
            duration = max(POWER_STEP_S, int((b - a).total_seconds()))
            window = f"[{duration}s:{POWER_STEP_S}s]"
            sums = await asyncio.gather(
                *(self._instant(f"sum_over_time(({power(pi)}){window})", b) for pi in integrations))
            counts = await asyncio.gather(
                *(self._instant(f"count_over_time(({point(p)}){window})", b) for p in powers))
            coverage = min(float(r[0]["value"][1]) * POWER_STEP_S / duration if r else 0.0 for r in counts)
            kwh = {pi.counter: (_round(float(r[0]["value"][1]) * POWER_STEP_S / 3600 * pi.to_kw, 2)
                                if r and coverage > 0 else None)
                   for pi, r in zip(integrations, sums)}
            return kwh, coverage

        return await asyncio.gather(*(period(a, b) for a, b in zip(bounds, bounds[1:])))

    def _derived(self, app: Appliance, values: dict[str, float | None]) -> dict[str, Any]:
        result = {}
        for name, (num, den, kind) in DERIVED.get(app.profile.name, {}).items():
            n = [values.get(k) for k in num]
            d = [values.get(k) for k in den]
            if any(v is None for v in n + d):
                continue
            if kind == "sum":
                result[name] = _round(sum(n), 2)
            elif sum(d) > 0:
                ratio = sum(n) / sum(d)
                result[name] = _round(1 - ratio if kind == "one_minus" else ratio, 3)
        return {"derived": result} if result else {}

    # ----------------------------------------------------------------- runtime
    async def runtime(self, app: Appliance, key: str, start: str = "", end: str = "",
                      lang: str = "en") -> dict[str, Any]:
        s = self.series(app, key)
        reg = s.reg
        if reg.enum is None and reg.data_type != "bool":
            raise HistoryError(f"'{key}' is not a state value (enum or on/off); use get_history.")
        t_start, t_end = self.span(start, end, "today")
        seconds = (t_end - t_start).total_seconds()
        if seconds > MAX_RUNTIME_DAYS * 86400:
            raise HistoryError(f"At most {MAX_RUNTIME_DAYS} days per runtime query.")
        result = await self._instant(f"{s.selector}[{int(seconds)}s]", t_end)
        # Merge the raw samples of all series of this data point (see COMBINE).
        samples = sorted({float(t): float(v) for r in result for t, v in r["values"]}.items())
        if not samples:
            raise HistoryError("no data in this time range")

        durations: dict[str, float] = {}
        runs: list[float] = []
        unknown = samples[0][0] - t_start.timestamp()
        starts = 0
        run_start: float | None = None
        for (t, v), nxt in zip(samples, samples[1:] + [(t_end.timestamp(), None)]):
            gap = nxt[0] - t
            state = str(self._enum_text(reg, v))
            if gap > MAX_SAMPLE_GAP:
                durations[state] = durations.get(state, 0) + MAX_SAMPLE_GAP / 2
                unknown += gap - MAX_SAMPLE_GAP / 2
            else:
                durations[state] = durations.get(state, 0) + gap
            on = v != 0
            if on and run_start is None:
                run_start = t
                starts += 1
            elif not on and run_start is not None:
                runs.append(t - run_start)
                run_start = None
        if samples[0][1] != 0:
            starts -= 1  # already running at the start of the range: not a start
        covered = seconds - max(0.0, unknown)
        out: dict[str, Any] = {
            "appliance": app.name,
            "key": key,
            "label": reg.display_label(lang),
            "start": t_start.isoformat(timespec="seconds"),
            "end": t_end.isoformat(timespec="seconds"),
            "hours_with_data": _round(covered / 3600, 2),
            "states": {
                state: {"hours": _round(d / 3600, 2), "share": _round(d / covered, 3) if covered else None}
                for state, d in sorted(durations.items(), key=lambda kv: -kv[1])
            },
            "starts": max(0, starts),
            "currently": str(self._enum_text(reg, samples[-1][1])),
        }
        if runs:
            out["completed_runs"] = {
                "count": len(runs),
                "avg_minutes": _round(sum(runs) / len(runs) / 60, 1),
                "min_minutes": _round(min(runs) / 60, 1),
                "max_minutes": _round(max(runs) / 60, 1),
            }
        out["note"] = ("'starts' and runs count changes from 0 (off/none) to any other state; "
                       "resolution is the 15 s export interval.")
        return out
