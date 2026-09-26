"""OpenTelemetry metrics for all polled values, exported via OTLP (e.g. to Prometheus).

One instrument per data point (e.g. ``housevitals.flow_temperature``) with the
appliance as attribute; lifetime energy counters are monotonic counters, all
other numeric values gauges. Enum values are exported as their raw code, booleans
as 0/1. Values are only observed while fresh, so a dead appliance produces gaps
instead of frozen lines.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from opentelemetry.metrics import CallbackOptions, Meter, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricReader,
    MetricsData,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource

from . import __version__
from .hub import Appliance, Hub
from .registry import Register

_LOGGER = logging.getLogger(__name__)

PREFIX = "housevitals"

# Profile units -> OpenTelemetry units. UCUM where Prometheus maps them to a clean
# suffix (Cel -> _celsius, W -> _watts, ...). Energy stays "kWh" (not UCUM "kW.h",
# which Prometheus would render as "_kW_h"); per-mille uses a UCUM annotation so
# no misleading "_ratio" suffix is added.
UCUM = {
    "°C": "Cel",
    "W": "W",
    "kW": "kW",
    "kWh": "kWh",
    "Wh": "Wh",
    "V": "V",
    "A": "A",
    "Hz": "Hz",
    "%": "%",
    "bar": "bar",
    "l/min": "l/min",
    "h": "h",
    "‰": "{permille}",
}


@dataclass(frozen=True)
class _Series:
    appliance: Appliance
    reg: Register


def _numeric(data: dict) -> float | None:
    value = data.get("value")
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    raw = data.get("raw")  # enum text -> raw code
    return float(raw) if isinstance(raw, (int, float)) else None


def _attrs(app: Appliance) -> dict[str, str]:
    return {"appliance": app.name, "profile": app.profile.name, "kind": app.profile.kind}


def metric_key(key: str, is_counter: bool, used: set[str]) -> str:
    """Metric name part for a register key.

    Prometheus drops "total" tokens from counter names before appending "_total"
    (electricity_total -> electricity_kWh_total). Do the same here, so the exported
    name is what ends up in Prometheus, and keep names unique when two keys would
    collide (e.g. boiler_gas_energy and boiler_gas_energy_total).
    """
    if is_counter:
        stripped = "_".join(t for t in key.split("_") if t != "total") or key
        name = stripped if stripped not in used else key.replace("total", "overall")
    else:
        name = key
    used.add(name)
    return name


@dataclass(frozen=True)
class Instrument:
    name: str  # OpenTelemetry name, e.g. housevitals.flow_temperature
    unit: str  # OpenTelemetry unit
    is_counter: bool
    series: tuple[_Series, ...]

    @property
    def prometheus_name(self) -> str:
        return prometheus_name(self.name, self.unit, self.is_counter)


# How Prometheus' OTLP receiver turns units into name suffixes.
PROM_UNIT_SUFFIX = {
    "Cel": "celsius", "W": "watts", "kW": "kW", "kWh": "kWh", "Wh": "Wh", "V": "volts",
    "A": "amperes", "Hz": "hertz", "%": "percent", "bar": "bar", "l/min": "l_per_min",
    "h": "hours", "s": "seconds",
}


def prometheus_name(name: str, unit: str, is_counter: bool) -> str:
    """Metric name as stored by Prometheus (OTLP translation with unit suffixes)."""
    out = name.replace(".", "_")
    suffix = PROM_UNIT_SUFFIX.get(unit, "")  # annotations like {permille} add nothing
    if suffix and not out.endswith(f"_{suffix}"):
        out += f"_{suffix}"
    if is_counter:
        out += "_total"
    return out


def instrument_plan(hub: Hub) -> list[Instrument]:
    """One instrument per exported data point (shared across appliances)."""
    # Group identical data points across appliances into one instrument. A key that
    # means different things in different profiles (unit/type differ) gets the
    # profile name appended.
    by_key: dict[str, dict[tuple, list[_Series]]] = {}
    for app in hub.appliances.values():
        for regs in app.groups.values():
            for reg in regs:
                if not reg.is_numeric:
                    continue
                sig = (reg.unit, reg.is_counter)
                by_key.setdefault(reg.key, {}).setdefault(sig, []).append(_Series(app, reg))

    plan: list[Instrument] = []
    used: set[str] = set()
    for key, variants in sorted(by_key.items()):
        for (unit, is_counter), series in variants.items():
            suffix = "" if len(variants) == 1 else f"_{series[0].appliance.profile.name}"
            name = f"{PREFIX}.{metric_key(key, is_counter, used)}{suffix}"
            plan.append(Instrument(name, UCUM.get(unit or "", unit or ""), is_counter, tuple(series)))
    return plan


def register_instruments(meter: Meter, hub: Hub) -> list[str]:
    """Create observable instruments for every polled register. Returns metric names."""
    names: list[str] = []
    for inst in instrument_plan(hub):
        reg = inst.series[0].reg
        description = reg.label + (f" (values: {reg.enum})" if reg.enum else "")
        callback = _make_callback(list(inst.series))
        create = meter.create_observable_counter if inst.is_counter else meter.create_observable_gauge
        create(inst.name, [callback], unit=inst.unit, description=description)
        names.append(inst.name)

    meter.create_observable_gauge(
        f"{PREFIX}.up", [lambda o: _up(hub)], unit="",
        description="1 if the last Modbus request to the appliance succeeded",
    )
    meter.create_observable_gauge(
        f"{PREFIX}.poll.duration", [lambda o: _poll_duration(hub)], unit="s",
        description="Duration of the last poll per appliance and poll group",
    )
    meter.create_observable_counter(
        f"{PREFIX}.poll.errors", [lambda o: _poll_errors(hub)], unit="",
        description="Failed polls per appliance and poll group",
    )
    meter.create_observable_gauge(
        f"{PREFIX}.appliance.info", [lambda o: _info(hub)], unit="",
        description="Static appliance information (serial, firmware, type) as attributes",
    )
    return names + [f"{PREFIX}.{n}" for n in ("up", "poll.duration", "poll.errors", "appliance.info")]


def _make_callback(series: list[_Series]):
    def callback(options: CallbackOptions) -> Iterable[Observation]:
        for s in series:
            if s.appliance.up is False:
                continue  # no flat line of last known values during an outage
            entry = s.appliance.fresh(s.reg)
            if entry is None:
                continue
            value = _numeric(entry.data)
            if value is not None:
                yield Observation(value, _attrs(s.appliance))

    return callback


def _up(hub: Hub) -> Iterable[Observation]:
    for app in hub.appliances.values():
        if app.up is not None:
            yield Observation(1.0 if app.up else 0.0, _attrs(app))


def _poll_duration(hub: Hub) -> Iterable[Observation]:
    for app in hub.appliances.values():
        for group, status in app.status.items():
            if status.duration is not None:
                yield Observation(status.duration, {**_attrs(app), "group": group})


def _poll_errors(hub: Hub) -> Iterable[Observation]:
    for app in hub.appliances.values():
        for group, status in app.status.items():
            yield Observation(status.errors, {**_attrs(app), "group": group})


def _info(hub: Hub) -> Iterable[Observation]:
    for app in hub.appliances.values():
        attrs = {**_attrs(app), "host": app.config.host}
        for reg in app.groups.get("static", []):
            entry = app.cache.get(reg.key)
            value = entry.data.get("value") if entry else None
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                attrs[reg.key] = str(value)
        yield Observation(1.0, attrs)


class BufferingExporter(MetricExporter):
    """Wraps the OTLP exporter so a short Prometheus outage leaves no gap.

    Failed batches are kept (up to MAX_BACKLOG_AGE) and re-sent oldest first once
    Prometheus answers again; Prometheus accepts them thanks to its out-of-order
    window (30 min). Older batches are dropped and counted. Outages are logged once
    when they start and once when they end, instead of on every export.
    """

    MAX_BACKLOG_AGE = 25 * 60.0

    def __init__(self, inner: MetricExporter):
        super().__init__(preferred_temporality=inner._preferred_temporality,
                         preferred_aggregation=inner._preferred_aggregation)
        self._inner = inner
        self._backlog: deque[tuple[float, MetricsData]] = deque()
        self._lock = threading.Lock()
        self.available: bool | None = None
        self.since: float | None = None
        self.dropped = 0

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000,
               **kwargs) -> MetricExportResult:
        with self._lock:
            now = time.time()
            self._backlog.append((now, metrics_data))
            while self._backlog and now - self._backlog[0][0] > self.MAX_BACKLOG_AGE:
                self._backlog.popleft()
                self.dropped += 1
            while self._backlog:
                result = self._inner.export(self._backlog[0][1], timeout_millis=timeout_millis)
                if result is not MetricExportResult.SUCCESS:
                    self._set_available(False)
                    return result
                self._backlog.popleft()
            self._set_available(True)
            return MetricExportResult.SUCCESS

    def _set_available(self, available: bool) -> None:
        if available == self.available:
            return
        if available is False:
            _LOGGER.warning("Metric export failing; buffering up to %d min for re-sending",
                            self.MAX_BACKLOG_AGE // 60)
        elif self.available is False:
            _LOGGER.warning("Metric export recovered; buffered batches re-sent (%d dropped in total)",
                            self.dropped)
        self.available, self.since = available, time.time()

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"available": self.available, "buffered_exports": len(self._backlog),
                               "dropped_exports": self.dropped}
        if self.since is not None:
            out["since"] = datetime.fromtimestamp(self.since).astimezone().isoformat(timespec="seconds")
        return out

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return self._inner.force_flush(timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        self._inner.shutdown(timeout_millis=timeout_millis)


def setup_metrics(hub: Hub, readers: list[MetricReader] | None = None
                  ) -> tuple[MeterProvider | None, BufferingExporter | None]:
    """Create a MeterProvider exporting all polled values.

    Without explicit readers, a buffering OTLP/HTTP exporter to service.otlp_endpoint
    is used; returns (None, None) if no endpoint is configured.
    """
    service = hub.config.service
    exporter = None
    if readers is None:
        if not service.otlp_endpoint:
            return None, None
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        # The wrapper reports outages; the OTLP exporter would log every failed retry.
        logging.getLogger("opentelemetry.exporter.otlp.proto.http.metric_exporter").setLevel(logging.CRITICAL)
        exporter = BufferingExporter(OTLPMetricExporter(endpoint=service.otlp_endpoint, timeout=5))
        readers = [PeriodicExportingMetricReader(
            exporter, export_interval_millis=int(service.export_interval * 1000),
            export_timeout_millis=10_000)]
    resource = Resource.create({
        "service.name": "housevitals",
        "service.version": __version__,
        "service.instance.id": socket.gethostname(),
    })
    provider = MeterProvider(resource=resource, metric_readers=readers)
    register_instruments(provider.get_meter("housevitals", __version__), hub)
    return provider, exporter
