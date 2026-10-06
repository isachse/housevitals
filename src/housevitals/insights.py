"""Heating and hot water figures for tenants' utility bills (Nebenkostenabrechnung).

The heating costs of a house with heat pumps are their electricity costs. For a
billing period this module reports per calendar month and in total:

    heat delivered and electricity used, for space heating and hot water
    share of the heat pumps' electricity covered by the house's own PV
    weather: mean outdoor temperature, heating days and degree days (G20/15)

The heat pumps' energy counters were only recorded from some day on (for the NEO, as
counters integrated from the measured power). Months before that are **estimated**
from the operating hours per mode that the NEO-RKM writes to its SD card (imported by
tools/import_rkm_log.py): hours x the mean electricity and heat per operating hour of
the measured time. A heat pump without measured hours in a mode borrows the factor of
the other heat pumps for that mode. Every month says whether its figures are measured,
estimated or both, and which heat pumps had no data.

The page /insights (web/insights.html) turns this into costs, the tenant's share and
savings per measure, with prices and areas from the `insights` section of the config.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from .config import InsightsConfig
from .history import History, HistoryError, _replacements, _round, parse_time, period_boundaries
from .hub import Appliance

MODES = ("heating", "dhw")
COUNTERS = {  # mode -> (electricity key, heat key) as reported by History.energy
    "heating": ("electricity_heating", "heat_delivered_heating"),
    "dhw": ("electricity_dhw", "heat_delivered_dhw"),
}
HOURS_METRICS = {  # imported NEO-RKM operating hours (tools/import_rkm_log.py)
    "heating": "housevitals_rkm_heating_hours_total",
    "dhw": "housevitals_rkm_dhw_hours_total",
}
WEATHER_METRIC = "housevitals_weather_temperature_celsius"  # imported Open-Meteo archive
FORECAST_METRIC = "housevitals_forecast_temperature_celsius"  # recorded live, archive lags days
MAX_MONTHS = 24
STEP_S = 3600
MAX_STEPS_PER_QUERY = 10000  # Prometheus refuses more than 11000 points per series
MAX_GAP_S = 3 * 3600  # longer without an hours sample counts as a gap in the log
# Hours are read from this long before the period, so a gap in the log that began
# before it (e.g. an SD card not written over the turn of the year) is interpolated
# from its last sample instead of leaving the first months missing.
HOURS_LOOKBACK = timedelta(days=180)
MIN_FACTOR_HOURS = 5.0  # measured operating hours needed for a kWh-per-hour factor
MIN_WEATHER_HOURS = 20  # hourly temperatures needed for a daily mean
WH_PER_LITRE_KELVIN = 1.163


class InsightsError(HistoryError):
    pass


def billing_span(start: str, end: str, now: datetime) -> tuple[datetime, datetime]:
    """Whole local months: start snapped to its month start (default: 12 months before
    the current month), end to a month start (exclusive) or now."""
    tz = now.tzinfo
    this_month = datetime.combine(now.date().replace(day=1), datetime.min.time(), tz)
    default_start = _add_months(this_month, -12)
    t_start = parse_time(start, tz, now, default_start)
    t_start = datetime.combine(t_start.astimezone(tz).date().replace(day=1), datetime.min.time(), tz)
    t_end = now
    if end:
        e = parse_time(end, tz, now, now).astimezone(tz)
        month = datetime.combine(e.date().replace(day=1), datetime.min.time(), tz)
        t_end = min(month if e == month else _add_months(month, 1), now)
    if t_start >= t_end:
        raise InsightsError("start must be before end")
    if len(period_boundaries(t_start, t_end, "month", tz)) - 1 > MAX_MONTHS:
        raise InsightsError(f"At most {MAX_MONTHS} months per report.")
    return t_start, t_end


def _add_months(dt: datetime, months: int) -> datetime:
    index = dt.year * 12 + dt.month - 1 + months
    return dt.replace(year=index // 12, month=index % 12 + 1, day=1)


# --------------------------------------------------------------------------- pure helpers
def value_at(series: list[tuple[float, float]], t: float) -> tuple[float | None, bool]:
    """(counter reading at t, interpolated) from an hourly series sorted by time.

    Inside a gap of the log (e.g. an SD card that was not written for weeks) the reading
    is interpolated linearly between the samples around it, so the hours of the gap are
    spread over its months instead of all landing in the month the log resumes. None
    before the first sample and after the last one (beyond MAX_GAP_S)."""
    before = after = None
    for ts, v in series:
        if ts > t:
            after = (ts, v)
            break
        before = (ts, v)
    if before is None:
        return None, False
    if t - before[0] <= MAX_GAP_S:
        return before[1], False
    if after is None:
        return None, False
    share = (t - before[0]) / (after[0] - before[0])
    return before[1] + share * (after[1] - before[1]), True


def rate_factor(counter: list[tuple[float, float]], hours: list[tuple[float, float]],
                since: float) -> tuple[float, float] | None:
    """(kWh, hours) from `since` on, over the time where both series have samples."""
    c = dict(counter)
    common = sorted(t for t, _ in hours if t >= since and t in c)
    if len(common) < 2:
        return None
    h = dict(hours)
    kwh, hrs = c[common[-1]] - c[common[0]], h[common[-1]] - h[common[0]]
    if hrs < MIN_FACTOR_HOURS or kwh < 0:
        return None
    return kwh, hrs


def daily_means(points: list[tuple[float, float]], tz) -> dict[date, float]:
    """Mean temperature per local day, for days with enough hourly values."""
    days: dict[date, list[float]] = defaultdict(list)
    for ts, v in points:
        days[datetime.fromtimestamp(ts, tz).date()].append(v)
    return {d: sum(vs) / len(vs) for d, vs in days.items() if len(vs) >= MIN_WEATHER_HOURS}


def degree_days(means: dict[date, float], room_c: float, limit_c: float) -> tuple[float, int]:
    """Gradtagzahl G20/15 (VDI 3807): sum of (room - mean) over days below the heating limit."""
    heating = [room_c - t for t in means.values() if t < limit_c]
    return sum(heating), len(heating)


def dhw_litres(heat_kwh: float, cfg: InsightsConfig) -> float:
    """Litres of hot water at the tap that the delivered heat corresponds to, after
    storage and circulation losses."""
    wh_per_litre = WH_PER_LITRE_KELVIN * (cfg.dhw_temperature_c - cfg.cold_water_temperature_c)
    return heat_kwh * (1 - cfg.dhw_loss_share) * 1000 / wh_per_litre


# --------------------------------------------------------------------------- report
class Insights:
    def __init__(self, history: History, config: InsightsConfig):
        self.history = history
        self.config = config
        self.tz = history.tz

    def appliances(self) -> tuple[list[Appliance], list[Appliance]]:
        """Heat pumps with heating/hot water counters, and inverters."""
        hps, invs = [], []
        for app in self.history.hub.appliances.values():
            keys = set(app.profile.registers)
            replaced = _replacements(app)
            wanted = [k for pair in COUNTERS.values() for k in pair]
            if app.profile.kind == "inverter":
                invs.append(app)
            elif all(k in keys or k in replaced for k in wanted):
                hps.append(app)
        return hps, invs

    async def _range(self, query: str, start: datetime, end: datetime) -> list[tuple[float, float]]:
        """One series (the first result) at STEP_S, in chunks Prometheus accepts."""
        out: list[tuple[float, float]] = []
        t = start
        while t < end:
            t2 = min(end, t + timedelta(seconds=STEP_S * MAX_STEPS_PER_QUERY))
            result = await self.history.query_range(query, t, t2, STEP_S)
            if result:
                out.extend((float(ts), float(v)) for ts, v in result[0]["values"])
            t = t2 + timedelta(seconds=STEP_S)
        return out

    def _counter_query(self, app: Appliance, key: str) -> str:
        derived = _replacements(app).get(key, key)
        sel = self.history.series(app, derived).selector
        return f"max by (appliance) (last_over_time({sel}[{STEP_S}s]))"

    @staticmethod
    def _hours_query(app: Appliance, mode: str) -> str:
        return (f'max by (appliance) (last_over_time({HOURS_METRICS[mode]}'
                f'{{appliance="{app.name}"}}[{STEP_S * 2}s]))')

    async def report(self, start: str = "", end: str = "") -> dict[str, Any]:
        cfg = self.config
        t_start, t_end = billing_span(start, end, self.history.now())
        bounds = period_boundaries(t_start, t_end, "month", self.tz)
        hps, invs = self.appliances()
        if not hps:
            raise InsightsError("No heat pump with heating and hot water counters is configured.")
        iso_start, iso_end = t_start.isoformat(), t_end.isoformat()

        # Measured kWh per month (the same figures as get_energy), hourly counter and
        # operating hour series for the estimate, hourly outdoor temperatures. Counters
        # and hours are read up to now: the factors come from the measured time, which
        # may lie after the period (e.g. last year's bill, recorded since this autumn).
        now = self.history.now()
        energy_q = self.history.energy(hps + invs, "month", iso_start, iso_end)
        counter_qs = {(app.name, key): self._range(self._counter_query(app, key), t_start, now)
                      for app in hps for pair in COUNTERS.values() for key in pair}
        hours_qs = {(app.name, mode): self._range(self._hours_query(app, mode), t_start - HOURS_LOOKBACK, now)
                    for app in hps for mode in MODES}
        weather_q = self._range(f"max(last_over_time({WEATHER_METRIC}[{STEP_S}s])) or "
                                f"avg(avg_over_time({FORECAST_METRIC}[{STEP_S}s]))", t_start, t_end)
        keys = list(counter_qs) + list(hours_qs)
        energy, weather, *series = await asyncio.gather(
            energy_q, weather_q, *counter_qs.values(), *hours_qs.values())
        counters = dict(zip(keys[:len(counter_qs)], series[:len(counter_qs)]))
        hours = dict(zip(keys[len(counter_qs):], series[len(counter_qs):]))

        # Measurement start per heat pump: first hourly sample of its electricity counter.
        measured_from: dict[str, float | None] = {}
        for app in hps:
            pts = counters[(app.name, COUNTERS["heating"][0])]
            first = pts[0][0] if pts else None
            # Recorded from the start of the period: nothing to estimate.
            measured_from[app.name] = None if first is None else (
                t_start.timestamp() if first - t_start.timestamp() <= STEP_S else first)

        factors = self._factors(hps, counters, hours, measured_from)
        months = self._months(bounds, hps, invs, energy, hours, measured_from, factors)
        means = daily_means(weather, self.tz)
        for m in months:
            a, b = (datetime.fromisoformat(m[k]).date() for k in ("start", "end"))
            month_means = {d: v for d, v in means.items() if a <= d < b}
            gtz, days = degree_days(month_means, cfg.room_temperature_c, cfg.heating_limit_c)
            m["weather"] = {
                "mean_temperature_c": _round(sum(month_means.values()) / len(month_means), 1) if month_means else None,
                "degree_days": _round(gtz, 1), "heating_days": days, "days_with_data": len(month_means)}

        total_gtz, total_days = degree_days(means, cfg.room_temperature_c, cfg.heating_limit_c)
        return {
            "start": t_start.isoformat(timespec="seconds"),
            "end": t_end.isoformat(timespec="seconds"),
            "timezone": str(self.tz),
            "heat_pumps": [a.name for a in hps],
            "inverter": invs[0].name if invs else None,
            "months": months,
            "total": self._total(months, cfg),
            "weather": {
                "degree_days": _round(total_gtz, 1), "heating_days": total_days,
                "days_with_data": len(means),
                # Heating energy ~ sum over heating days of (room - outdoor): one kelvin less
                # inside saves heating_days / degree_days of it (VDI 3807 degree-day method).
                "heating_saving_per_kelvin": _round(total_days / total_gtz, 3) if total_gtz else None,
            },
            "factors": factors,
            "measured_from": {name: (datetime.fromtimestamp(ts, self.tz).isoformat(timespec="seconds")
                                     if ts else None) for name, ts in measured_from.items()},
            "settings": {k: getattr(cfg, k) for k in cfg.__dataclass_fields__},
            "notes": [
                "Measured figures come from the heat pumps' energy counters (for the NEO integrated "
                "from the measured power); they are device readings, not calibrated meters.",
                "Estimated months: operating hours per mode x the mean kWh per hour of the measured "
                "time. Factors measured in mild weather underestimate the electricity of cold months.",
                "pv_share: share of the heat pumps' electricity from the house's own PV, assumed equal "
                "to the house's self-sufficiency in that month.",
                "interpolated: the operating hour log of these heat pumps has a gap around this "
                "month; its hours are spread evenly over the gap.",
                "Hot water heat includes storage and circulation losses.",
            ],
        }

    def _factors(self, hps: list[Appliance], counters, hours, measured_from) -> dict[str, Any]:
        """kWh per operating hour per heat pump and mode from the measured time; heat
        pumps without enough hours in a mode borrow the pooled factor of the others."""
        own: dict[tuple[str, str], dict[str, float]] = {}
        for app in hps:
            since = measured_from[app.name]
            if since is None:
                continue
            for mode in MODES:
                elec_key, heat_key = COUNTERS[mode]
                e = rate_factor(counters[(app.name, elec_key)], hours[(app.name, mode)], since)
                q = rate_factor(counters[(app.name, heat_key)], hours[(app.name, mode)], since)
                if e and q and e[0] > 0:
                    own[(app.name, mode)] = {"electricity": e[0], "heat": q[0], "hours": e[1]}
        out: dict[str, Any] = {}
        for app in hps:
            out[app.name] = {}
            for mode in MODES:
                f = own.get((app.name, mode))
                source = "own"
                if f is None:
                    pool = [v for (name, m), v in own.items() if m == mode]
                    if not pool:
                        out[app.name][mode] = None
                        continue
                    f = {k: sum(p[k] for p in pool) for k in ("electricity", "heat", "hours")}
                    source = "other heat pumps"
                out[app.name][mode] = {
                    "electricity_kwh_per_h": _round(f["electricity"] / f["hours"], 3),
                    "heat_kwh_per_h": _round(f["heat"] / f["hours"], 3),
                    "measured_hours": _round(f["hours"], 1), "source": source}
        return out

    def _months(self, bounds, hps, invs, energy, hours, measured_from, factors) -> list[dict[str, Any]]:
        apps = energy["appliances"]
        by_start = {name: {p["start"]: p for p in data["periods"]} for name, data in apps.items()}
        months = []
        for a, b in zip(bounds, bounds[1:]):
            key = a.isoformat(timespec="seconds")
            month: dict[str, Any] = {"start": key, "end": b.isoformat(timespec="seconds")}
            if b < datetime.combine((a + timedelta(days=32)).date().replace(day=1), datetime.min.time(), self.tz):
                month["partial"] = True
            for mode in MODES:
                elec_key, heat_key = COUNTERS[mode]
                elec = heat = 0.0
                sources, missing, interpolated = set(), [], []
                for app in hps:
                    since = measured_from[app.name]
                    complete = True
                    if since is not None and b.timestamp() > since:  # measured part
                        p = by_start.get(app.name, {}).get(key, {}).get("kWh", {})
                        if p.get(elec_key) is None or p.get(heat_key) is None:
                            complete = False
                        else:
                            elec += p[elec_key]
                            heat += p[heat_key]
                            if p[elec_key] or p[heat_key]:
                                sources.add("measured")
                    if since is None or a.timestamp() < since:  # estimated part
                        until = b.timestamp() if since is None else min(b.timestamp(), since)
                        series = hours[(app.name, mode)]
                        (h0, gap0), (h1, gap1) = value_at(series, a.timestamp()), value_at(series, until)
                        if gap0 or gap1:
                            interpolated.append(app.name)
                        f = factors[app.name][mode]
                        if h0 is None or h1 is None or h1 < h0 or f is None:
                            complete = False
                        elif h1 > h0:
                            elec += (h1 - h0) * f["electricity_kwh_per_h"]
                            heat += (h1 - h0) * f["heat_kwh_per_h"]
                            sources.add("estimated")
                    if not complete:
                        missing.append(app.name)
                entry: dict[str, Any] = {
                    "electricity_kwh": _round(elec, 1), "heat_kwh": _round(heat, 1),
                    "source": "mixed" if len(sources) > 1 else next(iter(sources), "none")}
                if missing:
                    entry["missing"] = missing
                if interpolated:
                    entry["interpolated"] = interpolated
                month[mode] = entry
            if invs:
                p = by_start.get(invs[0].name, {}).get(key)
                month["pv_share"] = _pv_share(p["kWh"] if p else {})
            months.append(month)
        return months

    @staticmethod
    def _total(months: list[dict[str, Any]], cfg: InsightsConfig) -> dict[str, Any]:
        total: dict[str, Any] = {}
        elec_all = pv_elec = 0.0
        covered = True
        for mode in MODES:
            elec = sum(m[mode]["electricity_kwh"] or 0 for m in months)
            heat = sum(m[mode]["heat_kwh"] or 0 for m in months)
            # Mixed months count as estimated: at most one month per heat pump.
            est = sum(m[mode]["heat_kwh"] or 0 for m in months if m[mode]["source"] != "measured")
            total[mode] = {"electricity_kwh": _round(elec, 1), "heat_kwh": _round(heat, 1),
                           "spf": _round(heat / elec, 2) if elec else None,
                           "complete": not any("missing" in m[mode] for m in months),
                           "estimated_heat_share": _round(est / heat, 2) if heat else None}
        for m in months:
            e = sum(m[mode]["electricity_kwh"] or 0 for mode in MODES)
            elec_all += e
            if m.get("pv_share") is None:
                covered = covered and e == 0
            else:
                pv_elec += e * m["pv_share"]
        total["electricity_kwh"] = _round(elec_all, 1)
        total["pv_share"] = _round(pv_elec / elec_all, 3) if elec_all and covered else None
        total["dhw_litres"] = round(dhw_litres(total["dhw"]["heat_kwh"], cfg))
        return total


def _pv_share(kwh: dict[str, float | None]) -> float | None:
    """Self-sufficiency of the month: share of the house's consumption not from the grid.
    PV used on site = PV - export (battery losses ignored, as the imported portal history
    has no battery counters)."""
    pv, export, grid = (kwh.get(k) for k in ("total_pv_energy", "total_export_energy", "total_import_energy"))
    if pv is None or export is None or grid is None:
        return None
    own = max(pv - export, 0.0)
    return _round(own / (own + grid), 3) if own + grid > 0 else None
