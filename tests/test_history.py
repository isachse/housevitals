"""History tools against a small in-process fake of the Prometheus HTTP API."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from housevitals.config import DeviceConfig, ServerConfig, ServiceConfig
from housevitals.errors import UnavailableError
from housevitals.history import History, HistoryError, _step_for, parse_time, period_boundaries
from housevitals.hub import Hub
from housevitals.context import Services
from housevitals.server import build_server

TZ = ZoneInfo("Europe/Berlin")


class FakePrometheus:
    """Stores samples per (metric, appliance) and evaluates the query shapes History uses."""

    FN = re.compile(r"^(\w+)\((.*)\[(\w+)\]\)$")
    RAW = re.compile(r"^(.*)\[(\d+)s\]$")
    AGG = re.compile(r"^(max|min|sum|avg) by \(([^)]*)\) \((.*)\)$")
    # sum/count over a subquery of power (optionally only while a state has a value)
    SUBQ = re.compile(r"^(sum|count)_over_time\(\((.*)\)\[(\d+)s:(\d+)s\]\)$")
    POINT = re.compile(r"^max by \(appliance\) \(([^()]*)\)$")
    POINT_WHILE = re.compile(r"^max by \(appliance\) \(([^()]*)\) \* on\(appliance\) "
                             r"\(max by \(appliance\) \(([^()]*)\) == bool (\d+)\)$")
    LOOKBACK = 60.0

    def __init__(self):
        # (metric, appliance, instance) -> samples; several instances = several series
        self.samples: dict[tuple[str, str, str], list[tuple[float, float]]] = {}
        self.queries: list[str] = []

    def add(self, metric, appliance, points, instance="host-a"):
        self.samples.setdefault((metric, appliance, instance), []).extend(points)

    @staticmethod
    def _seconds(d: str) -> float:
        return float(d[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[d[-1]]

    def _match(self, selector: str):
        names = apps = None
        if m := re.match(r'^\{__name__=~"([^"]+)",appliance=~"([^"]+)"\}$', selector):
            names, apps = set(m.group(1).split("|")), set(m.group(2).split("|"))
        elif m := re.match(r'^(\w+)\{appliance(=~|=)"([^"]+)"\}$', selector):
            names, apps = {m.group(1)}, set(m.group(3).split("|"))
        else:
            raise AssertionError(f"unsupported selector {selector}")
        return {k: v for k, v in self.samples.items() if k[0] in names and k[1] in apps}

    def _eval(self, fn, selector, window, t):
        out = []
        for (metric, app, instance), pts in self._match(selector).items():
            vals = [v for ts, v in pts if t - window < ts <= t]
            if not vals:
                continue
            value = {"last_over_time": vals[-1], "min_over_time": min(vals), "max_over_time": max(vals),
                     "avg_over_time": sum(vals) / len(vals), "count_over_time": len(vals)}[fn]
            labels = {"appliance": app, "instance": instance}
            if fn == "last_over_time":  # like Prometheus: keeps the metric name
                labels["__name__"] = metric
            out.append((labels, value))
        return out

    def _instant_point(self, selector: str, t: float) -> dict[str, float]:
        """Instant value per appliance (latest sample within the lookback, max over series)."""
        out: dict[str, float] = {}
        for (_, app, _), pts in self._match(selector).items():
            recent = [v for ts, v in pts if t - self.LOOKBACK < ts <= t]
            if recent:
                out[app] = max(out.get(app, recent[-1]), recent[-1])
        return out

    def _subquery(self, fn: str, inner: str, window: float, step: float, t: float):
        if m := self.POINT_WHILE.match(inner):
            power_sel, state_sel, code = m.group(1), m.group(2), float(m.group(3))
        elif m := self.POINT.match(inner):
            power_sel, state_sel, code = m.group(1), None, None
        else:
            raise AssertionError(f"unsupported subquery {inner}")
        values: dict[str, list[float]] = {}
        k = int(t // step)
        while k * step > t - window:
            at = k * step
            power = self._instant_point(power_sel, at)
            state = self._instant_point(state_sel, at) if state_sel else None
            for app, p in power.items():
                if state is not None:
                    if app not in state:
                        continue
                    p *= 1.0 if state[app] == code else 0.0
                values.setdefault(app, []).append(p)
            k -= 1
        return [({"appliance": app}, sum(vs) if fn == "sum" else len(vs)) for app, vs in values.items()]

    def _vector(self, q: str, t: float):
        """Instant evaluation of FN or an aggregation over FN: [(labels, value)]."""
        if agg := self.AGG.match(q):
            op, by = agg.group(1), [x.strip() for x in agg.group(2).split(",")]
            groups: dict[tuple, list[float]] = {}
            for labels, v in self._vector(agg.group(3), t):
                key = tuple((k, labels[k]) for k in by if k in labels)
                groups.setdefault(key, []).append(v)
            combine = {"max": max, "min": min, "sum": sum, "avg": lambda vs: sum(vs) / len(vs)}[op]
            return [(dict(key), combine(vs)) for key, vs in groups.items()]
        m = self.FN.match(q)
        return self._eval(m.group(1), m.group(2), self._seconds(m.group(3)), t)

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params["query"]
        self.queries.append(q)
        if request.url.path.endswith("/query_range"):
            start, end, step = (float(request.url.params[k]) for k in ("start", "end", "step"))
            series: dict[tuple, dict] = {}
            t = start
            while t <= end:
                for labels, v in self._vector(q, t):
                    key = tuple(sorted(labels.items()))
                    series.setdefault(key, {"metric": labels, "values": []})["values"].append([t, str(v)])
                t += step
            data = {"resultType": "matrix", "result": list(series.values())}
        else:
            t = float(request.url.params["time"])
            if m := self.SUBQ.match(q):
                result = self._subquery(m.group(1), m.group(2), float(m.group(3)), float(m.group(4)), t)
                data = {"resultType": "vector",
                        "result": [{"metric": labels, "value": [t, str(v)]} for labels, v in result]}
            elif self.AGG.match(q):
                data = {"resultType": "vector",
                        "result": [{"metric": labels, "value": [t, str(v)]} for labels, v in self._vector(q, t)]}
            elif m := self.FN.match(q):
                result = self._eval(m.group(1), m.group(2), self._seconds(m.group(3)), t)
                if m.group(1) != "last_over_time" and len(result) != len({tuple(l.items()) for l, _ in result}):
                    return httpx.Response(422, json={"status": "error", "error": "same labelset"})
                data = {"resultType": "vector",
                        "result": [{"metric": labels, "value": [t, str(v)]} for labels, v in result]}
            elif m := self.RAW.match(q):
                window = float(m.group(2))
                data = {"resultType": "matrix", "result": [
                    {"metric": {"appliance": app, "instance": instance},
                     "values": [[ts, str(v)] for ts, v in pts if t - window < ts <= t]}
                    for (_, app, instance), pts in self._match(m.group(1)).items()]}
            else:
                raise AssertionError(f"unsupported query {q}")
        return httpx.Response(200, json={"status": "success", "data": data})


def _hub():
    devices = [
        DeviceConfig(name="hp", host="127.0.0.1", profile="neo"),
        DeviceConfig(name="inverter", host="127.0.0.1", profile="sungrow_sh"),
    ]
    return Hub(ServerConfig(devices=devices, service=ServiceConfig(prometheus_url="http://prom")))


@pytest.fixture
def prom():
    return FakePrometheus()


def _history(prom, now):
    history = History(_hub(), "http://prom", "Europe/Berlin", transport=httpx.MockTransport(prom.handler))
    history.now = lambda: now
    return history


def test_parse_time_and_boundaries():
    now = datetime(2026, 3, 30, 12, 0, tzinfo=TZ)
    assert parse_time("24h", TZ, now, now) == now - timedelta(hours=24)
    assert parse_time("now-30m", TZ, now, now) == now - timedelta(minutes=30)
    assert parse_time("yesterday", TZ, now, now) == datetime(2026, 3, 29, tzinfo=TZ)
    assert parse_time("2026-03-01", TZ, now, now) == datetime(2026, 3, 1, tzinfo=TZ)
    assert parse_time("2026-03-01T06:30", TZ, now, now).hour == 6
    with pytest.raises(HistoryError):
        parse_time("last tuesday", TZ, now, now)

    # Days follow local midnight across the DST switch (29 March has 23 hours).
    bounds = period_boundaries(datetime(2026, 3, 28, 15, tzinfo=TZ), now, "day", TZ)
    assert [b.isoformat() for b in bounds[:3]] == [
        "2026-03-28T00:00:00+01:00", "2026-03-29T00:00:00+01:00", "2026-03-30T00:00:00+02:00"]
    assert bounds[-1] == now
    months = period_boundaries(datetime(2025, 11, 15, tzinfo=TZ), now, "month", TZ)
    assert [b.date().isoformat() for b in months[:-1]] == [
        "2025-11-01", "2025-12-01", "2026-01-01", "2026-02-01", "2026-03-01"]
    weeks = period_boundaries(datetime(2026, 3, 18, tzinfo=TZ), now, "week", TZ)
    assert all(b.weekday() == 0 for b in weeks[:-1])
    assert _step_for(86400, 100) == 900 and _step_for(600, 500) == 15


async def test_energy_per_local_day(prom):
    now = datetime(2026, 9, 26, 18, 0, tzinfo=TZ)
    d24, d25, d26 = (datetime(2026, 9, d, tzinfo=TZ).timestamp() for d in (24, 25, 26))
    # Counters sampled hourly from 24 Sep 00:30 on; +1 kWh electricity, +4 kWh heat per hour.
    for i in range(66):
        t = d24 + 1800 + i * 3600
        prom.add("housevitals_electricity_kWh_total", "hp", [(t, 100 + i)])
        prom.add("housevitals_heat_delivered_kWh_total", "hp", [(t, 400 + 4 * i)])
        prom.add("housevitals_pv_energy_kWh_total", "inverter", [(t, 1000 + 2 * i)])
        prom.add("housevitals_import_energy_kWh_total", "inverter", [(t, 50 + 0.5 * i)])
        prom.add("housevitals_direct_consumption_kWh_total", "inverter", [(t, 10 + i)])
        prom.add("housevitals_battery_discharge_kWh_total", "inverter", [(t, 20 + 0.5 * i)])
        prom.add("housevitals_export_energy_kWh_total", "inverter", [(t, 5 + 0.5 * i)])
    history = _history(prom, now)
    result = await history.energy(list(history.hub.appliances.values()), "day", "2026-09-23")

    hp = result["appliances"]["hp"]
    assert hp["periods_without_data"] == 1  # 23 Sep, before recording
    first, second, today = hp["periods"]
    # 24 Sep: recording started 00:30 -> first sample used as start, flagged partial.
    assert first["start"].startswith("2026-09-24") and first["partial"]
    assert first["kWh"]["electricity_total"] == 23
    # 25 Sep: complete local day from the readings at both midnights.
    assert second["kWh"]["electricity_total"] == 24 and "partial" not in second
    assert second["derived"]["spf"] == 4.0
    assert today["partial"] and today["kWh"]["heat_delivered_total"] == 4 * 18
    assert hp["total"]["kWh"]["electricity_total"] == 23 + 24 + 18

    inv = result["appliances"]["inverter"]["periods"][1]
    assert inv["kWh"]["total_pv_energy"] == 48
    derived = inv["derived"]
    assert derived["house_consumption"] == 24 + 12 + 12
    assert derived["self_sufficiency"] == 0.75  # 1 - 12/48
    assert derived["self_consumption_rate"] == 0.75  # 1 - 12/48
    assert d25 and d26  # boundaries used above

    with pytest.raises(HistoryError):
        await history.energy(list(history.hub.appliances.values()), "day", "2025-01-01")


async def test_derived_figures_need_the_same_coverage(prom):
    # PV, import and export imported for days; direct consumption and battery only
    # recorded since 25 Sep 12:00: house consumption and self-sufficiency for 25 Sep (and
    # the total) would mix both and are left out; self-consumption (PV, export) stays.
    now = datetime(2026, 9, 26, 18, 0, tzinfo=TZ)
    d24 = datetime(2026, 9, 24, tzinfo=TZ).timestamp()
    for i in range(66):
        t = d24 + 1800 + i * 3600
        prom.add("housevitals_pv_energy_kWh_total", "inverter", [(t, 1000 + 2 * i)])
        prom.add("housevitals_import_energy_kWh_total", "inverter", [(t, 50 + 0.5 * i)])
        prom.add("housevitals_export_energy_kWh_total", "inverter", [(t, 5 + 0.5 * i)])
        if i >= 36:  # from 25 Sep 12:30
            prom.add("housevitals_direct_consumption_kWh_total", "inverter", [(t, 10 + i)])
            prom.add("housevitals_battery_discharge_kWh_total", "inverter", [(t, 20 + 0.5 * i)])
    history = _history(prom, now)
    result = await history.energy([history.hub.get("inverter")], "day", "2026-09-24")
    inv = result["appliances"]["inverter"]
    day25 = next(p for p in inv["periods"] if p["start"].startswith("2026-09-25"))
    assert day25["partial"] and day25["kWh"]["total_direct_consumption"] is not None
    assert "house_consumption" not in day25.get("derived", {})
    assert "self_sufficiency" not in day25.get("derived", {})
    assert day25["derived"]["self_consumption_rate"] == 0.75
    assert "self_sufficiency" not in inv["total"].get("derived", {})
    day26 = next(p for p in inv["periods"] if p["start"].startswith("2026-09-26"))
    assert "self_sufficiency" in day26["derived"]  # all counters cover 26 Sep from its start


async def test_history_series_and_errors(prom):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=TZ)
    t0 = now.timestamp() - 3600
    prom.add("housevitals_flow_temperature_celsius", "hp", [(t0 + i * 60, 30 + i % 10) for i in range(60)])
    prom.add("housevitals_compressor", "hp", [(t0 + i * 60, 10 if i >= 30 else 0) for i in range(60)])
    history = _history(prom, now)
    app = history.hub.get("hp")
    result = await history.history(app, ["flow_temperature", "compressor"], "1h", "", 12)
    flow = result["series"]["flow_temperature"]
    assert (flow["min"], flow["max"], flow["unit"]) == (30, 39, "°C")
    assert result["step_s"] == 300 and len(flow["points"]) <= 13
    comp = result["series"]["compressor"]
    assert comp["last"] == "on" and {p[1] for p in comp["points"]} <= {"on", "off"}

    with pytest.raises(HistoryError, match="not recorded"):
        await history.history(app, ["low_pressure"])  # not in the poll plan without extra_keys
    with pytest.raises(HistoryError, match="Unknown register"):
        await history.history(app, ["nope"])


async def test_runtime_counts_starts_and_runs(prom):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=TZ)
    t0 = datetime(2026, 9, 26, 0, 0, tzinfo=TZ).timestamp()
    # 12 h of 15 s samples: compressor on 06:00-07:00 and 09:00-09:30, no data 10:00-11:00.
    points = []
    for i in range(12 * 240):
        t = t0 + i * 15
        if 10 * 3600 <= t - t0 < 11 * 3600:
            continue
        hour = (t - t0) / 3600
        points.append((t, 10 if 6 <= hour < 7 or 9 <= hour < 9.5 else 0))
    prom.add("housevitals_compressor", "hp", points)
    history = _history(prom, now)
    result = await history.runtime(history.hub.get("hp"), "compressor", "today")
    assert result["starts"] == 2
    assert result["completed_runs"] == {"count": 2, "avg_minutes": 45.0, "min_minutes": 30.0, "max_minutes": 60.0}
    assert result["states"]["on"]["hours"] == 1.5
    assert result["hours_with_data"] == pytest.approx(11, abs=0.05)
    assert result["currently"] == "off"
    with pytest.raises(HistoryError, match="not a state"):
        await history.runtime(history.hub.get("hp"), "flow_temperature")


async def test_history_tools_registered_only_with_prometheus():
    hub = _hub()
    names = {t.name for t in await build_server(Services.create(hub.config, hub)).list_tools()}
    assert {"get_history", "get_energy", "get_runtime"} <= names
    plain = ServerConfig(devices=[DeviceConfig(name="hp", host="127.0.0.1", profile="neo")])
    names = {t.name for t in await build_server(plain).list_tools()}
    assert "get_history" not in names


async def test_prometheus_down_is_reported():
    def fail(request):
        raise httpx.ConnectError("refused")

    history = History(_hub(), "http://prom", transport=httpx.MockTransport(fail))
    with pytest.raises(UnavailableError, match="not reachable"):
        await history.history(history.hub.get("hp"), ["flow_temperature"])
